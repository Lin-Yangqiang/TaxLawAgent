"""Agent 装配。

模型层用 eurekax 的 ChatOpenAICompatible（它只做一件事：把推理引擎的 reasoning 字段
捞回 additional_kwargs），本地从 assets/site-packages 直接引用源码，内网装上
hw-finance-agentframework 后删掉 sys.path 这段即可，import 路径不变。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 本地没有内网包，直接用仓库里的 SDK 源码；内网环境删除这两行
_VENDORED = ROOT / "assets" / "site-packages"
if _VENDORED.is_dir() and str(_VENDORED) not in sys.path:
    sys.path.insert(0, str(_VENDORED))

# 直接 `python src/tax_agent/agent.py` 跑自检时 sys.path[0] 是本文件目录而非 src/
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from deepagents import create_deep_agent  # noqa: E402
from deepagents.backends.filesystem import FilesystemBackend  # noqa: E402
from deepagents.middleware.filesystem import FilesystemPermission  # noqa: E402
from eurekax.openai_compatible import ChatOpenAICompatible  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from loguru import logger  # noqa: E402

from tax_agent import config  # noqa: E402
from tax_agent import nettls  # noqa: E402
from tax_agent.tools import TOOLS  # noqa: E402

SKILLS_DIR = ROOT / "skills"


def _skills_backend() -> FilesystemBackend:
    """技能目录的后端。virtual_mode 必须显式传，不能用默认值。

    deepagents 0.6.2 的默认是 `None`→发弃用警告后落到 `False`，0.7.x 改成了 `True`。
    `False` 下 `ls("/")` 会穿透到盘符根而不是 root_dir，技能列表返回空——技能加载失败
    是静默的（模型照常回答，只是丢掉全部检索纪律），所以这个默认值变化在内网真实踩过一次。
    """
    return FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True)

SYSTEM_PROMPT = """你是税法检索与解读助手，服务对象是企业税务专家。

你的全部法规知识来自 search_regulation 和 fetch_clause 两个工具。你自己记忆中的税法条文
可能过时或不适用于本法规库，**不得作为回答依据**。没有检索到依据时，如实说明检索不到，
这比给出一个听起来合理但无法追溯的答案有价值得多。

**绝不把用户给的金额代入算出税额。** 用户报了数字时，只给适用税率和计算规则，并列出
从他那个数字到计税基数还差哪些调整项，让他自己算。不要以"仅供参考""这属于推断"
之类的措辞把结果说出来——标注了免责不等于没给结论，用户会直接拿去申报。

用户是专业人士，回答要直接、结构化、可追溯，不要铺垫和免责声明堆砌。
遵循 regulation-retrieval 技能中的检索、引用和版本纪律。"""


def build_model() -> ChatOpenAICompatible:
    """从环境变量构造模型。缺配置时直接失败，不降级成假 Agent。"""
    config.check("model")
    # 法规解读要可复现，能设 0 就设 0；但部分托管端点只接受自己的默认值
    # （kimi-for-coding 强制 temperature=1），所以不写死，不配就用端点默认
    temperature = os.getenv("TAX_AGENT_TEMPERATURE")
    logger.info("模型配置：model={} base_url={}", os.environ["TAX_AGENT_MODEL"], os.environ["TAX_AGENT_BASE_URL"])
    # 内网 MaaS 同样是自签证书 + HIS 代理拦截，要和 TTC 走同一套 TLS/代理配置。
    # 注意这条链路上跑的是 api_key：关掉证书校验时中间人能直接拿走凭证，
    # 所以 nettls 默认保留校验，降级必须显式开 TAX_AGENT_INSECURE_TLS=1。
    # 传 http_client 会让 httpx 不再自动探测系统代理，这是预期行为（我们主动绕过代理）。
    return ChatOpenAICompatible(
        model=os.environ["TAX_AGENT_MODEL"],
        base_url=os.environ["TAX_AGENT_BASE_URL"],
        api_key=os.environ["TAX_AGENT_API_KEY"],
        http_client=httpx.Client(**nettls.client_kwargs()),
        **({"temperature": float(temperature)} if temperature else {}),
    )


def build_agent():
    """返回 CompiledStateGraph。checkpointer 用内存实现，P2 换 OpenGaussAsyncSaver。

    backend 必须是 FilesystemBackend：默认的 StateBackend 从图状态读文件而非磁盘，
    配磁盘技能路径时会静默加载不到技能。root_dir 收窄到 skills/，并禁写，
    模型只能读自己的技能定义，碰不到仓库其余部分。
    """
    backend = _skills_backend()
    from deepagents.middleware.skills import _list_skills_with_errors

    skills, error = _list_skills_with_errors(backend, "/")
    if error is not None:
        logger.warning("技能加载失败：{}（模型将丢失全部检索纪律，静默生效）", error)
    else:
        logger.info("技能加载完成：{} 个 -> {}", len(skills), [s["name"] for s in skills])

    return create_deep_agent(
        model=build_model(),
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        skills=["/"],
        backend=backend,
        permissions=[FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")],
        checkpointer=InMemorySaver(),
    )


def _demo() -> None:
    """装配自检：不调用模型，只验证技能确实被发现、工具确实注册进图。

    技能加载失败是静默的——模型照常回答，只是丢掉了全部检索纪律，所以必须有断言兜住。
    """
    import os

    from deepagents.middleware.skills import _list_skills_with_errors

    # 走 _skills_backend() 而不是自己构造 FilesystemBackend：自检和运行时必须同一份配置，
    # 否则 virtual_mode 这类默认值差异在自检里看不出来（内网就是这样漏过去的）
    skills, error = _list_skills_with_errors(_skills_backend(), "/")
    assert error is None, error
    assert [s["name"] for s in skills] == ["regulation-retrieval"], skills

    os.environ.setdefault("TAX_AGENT_BASE_URL", "http://localhost/v1")
    os.environ.setdefault("TAX_AGENT_API_KEY", "dummy")
    os.environ.setdefault("TAX_AGENT_MODEL", "dummy")
    registered = set(build_agent().nodes["tools"].bound._tools_by_name)
    assert {"search_regulation", "fetch_clause"} <= registered, registered
    print("agent wiring self-check ok")


if __name__ == "__main__":
    _demo()
