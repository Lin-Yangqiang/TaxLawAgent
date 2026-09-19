"""环境变量的唯一真相来源：加载 `.env`、列清单、启动时校验齐全性。

三个真实问题：`.env` 加载逻辑曾经写了两遍（server.py / cli.py）且行为不一致；
环境变量清单只存在于 `.env.example`，代码里没有真相来源；`.env.example` 会和代码
悄悄漂移（新增一个变量却忘了写文档）。这个模块把三件事收进一处：`load_dotenv()`
是唯一实现，`ENV_VARS` 是唯一清单，`_demo()` 对账 `.env.example` 防止再漂移。

不做的事：不引入类型转换框架，不给每个变量包一个 getter——`os.getenv("X")`
已经足够直白，模块里没收的变量继续原样读取环境变量。
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

# PORT 是 web-demo/serve.py 读的 demo 脚本配置，不是服务配置，不收进清单。
# 全仓环境变量清单。新增一个环境变量时必须同时写进这里和 .env.example，
# 下面的 _demo() 会对账，漏了哪边都跑不过自检。
ENV_VARS: dict[str, tuple[str, str]] = {
    "TAX_AGENT_BASE_URL": ("model", "模型端点（OpenAI 兼容）"),
    "TAX_AGENT_API_KEY": ("model", "模型 API key"),
    "TAX_AGENT_MODEL": ("model", "模型名"),
    "TAX_AGENT_TEMPERATURE": ("model", "采样温度，可选；不配就用端点默认值"),
    "TAX_AGENT_SOURCE": ("source", "检索源：local / ttc_public / ttc_user，缺省 local"),
    "TTC_BASE_URL": ("source", "TTC 服务根地址"),
    "TTC_APIGW_ID": ("source", "APIGW 静态 AK，仅 ttc_public 用"),
    "TTC_APIGW_APPKEY": ("source", "APIGW 静态 SK，仅 ttc_public 用"),
    "TTC_APIC_TOKEN_URL": ("source", "APIC 换 token 服务根地址，仅 ttc_public 用"),
    "TTC_APIC_APP_ID": ("source", "APIC 应用 ID，仅 ttc_public 用"),
    "TTC_APIC_SECRET": ("source", "APIC 应用密钥，仅 ttc_public 用"),
    "TAX_AGENT_LOG_LEVEL": ("runtime", "loguru 日志级别，默认 INFO"),
    "TAX_AGENT_ENV": ("runtime", "设为 prod 时关闭自动加载 .env"),
    "TAX_AGENT_CA_BUNDLE": ("runtime", "内网根 CA 证书路径"),
    "TAX_AGENT_INSECURE_TLS": ("runtime", "显式关闭 TLS 校验，仅内网联调"),
    "TAX_AGENT_TRUST_ENV": ("runtime", "是否读 HTTP_PROXY 等代理环境变量"),
    "TAX_AGENT_USER_TOKEN": ("runtime", "cli.py --url 走 HTTP 模式时透传的用户 token"),
    "TAX_AGENT_DB_HOST": ("db", "OpenGauss 主机地址，5 个 DB_* 变量要么都不配（内存模式）要么全配"),
    "TAX_AGENT_DB_PORT": ("db", "OpenGauss 端口"),
    "TAX_AGENT_DB_USER": ("db", "OpenGauss 用户名"),
    "TAX_AGENT_DB_PASSWORD": ("db", "OpenGauss 密码"),
    "TAX_AGENT_DB_NAME": ("db", "OpenGauss 数据库名"),
    "TAX_AGENT_DB_SCHEMA": ("db", "OpenGauss schema，可选，不配就用默认 schema"),
}

_REQUIRED_MODEL = ("TAX_AGENT_BASE_URL", "TAX_AGENT_API_KEY", "TAX_AGENT_MODEL")


def load_dotenv() -> None:
    """联调时把 `.env` 读进环境变量。生产环境**不读**。

    为什么需要：uvicorn 用 `--factory` 起服务时不经过 `cli.py`，没人加载 `.env`，
    模型三件套会全缺，表现为启动即报"缺少模型配置环境变量"。

    为什么要挡生产：`.env` 是开发者本机的配置（联调用的模型端点、应用级凭证），
    在生产悄悄生效会让服务连到错误的后端，而且优先级问题极难排查。
    生产由部署平台注入真实环境变量，设 `TAX_AGENT_ENV=prod` 关掉这里。

    用 `setdefault`：已经导出到 shell 的变量优先，`.env` 只补没有的。
    """
    if os.getenv("TAX_AGENT_ENV") == "prod":
        return
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    logger.info("已加载 .env（联调用；生产设 TAX_AGENT_ENV=prod 关闭）")


def check(group: str) -> None:
    """启动时校验某一组环境变量齐全，缺什么一次全报出来。

    Args:
        group: "model" 或 "source"。

    Raises:
        RuntimeError: 有必需变量缺失。消息里列出全部缺失项和它们的用途，
            不要让人配一个跑一次炸一次。
    """
    if group == "model":
        required = _REQUIRED_MODEL
    elif group == "source":
        # local 不需要 TTC，其余两个源都要有个 base_url 才谈得上发请求
        required = () if os.getenv("TAX_AGENT_SOURCE", "local") == "local" else ("TTC_BASE_URL",)
    else:
        raise ValueError(f"未知分组：{group!r}，可选 model / source")

    missing = [k for k in required if not os.getenv(k)]
    if missing:
        detail = "、".join(f"{k}（{ENV_VARS[k][1]}）" for k in missing)
        raise RuntimeError(f"缺少{group}配置环境变量：{detail}（参考 .env.example）")


def _demo() -> None:
    saved = {k: os.environ.get(k) for k in (*ENV_VARS, "TAX_AGENT_ENV")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        # 对账 .env.example：注释掉的变量（# TAX_AGENT_X=1）也算数，代码清单必须与它完全一致
        example_file = Path(__file__).resolve().parents[2] / ".env.example"
        example_keys: set[str] = set()
        for line in example_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().lstrip("#").strip()
            if "=" in line:
                key = line.split("=", 1)[0].strip()
                if key.isidentifier():
                    example_keys.add(key)
        code_keys = set(ENV_VARS)
        assert code_keys == example_keys, (
            f"代码有但 .env.example 没有：{code_keys - example_keys}；"
            f".env.example 有但代码没有：{example_keys - code_keys}"
        )

        # check("model")：三个变量齐全时不抛，缺一个时抛且消息里有那个变量名
        os.environ.update(TAX_AGENT_BASE_URL="x", TAX_AGENT_API_KEY="x", TAX_AGENT_MODEL="x")
        check("model")
        del os.environ["TAX_AGENT_API_KEY"]
        try:
            check("model")
            raise AssertionError("应当抛 RuntimeError")
        except RuntimeError as exc:
            assert "TAX_AGENT_API_KEY" in str(exc), exc

        # check("source")：local 不需要 TTC_BASE_URL，ttc_user 需要
        os.environ["TAX_AGENT_SOURCE"] = "local"
        check("source")
        os.environ["TAX_AGENT_SOURCE"] = "ttc_user"
        try:
            check("source")
            raise AssertionError("应当抛 RuntimeError")
        except RuntimeError as exc:
            assert "TTC_BASE_URL" in str(exc), exc

        # load_dotenv() 在 TAX_AGENT_ENV=prod 时不改动任何环境变量
        os.environ["TAX_AGENT_ENV"] = "prod"
        before = dict(os.environ)
        load_dotenv()
        assert os.environ == before, "TAX_AGENT_ENV=prod 时不该改动环境变量"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("config self-check ok")


if __name__ == "__main__":
    _demo()
