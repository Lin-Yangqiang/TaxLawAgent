"""用户身份的接收与传递。

身份只走 contextvars，绝不进 LangGraph 的 state 或 config——checkpointer 二期换成
OpenGauss 持久化后，写进图状态的东西会落盘，并在每次恢复会话时回灌给模型，
token 会永久留在库里。

身份来源优先级：TTC 侧认的是网关透传头 `x-jalor-userAccount`（对应
`RequestContext.getCurrent().getUser().getUserAccount()`），这是权威来源；
解析 JWT claim 只是本地/直连没有网关头时的退路，两条路径解析出的可能不是同一个人，
不能用于会话归属校验。
"""

from __future__ import annotations

import base64
import json
from contextvars import ContextVar
from typing import NamedTuple

from loguru import logger

# 退路专用：没有网关头 x-jalor-userAccount 时，从 JWT claim 里猜身份。
# claim 名待联调确认，按 uid -> userAccount -> sub 顺序取第一个非空。
_CLAIM_KEYS = ("uid", "userAccount", "sub")


def _token_fingerprint(raw: str) -> str:
    """token 出问题时用来定位是"哪个 token"，绝不能是原文本身。"""
    return f"len={len(raw)} prefix={raw[:4]!r}"


class Identity(NamedTuple):
    isolate_key: str
    auth_mode: str  # "none" | "user_token"
    token: str | None


def _decode_segment(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def parse_token(raw: str | None, user_account: str | None = None) -> Identity:
    """解析用户身份，优先取网关透传头，其次退回 JWT claim 解析。

    Args:
        raw: 原始 token（`x-jwt-ms-token`），只做透传/日志指纹，不再用来解身份。
        user_account: 网关头 `x-jalor-userAccount` 的值。非空时直接采用——网关已经
            解析并验签过了，这条路上 token 对我们只是个透传的不透明字符串，没必要
            再解一遍 JWT；为空时才走 claim 解析（本地/直连没有网关头的退路）。

    Returns:
        解析出的 Identity。
    """
    if not raw:
        return Identity("local", "none", None)

    if user_account:
        return Identity(user_account, "user_token", raw)

    segments = raw.split(".")
    if len(segments) != 3:
        logger.warning("token 解析失败：段数不对（{}），{}", len(segments), _token_fingerprint(raw))
        raise ValueError(f"token 段数不对：{len(segments)}")

    try:
        payload = _decode_segment(segments[1])
    except Exception as exc:
        logger.warning("token 解析失败：payload 解码异常（{}），{}", exc, _token_fingerprint(raw))
        raise ValueError(f"token payload 解码失败：{exc}") from exc

    for key in _CLAIM_KEYS:
        value = payload.get(key)
        if value:
            return Identity(str(value), "user_token", raw)

    logger.warning("token 解析失败：payload 中没有可用的用户标识（尝试过 {}），{}", _CLAIM_KEYS, _token_fingerprint(raw))
    raise ValueError(f"token payload 中没有可用的用户标识（尝试过 {_CLAIM_KEYS}）")


_current: ContextVar[Identity] = ContextVar("tax_agent_identity", default=Identity("local", "none", None))


def bind(identity: Identity) -> None:
    _current.set(identity)


def current() -> Identity:
    return _current.get()


def _demo() -> None:
    assert parse_token(None) == Identity("local", "none", None)
    assert parse_token("") == Identity("local", "none", None)

    def make_token(payload: dict) -> str:
        segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJub25lIn0.{segment}.sig"

    token = make_token({"uid": "zhangsan"})
    identity = parse_token(token)
    assert identity == Identity("zhangsan", "user_token", token), identity

    # uid 优先于 userAccount 和 sub
    token2 = make_token({"userAccount": "lisi", "sub": "wangwu"})
    assert parse_token(token2).isolate_key == "lisi"

    token3 = make_token({"sub": "wangwu"})
    assert parse_token(token3).isolate_key == "wangwu"

    for bad in ("only.two", "not-a-jwt", "a.b.c", make_token({})):
        try:
            parse_token(bad)
            raise AssertionError(f"应当抛 ValueError：{bad}")
        except ValueError:
            pass

    # 有网关头时以网关头为准，跳过 JWT 解码：uid 与网关头不同也要取网关头
    gateway_token = make_token({"uid": "zhangsan"})
    gateway_identity = parse_token(gateway_token, "lisi")
    assert gateway_identity == Identity("lisi", "user_token", gateway_token), gateway_identity

    # 有网关头时，格式不合法的 token 也不报错——网关已验签，我们不解它
    assert parse_token("not-a-jwt", "lisi") == Identity("lisi", "user_token", "not-a-jwt")

    bind(identity)
    assert current() == identity
    print("identity self-check ok")


if __name__ == "__main__":
    _demo()
