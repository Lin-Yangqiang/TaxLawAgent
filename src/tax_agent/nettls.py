"""内网 HTTP 客户端的 TLS 与代理配置，三处出站请求（TTC / APIC 换 token / 模型）共用。

单独成模块，是因为内网有两个彼此独立的障碍，而它们**不该被捆成一个开关**：

1. **自签证书**：内网服务的证书由公司内部 CA 签发，公网 CA 包里没有这个根证书。
   正解是把内网根 CA 喂给 httpx（`verify=<ca.pem>`），**不是关掉校验**。
2. **系统代理 407**：HIS 系统代理会拦截内网直连请求。解法是 `trust_env=False`
   让 httpx 忽略 `HTTP_PROXY` 等环境变量，与证书完全无关。

把两件事分开的实际意义：`TAX_AGENT_CA_BUNDLE` 指向内网根 CA 时，代理绕过照旧生效，
但 TLS 校验保留。只有在拿不到根 CA 时才退到 `verify=False`，且必须显式开
`TAX_AGENT_INSECURE_TLS=1`——默认不成立，避免"本地图方便"悄悄变成生产配置。

**为什么这件事值得单独一个模块**：`verify=False` 对 TTC 请求泄露的是查询内容，
对模型请求泄露的是 `api_key`（中间人可直接拿走凭证），性质不同但同样不该默认发生。
三个调用点各写一遍 `verify=False` 迟早漂移成不一致，集中在这里只有一处可审计。

除了配置本身，这里还提供一个已经套上这套配置的 `post`，供各 RegulationSource
实现直接复用，不用各自拼一遍 `**client_kwargs()`。
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger

CA_BUNDLE_ENV = "TAX_AGENT_CA_BUNDLE"
INSECURE_ENV = "TAX_AGENT_INSECURE_TLS"


def verify_option() -> str | bool:
    """httpx 的 `verify` 取值：内网根 CA 路径优先，其次显式降级，默认保持校验。

    Returns:
        CA 证书路径（字符串）、`False`（显式降级）或 `True`（默认，保持校验）。

    Raises:
        FileNotFoundError: `TAX_AGENT_CA_BUNDLE` 指向的文件不存在。配错路径时
            httpx 的报错很难懂，这里直接点出是哪个环境变量。
    """
    ca_bundle = os.getenv(CA_BUNDLE_ENV)
    if ca_bundle:
        if not os.path.isfile(ca_bundle):
            raise FileNotFoundError(f"{CA_BUNDLE_ENV} 指向的证书文件不存在：{ca_bundle}")
        return ca_bundle

    if os.getenv(INSECURE_ENV) == "1":
        # ponytail: 关闭证书校验换取内网自签证书下能跑通，中间人可解密流量——
        # 对模型请求意味着 api_key 可被截获。拿到内网根 CA 后改配 TAX_AGENT_CA_BUNDLE。
        logger.warning(
            "已关闭 TLS 证书校验（{}=1）：流量可被中间人解密，模型 api_key 有泄露风险。"
            "仅限内网联调，拿到内网根 CA 后请改配 {}",
            INSECURE_ENV,
            CA_BUNDLE_ENV,
        )
        return False

    return True


def trust_env() -> bool:
    """是否读 `HTTP_PROXY` 等代理环境变量。

    默认 `False`——内网 HIS 系统代理会 407 拦截内网直连。需要走代理时（比如访问
    外网端点）设 `TAX_AGENT_TRUST_ENV=1`。

    Returns:
        传给 httpx `trust_env` 的值。
    """
    return os.getenv("TAX_AGENT_TRUST_ENV") == "1"


def client_kwargs() -> dict[str, object]:
    """httpx 客户端的 TLS/代理参数，直接展开进 `httpx.post` 或 `httpx.Client`。"""
    return {"verify": verify_option(), "trust_env": trust_env()}


def post(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """套用本模块 TLS/代理配置的 `httpx.post`，供各 RegulationSource 实现共用。

    Args:
        url: 请求地址。
        body: JSON 请求体。
        headers: 请求头。
        timeout: 超时秒数。

    Returns:
        响应体解析后的 JSON。
    """
    return httpx.post(url, json=body, headers=headers, timeout=timeout, **client_kwargs()).json()


def _demo() -> None:
    import tempfile

    for key in (CA_BUNDLE_ENV, INSECURE_ENV, "TAX_AGENT_TRUST_ENV"):
        os.environ.pop(key, None)

    # 默认必须保持证书校验，且不读系统代理——"默认安全"是这个模块存在的理由
    assert verify_option() is True
    assert trust_env() is False
    assert client_kwargs() == {"verify": True, "trust_env": False}

    # 只有显式开 INSECURE 才降级；单独配 TRUST_ENV 不影响证书校验
    os.environ["TAX_AGENT_INSECURE_TLS"] = "1"
    assert verify_option() is False
    os.environ["TAX_AGENT_TRUST_ENV"] = "1"
    assert trust_env() is True
    del os.environ["TAX_AGENT_TRUST_ENV"]

    # CA 路径优先于 INSECURE：两个都配时保留校验，不能让降级开关盖掉正解
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as fh:
        ca_path = fh.name
    os.environ[CA_BUNDLE_ENV] = ca_path
    assert verify_option() == ca_path, verify_option()
    del os.environ[INSECURE_ENV]
    assert verify_option() == ca_path

    # 配错路径要指名是哪个环境变量，httpx 自己的报错看不出来
    os.environ[CA_BUNDLE_ENV] = ca_path + ".missing"
    try:
        verify_option()
        raise AssertionError("应当抛 FileNotFoundError")
    except FileNotFoundError as exc:
        assert CA_BUNDLE_ENV in str(exc), exc

    del os.environ[CA_BUNDLE_ENV]
    os.unlink(ca_path)

    # 参数名必须与 httpx 对得上，拼错了要在自检里暴露而不是运行时
    httpx.Client(**client_kwargs()).close()

    # 要防的是 post() 里传给 httpx.post 的那串关键字拼错。httpx.post 的签名就能回答，
    # 不用发真实请求；断言 post() 自己的参数名则什么都证明不了——那是拿代码跟自己对账。
    import inspect

    inspect.signature(httpx.post).bind("https://x", json={}, headers={}, timeout=1.0, **client_kwargs())

    print("nettls self-check ok")


if __name__ == "__main__":
    _demo()
