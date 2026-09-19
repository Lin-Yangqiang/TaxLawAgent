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

from deepagents import create_deep_agent  # noqa: E402
from deepagents.backends.filesystem import FilesystemBackend  # noqa: E402
from deepagents.middleware.filesystem import FilesystemPermission  # noqa: E402
from eurekax.openai_compatible import ChatOpenAICompatible  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from loguru import logger  # noqa: E402

from tax_agent.tools import TOOLS  # noqa: E402

SKILLS_DIR = ROOT / "skills"

SYSTEM_PROMPT = """你是税法检索与解读助手，服务对象是企业税务专家。

你的全部法规知识来自 search_regulation 和 fetch_clause 两个工具。你自己记忆中的税法条文
可能过时或不适用于本法规库，**不得作为回答依据**。没有检索到依据时，如实说明检索不到，
这比给出一个听起来合理但无法追溯的答案有价值得多。

用户是专业人士，回答要直接、结构化、可追溯，不要铺垫和免责声明堆砌。
遵循 regulation-retrieval 技能中的检索、引用和版本纪律。"""


def build_model() -> ChatOpenAICompatible:
    """从环境变量构造模型。缺配置时直接失败，不降级成假 Agent。"""
    missing = [k for k in ("TAX_AGENT_BASE_URL", "TAX_AGENT_API_KEY", "TAX_AGENT_MODEL") if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"缺少模型配置环境变量：{', '.join(missing)}（参考 .env.example）")
    # 法规解读要可复现，能设 0 就设 0；但部分托管端点只接受自己的默认值
    # （kimi-for-coding 强制 temperature=1），所以不写死，不配就用端点默认
    temperature = os.getenv("TAX_AGENT_TEMPERATURE")
    logger.info("模型配置：model={} base_url={}", os.environ["TAX_AGENT_MODEL"], os.environ["TAX_AGENT_BASE_URL"])
    return ChatOpenAICompatible(
        model=os.environ["TAX_AGENT_MODEL"],
        base_url=os.environ["TAX_AGENT_BASE_URL"],
        api_key=os.environ["TAX_AGENT_API_KEY"],
        **({"temperature": float(temperature)} if temperature else {}),
    )


def build_agent():
    """返回 CompiledStateGraph。checkpointer 用内存实现，P2 换 OpenGaussAsyncSaver。

    backend 必须是 FilesystemBackend：默认的 StateBackend 从图状态读文件而非磁盘，
    配磁盘技能路径时会静默加载不到技能。root_dir 收窄到 skills/，并禁写，
    模型只能读自己的技能定义，碰不到仓库其余部分。
    """
    backend = FilesystemBackend(root_dir=SKILLS_DIR)
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

    skills, error = _list_skills_with_errors(FilesystemBackend(root_dir=SKILLS_DIR), "/")
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
