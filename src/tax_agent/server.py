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
from uuid import uuid4

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
from tax_agent.log import TRACE_ID  # noqa: E402

# 微服务名。pyxis 用它拼 openapi/docs 路由，内网网关也按这个前缀路由，
# 所以业务接口一并挂在同一前缀下，本地和内网的 URL 完全一致。
SERVICE_NAME = "/tax-agent"

# 错误码取自技术设计文档 §8.3。只列现在真会发生的，其余（IDEMPOTENCY_CONFLICT /
# MODEL_RATE_LIMITED 等）等有真实案例再加——猜一个码写进契约，前端会按它写分支。
ERROR_CODES = {
    PermissionError: "UNAUTHENTICATED",  # UserTokenProvider 缺用户 token 时抛
}


def _error(status: int, code: str, message: str, trace_id: str) -> JSONResponse:
    """统一的错误响应体：前端按 `code` 分支，`trace_id` 用来找日志和上游。"""
    return JSONResponse(
        {"code": code, "message": message, "trace_id": trace_id},
        status_code=status,
        headers={"X-TRACERID": trace_id},
    )


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
        # X-TRACERID 是 HIS 平台约定的链路头，网关有就用网关的，没有就我们生成。
        # 不装 pyxis 的 TracingMiddleware：它在 call_next 返回后立刻 reset contextvar
        # （middleware.py:26-29），而 SSE 的 body 是响应返回之后才流的，
        # event_stream() 里的日志那时已经取不到值了。
        trace_id = request.headers.get("x-tracerid") or uuid4().hex[:12]
        TRACE_ID.set(trace_id)

        raw_token = request.headers.get("x-jwt-ms-token")
        try:
            identity = parse_token(raw_token, request.headers.get("x-jalor-userAccount"))
        except ValueError as exc:
            logger.warning("401：token 解析失败 - {}", exc)
            return _error(401, "UNAUTHENTICATED", str(exc), trace_id)

        if body.session_id is None:
            session_id = session_manager.create_session(identity.isolate_key)
        elif not session_manager.validate_ownership(identity.isolate_key, body.session_id):
            # 会话不存在也归到这一支：404 会泄露"这个 session_id 存在但不属于你"
            logger.warning(
                "403：越权访问会话 - 请求者={} 目标 session_id={}",
                identity.isolate_key,
                body.session_id,
            )
            return _error(403, "ACCESS_DENIED", "无权访问该会话", trace_id)
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
            # 只放 thread_id：token 一旦进 config，二期换持久化 checkpointer 后会落盘
            config = {"configurable": {"thread_id": session_id}}
            try:
                async for msg, _meta in agent.astream(
                    {"messages": [{"role": "user", "content": body.question}]},
                    config=config,
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
                # 引用校验的证据集不能只看这次 HTTP 请求：同一 session 里追问时，模型常常
                # 引用上一轮已经真实检索过的条款做延伸解读，这不是编造，只是"本轮"这个口径
                # 划窄了。checkpointer 按 thread_id 存了整个会话的消息历史，取来做证据集，
                # 既不放松防编造（引用的号仍必须来自某次真实工具返回），也不再误伤合法的
                # 多轮解读。tool_names（下面"工具调用"展示）仍然只看本轮，两者用途不同。
                state = await agent.aget_state(config)
                session_tool_messages = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
                problems = audit_citations(answer, session_tool_messages)
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
                        "trace_id": trace_id,
                    }
                )
            except Exception as exc:
                # 响应头早就发出去了（200 + text/event-stream），HTTP 状态码这时改不了，
                # 只能在流里补一帧告诉前端出了什么事。不重新抛：抛出去只会让连接
                # 无声无息地断掉，前端看到的是答案停在半截。
                code = ERROR_CODES.get(type(exc), "UPSTREAM_UNAVAILABLE")
                logger.exception("流式回答失败：session_id={} code={}", session_id, code)
                # 截断：我们自己抛的异常消息都很短（errorCode/tracerId），但 httpx 和模型
                # SDK 抛的会把整个响应体拼进消息，里面可能回显 prompt——而 prompt 里有条款正文。
                # 排障靠 trace_id 和服务端那条带 traceback 的日志，前端不需要全文。
                yield _sse({"type": "error", "code": code, "message": str(exc)[:200], "trace_id": trace_id})
                return

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"X-TRACERID": trace_id})

    @app.get("/actuator/health")
    @app.get(f"{SERVICE_NAME}/health")
    async def health():
        """存活探针。

        只报进程活着 + 当前检索源配置，**不主动探活上游**：真去调模型和 TTC 会让
        探针变慢，还可能被 K8s 的高频探测打成对上游的压测。

        Returns:
            `{"status": "UP", "source": ...}`。
        """
        # ponytail: 只有进程存活 + 配置快照，等真有"进程活着但上游全挂"的事故再加依赖探活
        return {"status": "UP", "source": os.getenv("TAX_AGENT_SOURCE", "local")}

    return app


