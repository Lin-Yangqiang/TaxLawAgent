"""FastAPI 服务：POST /chat，SSE 流式返回。

身份链路：网关透传头 `x-jalor-userAccount`（权威）+ `x-jwt-ms-token` -> parse_token
-> bind() 进 contextvar；没有网关头时退回解析 token 里的 JWT claim。
用户 token 绝不进 LangGraph 的 config/state——传给 agent 的 config 里只有
thread_id，原因见 identity.py 顶部注释。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import ToolMessage
from loguru import logger
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
# 本地没有内网包，直接用仓库里的 SDK 源码；内网环境删除这两行
_VENDORED = ROOT / "assets" / "site-packages"
if _VENDORED.is_dir() and str(_VENDORED) not in sys.path:
    sys.path.insert(0, str(_VENDORED))

# 直接 `python src/tax_agent/server.py` 跑自检时 sys.path[0] 是本文件目录而非 src/
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eurekax.isolation.memory_session_binding import MemorySessionBinding  # noqa: E402
from eurekax.isolation.session_manager import SessionManager  # noqa: E402
from pyxis.app_factory import create_app as create_pyxis_app  # noqa: E402

from tax_agent.audit import audit_citations  # noqa: E402
from tax_agent.identity import bind, parse_token  # noqa: E402

# 微服务名。pyxis 用它拼 openapi/docs 路由，内网网关也按这个前缀路由，
# 所以业务接口一并挂在同一前缀下，本地和内网的 URL 完全一致。
SERVICE_NAME = "/tax-agent"


def _load_dotenv_for_dev() -> None:
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


class ChatRequest(BaseModel):
    """`POST /chat` 的请求体。

    Attributes:
        question: 用户本轮提问。
        session_id: 续聊时传上一轮返回的会话 ID；不传则新建会话。
    """

    question: str
    session_id: str | None = None


def _sse(payload: dict) -> bytes:
    """把一个事件编码成 SSE 帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def create_app(agent=None, session_manager=None) -> FastAPI:
    """装配 HTTP 服务。

    用 `pyxis.app_factory` 建 app——它只是个带标题和 docs 路由前缀的 FastAPI 构造函数，
    不拉配置中心也不装鉴权中间件，本地和内网跑同一份代码。

    Args:
        agent: 已编译的 LangGraph agent。默认现建一个（需要模型环境变量）；
            自检时注入 stub，避免真调模型。
        session_manager: 会话绑定管理器。默认内存实现，P2 换 OpenGaussSessionBinding。

    Returns:
        可交给 uvicorn 的 FastAPI 实例。
    """
    from tax_agent.log import setup

    setup()  # uvicorn 用 --factory 调这个函数起服务，__main__ 只用于自检，日志配置放这里才覆盖真实入口
    _load_dotenv_for_dev()  # 必须在 build_agent() 之前：模型三件套从环境变量读
    if agent is None:
        from tax_agent.agent import build_agent

        agent = build_agent()
    session_manager = session_manager or SessionManager(MemorySessionBinding())

    app = create_pyxis_app(service_name=SERVICE_NAME)

    @app.post(f"{SERVICE_NAME}/chat")
    async def chat(body: ChatRequest, request: Request):
        """接一轮提问，SSE 流式吐回答，末帧带会话 ID 与引用校验结果。"""
        raw_token = request.headers.get("x-jwt-ms-token")
        try:
            identity = parse_token(raw_token, request.headers.get("x-jalor-userAccount"))
        except ValueError as exc:
            logger.warning("401：token 解析失败 - {}", exc)
            return JSONResponse({"detail": str(exc)}, status_code=401)

        if body.session_id is None:
            session_id = session_manager.create_session(identity.isolate_key)
        elif not session_manager.validate_ownership(identity.isolate_key, body.session_id):
            # 会话不存在也归到这一支：404 会泄露"这个 session_id 存在但不属于你"
            logger.warning(
                "403：越权访问会话 - 请求者={} 目标 session_id={}",
                identity.isolate_key,
                body.session_id,
            )
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        else:
            session_id = body.session_id

        bind(identity)
        logger.info(
            "请求进入：session_id={} auth_mode={} 问题={!r}",
            session_id,
            identity.auth_mode,
            body.question,
        )

        async def event_stream():
            """边流式吐 token 边攒完整答案，末尾就地做引用校验，省一次模型调用。"""
            started = time.perf_counter()
            tool_messages: list[ToolMessage] = []
            answer_parts: list[str] = []
            async for msg, _meta in agent.astream(
                {"messages": [{"role": "user", "content": body.question}]},
                # 只放 thread_id：token 一旦进 config，二期换持久化 checkpointer 后会落盘
                config={"configurable": {"thread_id": session_id}},
                stream_mode="messages",
            ):
                if isinstance(msg, ToolMessage):
                    tool_messages.append(msg)
                    # stream_mode="messages" 会吐出 ReAct 循环里**每一轮**的模型输出，包括
                    # "两个检索均无结果，换个关键词再试" 这类中间独白。它们不是答案：混进
                    # answer 会污染 audit_citations——中间轮提到过的条款号能把一个终答里
                    # 其实没引用的回答"洗"成合格。清掉之后 answer 只剩最后一次工具返回之后
                    # 的文本，也就是终答。独白照旧逐字流给用户，前端用这个 tool 帧分段显示。
                    answer_parts.clear()
                    yield _sse({"type": "tool", "name": msg.name})
                elif msg.content:
                    answer_parts.append(msg.content)
                    yield _sse({"type": "token", "text": msg.content})

            answer = "".join(answer_parts)
            problems = audit_citations(answer, tool_messages)
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            tool_names = [m.name for m in tool_messages]
            logger.info(
                "一轮结束：session_id={} 耗时={}ms 工具调用={} 引用校验={}",
                session_id,
                elapsed_ms,
                tool_names,
                "通过" if not problems else "不通过",
            )
            if problems:
                logger.warning("引用校验不通过：session_id={} 问题={}", session_id, problems)
            yield _sse(
                {
                    "type": "done",
                    "session_id": session_id,
                    "auth_mode": identity.auth_mode,
                    "tool_calls": tool_names,
                    "citation_problems": problems,
                }
            )

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


