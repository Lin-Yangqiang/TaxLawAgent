"""TTC 请求凭证：生产用户身份透传，本地开发用应用级静态凭证。

为什么用户身份走透传而不是自签 JWT：TTC 的 `x-jwt-ms-token` 由 IAM SDK 用远程公钥
验签，本地没有对应私钥，自签的 token 一定被拒；企业 IAM 真实签发给用户的 token
本来就在受信任的验签链路里，透传才是唯一走得通的路，见 CLAUDE.md 的鉴权约定。

应用级凭证（ApicProvider / ApigwProvider）的定位是本地开发拿真实数据，不是生产兜底：
它们没有用户身份，TTC 侧拿不到 `x-jwt-ms-token` 里的用户信息，维度权限无从生效，
只能调公有 API（queryTtcClauseDataList / extTaxClauseSearch），调不了私有接口
taxClauseSearch / queryTlpInfo。所以 UserTokenProvider 缺用户 token 时直接报错，
不能悄悄换成应用级凭证去查——那会让"以谁的名义查询"这件事变得不确定。
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from loguru import logger

# 直接 `python src/tax_agent/auth.py` 跑自检时 sys.path[0] 是本文件目录而非 src/
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tax_agent.identity import current  # noqa: E402


class AuthProvider(Protocol):
    def headers(self) -> dict[str, str]: ...


class UserTokenProvider:
    """把当前请求的用户 IAM token 原样透传给 TTC。生产路径唯一使用的凭证。"""

    def headers(self) -> dict[str, str]:
        """返回携带用户 token 的请求头。

        Returns:
            `{"x-jwt-ms-token": <token>}`。

        Raises:
            PermissionError: 当前上下文没有用户 token。
        """
        identity = current()
        if identity.token is None:
            logger.warning("请求无用户 token")
            raise PermissionError("当前请求没有用户 token")
        # 只记长度，token 原文绝不能进日志
        logger.info("透传用户 token 给 TTC，token len={}", len(identity.token))
        return {"x-jwt-ms-token": identity.token}


TOKEN_PATH = "/ApiCommonQuery/appToken/getRestAppDynamicToken"

# APIC 动态 token 服务端寿命约 1 小时，提前一分钟换，避免拿着刚过期的 token 发请求
TOKEN_TTL_SECONDS = 3540.0


def _post_token(url: str, body: dict[str, Any]) -> dict[str, Any]:
    """默认的换 token 传输层。

    Raises:
        RuntimeError: HTTP 状态码非 200。
    """
    # ponytail: verify=False 是内网自签证书的已知妥协，trust_env=False 是因为 HIS
    # 系统代理会 407 拦截内网请求。正式部署换成可信证书后应把 verify 打开。
    response = httpx.post(
        url,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=15.0,
        verify=False,
        trust_env=False,
    )
    if response.status_code != 200:
        raise RuntimeError(f"APIC 换 token 失败：HTTP {response.status_code}")
    return response.json()


class ApicProvider:
    """APIC 应用级凭证，仅本地开发用来调 TTC 公有 API 拿真实数据。

    没有用户身份，不能用于生产的用户请求路径——TTC 侧拿不到用户维度信息，
    维度权限无从生效。只能调 queryTtcClauseDataList / extTaxClauseSearch
    这类公有 API，调不了 taxClauseSearch / queryTlpInfo 私有接口。

    不走 `pyxis.authorization.his_authorization.get_dynamic_token`：它依赖
    `cachetools`，本地 venv 没装，模块顶层 import 会让整个 auth.py 导不进来。
    换 token 本身就十来行，自己实现比为它补一个依赖划算。

    Args:
        endpoint: APIC 服务根地址，换 token 的路径由类内部拼。
        app_id: 应用 ID。
        static_secret: 静态密钥，用来换取动态 token，绝不进日志。
        post: 可注入的传输层，签名 `post(url, body) -> dict`；自检用假实现替换，
            默认才真正发 HTTP 请求。与 TtcSource 的 post 参数同一用法。
    """

    def __init__(
        self,
        endpoint: str,
        app_id: str,
        static_secret: str,
        post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._app_id = app_id
        self._static_secret = static_secret
        self._post = post or _post_token
        self._token: str | None = None
        self._expires_at = 0.0

    def _fetch_token(self) -> str:
        """用静态密钥换一个动态 token。

        Returns:
            APIC 返回的 token 原值，已经是可直接作为 `Authorization` 头值的完整字符串。

        Raises:
            RuntimeError: 响应里没有 result。
        """
        # credential 是 base64(静态密钥)，不是 base64(appId:密钥)——内网实测的形状
        credential = base64.b64encode(self._static_secret.encode()).decode()
        payload = self._post(
            self._endpoint + TOKEN_PATH, {"appId": self._app_id, "credential": credential}
        )
        token = payload.get("result")
        if not token:
            raise RuntimeError("APIC 换 token 失败：响应里没有 result")
        return token

    def headers(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._expires_at:
            self._token = self._fetch_token()
            self._expires_at = time.monotonic() + TOKEN_TTL_SECONDS
            logger.info("已换取 APIC 动态 token，app_id={}", self._app_id)
        # 动态 token 原值就是完整的 Authorization 头值，不要再包一层 "Basic " +
        # base64(appId:token)。旧项目 docstring 那样写，但实测代码是直接透传原值。
        return {"Authorization": self._token}


class ApigwProvider:
    """APIGW 静态 AK/SK 凭证，仅本地开发用来调 TTC 公有 API 拿真实数据。

    与 ApicProvider 同样的用途边界：没有用户身份，不能用于生产的用户请求路径。
    静态 AK/SK 不需要像 APIC 那样先换动态 token，两个请求头直接够用。

    Args:
        hw_id: APIGW 分配的 AK（Access Key ID）。
        hw_appkey: APIGW 分配的 SK，绝不进日志。
    """

    def __init__(self, hw_id: str, hw_appkey: str) -> None:
        self._hw_id = hw_id
        self._hw_appkey = hw_appkey

    def headers(self) -> dict[str, str]:
        logger.info("使用 APIGW 应用级凭证（仅限本地开发调公有 API）")
        return {"X-HW-ID": self._hw_id, "X-HW-APPKEY": self._hw_appkey}


def _demo() -> None:
    from tax_agent.identity import Identity, bind

    bind(Identity("u1", "user_token", "sometoken"))
    assert UserTokenProvider().headers() == {"x-jwt-ms-token": "sometoken"}

    bind(Identity("local", "none", None))
    try:
        UserTokenProvider().headers()
        raise AssertionError("应当抛 PermissionError")
    except PermissionError:
        pass

    token_calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        token_calls.append((url, body))
        return {"result": "apic-dynamic-token-value"}

    apic = ApicProvider("https://apic.invalid", "app", "secret", post=fake_post)
    # token 原值直接做 Authorization 头，不再包 "Basic " + base64(appId:token)
    assert apic.headers() == {"Authorization": "apic-dynamic-token-value"}, apic.headers()
    assert token_calls[0][0] == "https://apic.invalid" + TOKEN_PATH, token_calls[0]
    # credential 是 base64(密钥) 而不是 base64(appId:密钥)
    assert token_calls[0][1] == {
        "appId": "app",
        "credential": base64.b64encode(b"secret").decode(),
    }, token_calls[0]
    # 第二次取头必须命中缓存，不再换 token
    apic.headers()
    assert len(token_calls) == 1, token_calls

    empty = ApicProvider("https://apic.invalid", "app", "secret", post=lambda u, b: {})
    try:
        empty.headers()
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError as exc:
        assert "没有 result" in str(exc), exc

    apigw_headers = ApigwProvider("hw-id-1", "hw-appkey-1").headers()
    assert apigw_headers == {"X-HW-ID": "hw-id-1", "X-HW-APPKEY": "hw-appkey-1"}, apigw_headers

    print("auth self-check ok")


if __name__ == "__main__":
    _demo()