def _parse_sse(text: str) -> list[dict]:
    """把 SSE 响应体拆回事件字典列表。"""
    return [json.loads(block[len("data: ") :]) for block in text.strip().split("\n\n") if block.startswith("data: ")]


def _demo() -> None:
    """自检绝不调用真实模型：注入一个记录 config/identity 的 stub agent。"""
    import base64
    import types

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

        async def aget_state(self, config):
            # 真实 aget_state 返回整个会话的历史消息；这里固定一条即可，
            # 下面的用例都不依赖"检索过哪条"，只依赖"确实有过检索"
            return types.SimpleNamespace(
                values={"messages": [ToolMessage(content="AD-VAT-CN-00001", tool_call_id="1", name="search_regulation")]}
            )

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
    assert done["trace_id"] and r.headers["x-tracerid"] == done["trace_id"], done

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
    assert done["citation_problems"] == ["回答中没有任何条款引用，但本会话检索到过条款"], done

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
    assert r.json()["code"] == "ACCESS_DENIED", r.text

    # 4. 带网关头 x-jalor-userAccount 时，isolate_key 取网关头而非 token claim
    r = client.post(
        f"{SERVICE_NAME}/chat",
        json={"question": "你好"},
        headers={"x-jwt-ms-token": zhangsan_token, "x-jalor-userAccount": "wangwu"},
    )
    assert r.status_code == 200, r.text
    assert stub.captured_identities[-1].isolate_key == "wangwu"

    # 5. 跨轮引用：上一轮真检索过的条款，本轮凭会话上下文引用（没再调工具）
    #    不该被判"未经检索"——这是合法的延伸解读，不是编造
    class MultiTurnStubAgent:
        """按 thread_id 攒历史消息，模拟 checkpointer 的跨轮持久化。"""

        def __init__(self) -> None:
            self.history: dict[str, list] = {}

        async def astream(self, _input, config=None, stream_mode=None):
            turn = self.history.setdefault(config["configurable"]["thread_id"], [])
            if not turn:
                tm = ToolMessage(content="AD-VAT-CN-00001", tool_call_id="1", name="search_regulation")
                final = AIMessageChunk(content="零税率需备案[AD-VAT-CN-00001]")
            else:
                tm = None
                final = AIMessageChunk(content="境外消费同样满足[AD-VAT-CN-00001]")
            if tm is not None:
                turn.append(tm)
                yield tm, {}
            turn.append(final)
            yield final, {}

        async def aget_state(self, config):
            return types.SimpleNamespace(values={"messages": self.history.get(config["configurable"]["thread_id"], [])})

    multi = MultiTurnStubAgent()
    client2 = TestClient(create_app(agent=multi, session_manager=SessionManager(MemorySessionBinding())))
    r = client2.post(f"{SERVICE_NAME}/chat", json={"question": "零税率条件？"})
    sid = _parse_sse(r.text)[-1]["session_id"]
    r = client2.post(f"{SERVICE_NAME}/chat", json={"question": "境外消费也算吗？", "session_id": sid})
    done = _parse_sse(r.text)[-1]
    assert done["citation_problems"] == [], done

    # 6. 流式中途抛异常：连接不能无声断掉，末帧必须是 error 帧且状态码仍是 200
    #    （响应头在流开始前就发出去了，这时已经改不了状态码）
    class BoomAgent:
        async def astream(self, _input, config=None, stream_mode=None):
            yield AIMessageChunk(content="先吐一点……"), {}
            raise RuntimeError("上游 TTC 5xx")
            yield  # pragma: no cover - 让函数体是 async generator

    client_boom = TestClient(create_app(agent=BoomAgent(), session_manager=SessionManager(MemorySessionBinding())))
    r = client_boom.post(f"{SERVICE_NAME}/chat", json={"question": "你好"})
    assert r.status_code == 200, r.text
    last = _parse_sse(r.text)[-1]
    assert last["type"] == "error" and last["code"] and last["trace_id"], last

    # 6b. PermissionError 要映射到 UNAUTHENTICATED（ERROR_CODES 里注册过的类型）
    class ForbiddenAgent:
        async def astream(self, _input, config=None, stream_mode=None):
            raise PermissionError("缺用户 token")
            yield  # pragma: no cover

    client_forbidden = TestClient(
        create_app(agent=ForbiddenAgent(), session_manager=SessionManager(MemorySessionBinding()))
    )
    r = client_forbidden.post(f"{SERVICE_NAME}/chat", json={"question": "你好"})
    assert r.status_code == 200, r.text
    last = _parse_sse(r.text)[-1]
    assert last["type"] == "error" and last["code"] == "UNAUTHENTICATED", last

    # 7. 健康检查：两条路径都要通，不依赖任何真实 agent 调用
    assert client.get("/actuator/health").json()["status"] == "UP"
    assert client.get(f"{SERVICE_NAME}/health").json()["status"] == "UP"

    print("server self-check ok")


if __name__ == "__main__":
    _demo()