def _parse_sse(text: str) -> list[dict]:
    """把 SSE 响应体拆回事件字典列表。"""
    return [json.loads(block[len("data: ") :]) for block in text.strip().split("\n\n") if block.startswith("data: ")]


def _demo() -> None:
    """自检绝不调用真实模型：注入一个记录 config/identity 的 stub agent。"""
    import base64

    from fastapi.testclient import TestClient
    from langchain_core.messages import AIMessageChunk

    from tax_agent.identity import current

    class StubAgent:
        def __init__(self) -> None:
            self.captured_configs: list[dict] = []
            self.captured_identities: list = []

        # 按真实 ReAct 序列出牌：中间独白 -> 工具返回 -> 终答。独白里故意带一条合法引用，
        # 终答故意不带引用且超过 80 字——这正是最危险的那种形状：独白里的条款号一旦被拼进
        # answer，就会把一个毫无依据的终答"洗"成引用校验通过
        NARRATION = "先检索一下，[AD-VAT-CN-00001] 看起来相关，再确认一下。"
        FINAL = "很抱歉，" + "法规库中没有查到可以支撑这个问题的现行条款，因此无法给出带条款依据的解答。" * 4

        async def astream(self, _input, config=None, stream_mode=None):
            self.captured_configs.append(config)
            self.captured_identities.append(current())
            yield AIMessageChunk(content=self.NARRATION), {}
            yield ToolMessage(content="AD-VAT-CN-00001", tool_call_id="1", name="search_regulation"), {}
            yield AIMessageChunk(content=self.FINAL), {}

    def make_token(payload: dict) -> str:
        segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJub25lIn0.{segment}.sig"

    stub = StubAgent()
    app = create_app(agent=stub, session_manager=SessionManager(MemorySessionBinding()))
    client = TestClient(app)

    # 1. 无 token 路径
    r = client.post(f"{SERVICE_NAME}/chat", json={"question": "你好"})
    assert r.status_code == 200, r.text
    done = _parse_sse(r.text)[-1]
    assert done["type"] == "done" and done["auth_mode"] == "none" and done["session_id"]

    # 2. 有 token 路径
    zhangsan_token = make_token({"uid": "zhangsan"})
    r = client.post(f"{SERVICE_NAME}/chat", json={"question": "你好"}, headers={"x-jwt-ms-token": zhangsan_token})
    assert r.status_code == 200, r.text
    done = _parse_sse(r.text)[-1]
    assert done["auth_mode"] == "user_token", done
    zhangsan_session_id = done["session_id"]
    assert stub.captured_identities[-1].isolate_key == "zhangsan"

    # 2b. 中间独白不算答案：照旧流给用户，但不进引用校验
    events = _parse_sse(r.text)
    assert {"type": "tool", "name": "search_regulation"} in events, events
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert StubAgent.NARRATION in streamed, streamed  # 独白该看见就看见，不是吞掉
    # 独白里那条 [AD-VAT-CN-00001] 不能替终答背书：answer 只该有终答，于是必须报"没有引用"
    assert done["citation_problems"] == ["回答中没有任何条款引用"], done

    # token 不进图：检查上面 zhangsan 那次调用留下的 config
    zhangsan_config = stub.captured_configs[-1]
    assert zhangsan_token not in repr(zhangsan_config), zhangsan_config
    assert set(zhangsan_config["configurable"].keys()) == {"thread_id"}, zhangsan_config

    # 3. 越权必须 403：用 lisi 的 token 带 zhangsan 的 session_id
    lisi_token = make_token({"uid": "lisi"})
    r = client.post(
        f"{SERVICE_NAME}/chat",
        json={"question": "你好", "session_id": zhangsan_session_id},
        headers={"x-jwt-ms-token": lisi_token},
    )
    assert r.status_code == 403, r.text

    # 4. 带网关头 x-jalor-userAccount 时，isolate_key 取网关头而非 token claim
    r = client.post(
        f"{SERVICE_NAME}/chat",
        json={"question": "你好"},
        headers={"x-jwt-ms-token": zhangsan_token, "x-jalor-userAccount": "wangwu"},
    )
    assert r.status_code == 200, r.text
    assert stub.captured_identities[-1].isolate_key == "wangwu"

    print("server self-check ok")


if __name__ == "__main__":
    _demo()
