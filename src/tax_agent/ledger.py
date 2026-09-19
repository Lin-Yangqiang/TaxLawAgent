"""运行账本：对应技术设计文档 §7.3 的 `t_agent_run` / `t_agent_tool_call` 两张自建表。

这两张表记的是"一次运行的账"——状态、模型版本、质量信号、耗时、工具调用序列，
不是业务数据（法规检索结果不进这里，业务证据链路仍由 audit.py 管）。之所以单独
成模块而不是散落在 server.py / tools.py：写库这件事要在两个完全不相关的地方
触发（`chat()` 里开始一次 run，`@tool` 每次被调用时记一条 tool_call），需要一个
公共的中转点把两边缝起来，ContextVar 就是这个缝合点。

隐私口径（硬约束，见 CLAUDE.md）：这张账本只存 hash 和计数，**不存用户提问原文、
不存工具入参原文、不存条款正文**。`input_hash` / `args_hash` 都是内容摘要，任何
需要还原"用户到底问了什么"的排障，只能靠这个 hash 去别处（如果有的话）反查，
账本本身绝不作为可读文本的存档。

覆盖范围：只记我们自己的两个 `@tool`。deepagents 自带的 `read_file`（模型读技能
定义用的）不经过 `instrument`，所以 SSE done 帧里的 `tool_calls` 会比账本多出它——
这是有意的，账本要回答的是"检索了几次、召回几条证据"，读自己的技能文件不是检索。
# ponytail: 不记框架自带工具，将来要算完整的模型调用成本时，改成在 ToolNode 层面拦
"""

from __future__ import annotations

import functools
import json
import os
import sys
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

# 直接 `python src/tax_agent/ledger.py` 跑自检时 sys.path[0] 是本文件目录而非 src/
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tax_agent import persistence  # noqa: E402
from tax_agent.sources import content_hash  # noqa: E402

RUN_TYPE = "CHAT"


@dataclass
class ToolCallRecord:
    """一次工具调用的账，对应 `t_agent_tool_call` 一行。"""

    tool_call_id: str
    tool_name: str
    args_hash: str
    status: str
    latency_ms: int
    evidence_count: int
    error_code: str | None = None


@dataclass
class RunRecord:
    """一次 `/chat` 请求的账，对应 `t_agent_run` 一行。"""

    run_id: str
    thread_id: str
    actor_id: str
    trace_id: str
    started_at: datetime
    run_type: str = RUN_TYPE
    # 我们没有租户概念，这个字段永远是 None——不许为了填满这一列编一个值进去
    tenant_id: str | None = None
    status: str = "RUNNING"
    model_name: str | None = None
    prompt_version: str | None = None
    input_hash: str = ""
    usage_json: dict | None = None
    quality_json: dict | None = None
    finished_at: datetime | None = None
    latency_ms: int | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)


# 默认必须是 None 而不是 []：可变默认值在 ContextVar 场景下不是"每个 context 一份"，
# 而是所有从未 set() 过的 context 共用同一个 list 对象，并发请求之间的工具调用会互相串。
_TOOL_CALLS: ContextVar[list[ToolCallRecord] | None] = ContextVar("tax_agent_tool_calls", default=None)


@lru_cache(maxsize=1)
def prompt_version() -> str:
    """系统提示词的版本号：内容 hash，免去一张手工维护的版本注册表。

    Returns:
        `SYSTEM_PROMPT` 的 content_hash。

    在函数体内惰性 import agent.py：那个模块会拉起 deepagents 整条依赖链，
    ledger 只是记账，模块顶层不该背这个重量级依赖。
    """
    from tax_agent.agent import SYSTEM_PROMPT

    return content_hash(SYSTEM_PROMPT)


def begin_run(thread_id: str, actor_id: str, trace_id: str, question: str) -> RunRecord:
    """开始记一次运行的账，同时把工具调用收集器挂到当前 context 上。

    Args:
        thread_id: 会话 ID（LangGraph 的 thread_id）。
        actor_id: 发起者标识，取 `Identity.isolate_key`。
        trace_id: 链路 ID，用来跟日志和 done 帧对齐。
        question: 用户本轮提问，**只取其 hash**，不存原文。

    Returns:
        新建的 RunRecord；调用方后续传给 `finish_run` 收尾。
    """
    _TOOL_CALLS.set([])
    return RunRecord(
        run_id=uuid.uuid4().hex,
        thread_id=thread_id,
        actor_id=actor_id,
        trace_id=trace_id,
        started_at=datetime.now(timezone.utc),
        input_hash=content_hash(question),
        model_name=os.getenv("TAX_AGENT_MODEL"),
        prompt_version=prompt_version(),
    )


def record_tool_call(rec: ToolCallRecord) -> None:
    """把一条工具调用记录挂到当前 run 上。

    Args:
        rec: 待记录的调用。

    没有 run 上下文（`_TOOL_CALLS.get()` 是 None）时静默丢弃：工具在请求之外
    被调用是正常场景（模块自检、evals），这时没有 run 可挂，不该报错也不该
    凭空造一个 run 出来。
    """
    calls = _TOOL_CALLS.get()
    if calls is None:
        return
    calls.append(rec)


def finish_run(
    run: RunRecord,
    status: str,
    usage: dict | None = None,
    quality: dict | None = None,
) -> RunRecord:
    """收尾一次运行：搬运工具调用、填时间字段、落一条日志、尝试写库。

    Args:
        run: `begin_run` 返回的记录。
        status: "SUCCEEDED" / "FAILED"。
        usage: token 用量，拿不到就传 None，不要编。
        quality: 质量信号（如引用校验结果、错误码）。

    Returns:
        填好字段的 run（原地修改并返回，方便调用方链式使用）。
    """
    run.tool_calls = _TOOL_CALLS.get() or []
    run.status = status
    run.usage_json = usage
    run.quality_json = quality
    run.finished_at = datetime.now(timezone.utc)
    run.latency_ms = round((run.finished_at - run.started_at).total_seconds() * 1000)

    # 只含账本字段本身，不含 input_hash 以外的任何输入信息——这条日志本身也要过隐私口径。
    # usage 一并打出来：没配数据库时这是唯一能看到端点到底回没回 token 用量的地方
    logger.info(
        "运行结束：run_id={} thread_id={} actor_id={} status={} latency_ms={} 工具调用={} usage={} trace_id={}",
        run.run_id,
        run.thread_id,
        run.actor_id,
        run.status,
        run.latency_ms,
        len(run.tool_calls),
        run.usage_json,
        run.trace_id,
    )
    _write(run)
    return run


def _write(run: RunRecord) -> None:
    """把 run 写进 `t_agent_run` / `t_agent_tool_call`。

    未启用数据库时直接跳过。写库失败只记日志不上抛：账本是可观测性副产品，
    不是产品功能，写不进去不能把用户已经拿到的回答搞挂。
    """
    if not persistence.enabled():
        return

    import sqlalchemy

    schema = os.getenv("TAX_AGENT_DB_SCHEMA")
    run_table = f"{schema}.t_agent_run" if schema else "t_agent_run"
    call_table = f"{schema}.t_agent_tool_call" if schema else "t_agent_tool_call"

    try:
        with persistence.sqlalchemy_pool().context_session(commit=True) as session:
            session.execute(
                sqlalchemy.text(
                    f"""
                    INSERT INTO {run_table}
                        (run_id, thread_id, tenant_id, actor_id, run_type, status,
                         model_name, prompt_version, input_hash, usage_json, quality_json,
                         trace_id, started_at, finished_at, latency_ms)
                    VALUES
                        (:run_id, :thread_id, :tenant_id, :actor_id, :run_type, :status,
                         :model_name, :prompt_version, :input_hash, :usage_json, :quality_json,
                         :trace_id, :started_at, :finished_at, :latency_ms)
                    """
                ),
                {
                    "run_id": run.run_id,
                    "thread_id": run.thread_id,
                    "tenant_id": run.tenant_id,
                    "actor_id": run.actor_id,
                    "run_type": run.run_type,
                    "status": run.status,
                    "model_name": run.model_name,
                    "prompt_version": run.prompt_version,
                    "input_hash": run.input_hash,
                    # ponytail: 存 TEXT 不用 jsonb，OpenGauss 的 jsonb 支持要按版本确认；
                    # 确认支持后再改列类型
                    "usage_json": json.dumps(run.usage_json, ensure_ascii=False) if run.usage_json else None,
                    "quality_json": json.dumps(run.quality_json, ensure_ascii=False) if run.quality_json else None,
                    "trace_id": run.trace_id,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "latency_ms": run.latency_ms,
                },
            )
            for seq, call in enumerate(run.tool_calls):
                session.execute(
                    sqlalchemy.text(
                        f"""
                        INSERT INTO {call_table}
                            (run_id, seq, tool_call_id, tool_name, args_hash, status,
                             latency_ms, evidence_count, error_code, trace_id)
                        VALUES
                            (:run_id, :seq, :tool_call_id, :tool_name, :args_hash, :status,
                             :latency_ms, :evidence_count, :error_code, :trace_id)
                        """
                    ),
                    {
                        "run_id": run.run_id,
                        "seq": seq,
                        "tool_call_id": call.tool_call_id,
                        "tool_name": call.tool_name,
                        "args_hash": call.args_hash,
                        "status": call.status,
                        "latency_ms": call.latency_ms,
                        "evidence_count": call.evidence_count,
                        "error_code": call.error_code,
                        "trace_id": run.trace_id,
                    },
                )
    except Exception:
        # ponytail: 同步写库阻塞事件循环，单条 INSERT 可接受；成为瓶颈时改
        # asyncio.to_thread 或异步队列
        logger.exception("运行账本写库失败：run_id={}", run.run_id)


def _args_hash(kwargs: dict[str, Any]) -> str:
    """工具入参的摘要，不是原文——入参里可能有用户的检索词（`query`）。"""
    payload = {k: v for k, v in kwargs.items() if k != "tool_call_id"}
    return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str))


def _evidence_count(result: Any) -> int:
    """从工具返回值里数出"这次调用给了几条证据"，用于账本的 evidence_count 列。"""
    if not isinstance(result, dict):
        return 0
    if "hits" in result:
        return len(result["hits"])
    return 1 if result.get("status") == "OK" else 0


def instrument(fn):
    """给 `@tool` 函数记账的装饰器，**必须套在 `@tool` 内层**（`@tool` 在外）。

    顺序反了会记不到账：`@tool` 会把函数包成 `BaseTool` 实例，`instrument`
    再套在外面就是在包工具对象而不是包被调用的函数体。

    工具的返回值形如 `{"status": "OK"/"NO_EVIDENCE"/"NOT_FOUND"/"INVALID_AS_OF"/
    "UNSUPPORTED_AS_OF", ...}`，这套状态词汇已经是工具协议的一部分，直接复用，
    不再造一张"内部状态 -> 账本状态"的映射表。只有真的抛出异常时才记 "ERROR"。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        status, error_code, result = "ERROR", None, None
        try:
            result = fn(*args, **kwargs)
            status = result.get("status", "OK") if isinstance(result, dict) else "OK"
            return result
        except Exception as exc:
            error_code = type(exc).__name__
            raise
        finally:
            record_tool_call(
                ToolCallRecord(
                    tool_call_id=kwargs.get("tool_call_id") or "",
                    tool_name=fn.__name__,
                    args_hash=_args_hash(kwargs),
                    status=status,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    evidence_count=_evidence_count(result),
                    error_code=error_code,
                )
            )

    return wrapper


def _demo() -> None:
    # 1. 没有 run 上下文时调用 record_tool_call：不抛异常，也不留下任何东西
    #    （_TOOL_CALLS 默认是 None，这里确认真的是 None，不是上一个用例漏清的 []）
    assert _TOOL_CALLS.get() is None
    record_tool_call(
        ToolCallRecord("x", "noop", "h", "OK", 0, 0)
    )
    assert _TOOL_CALLS.get() is None

    # 2. 正常一轮：instrument 包一个假工具函数，记下 1 条调用
    def fake_search(query: str, tool_call_id: str = None) -> dict:
        return {"status": "OK", "hits": [1, 2, 3]}

    instrumented = instrument(fake_search)
    run = begin_run("thread-1", "zhangsan", "trace-1", "跨境研发服务零税率")
    assert run.tenant_id is None
    instrumented(query="x", tool_call_id="call-1")
    finish_run(run, "SUCCEEDED")
    assert len(run.tool_calls) == 1, run.tool_calls
    call = run.tool_calls[0]
    assert call.evidence_count == 3, call
    assert call.status == "OK", call
    assert call.latency_ms >= 0, call
    assert call.tool_call_id == "call-1", call

    # 3. 假函数抛异常：status 落 ERROR，error_code 是异常类名，异常本身要向上传播
    def fake_boom(tool_call_id: str = None) -> dict:
        raise RuntimeError("上游炸了")

    instrumented_boom = instrument(fake_boom)
    run2 = begin_run("thread-2", "lisi", "trace-2", "常设机构认定")
    try:
        instrumented_boom(tool_call_id="call-2")
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError:
        pass
    finish_run(run2, "FAILED")
    assert run2.tool_calls[0].status == "ERROR", run2.tool_calls
    assert run2.tool_calls[0].error_code == "RuntimeError", run2.tool_calls

    # 4. NO_EVIDENCE：hits 是空列表，evidence_count 跟着是 0
    def fake_no_evidence(tool_call_id: str = None) -> dict:
        return {"status": "NO_EVIDENCE", "hits": []}

    run3 = begin_run("thread-3", "wangwu", "trace-3", "遗产税起征点")
    instrument(fake_no_evidence)(tool_call_id="call-3")
    finish_run(run3, "SUCCEEDED")
    assert run3.tool_calls[0].status == "NO_EVIDENCE", run3.tool_calls
    assert run3.tool_calls[0].evidence_count == 0, run3.tool_calls

    # 5. fetch_clause 的形状：命中但没有 hits 键，一条命中就是 1 条证据
    def fake_fetch(tool_call_id: str = None) -> dict:
        return {"status": "OK", "content": "条文正文……"}

    run4 = begin_run("thread-4", "zhaoliu", "trace-4", "AD-VAT-CN-00001")
    instrument(fake_fetch)(tool_call_id="call-4")
    finish_run(run4, "SUCCEEDED")
    assert run4.tool_calls[0].evidence_count == 1, run4.tool_calls

    # 6. 隐私断言：args_hash 不能让人从结果反推出原文，且与键序无关
    h1 = _args_hash({"query": "增值税零税率", "tool_call_id": "x"})
    h2 = _args_hash({"tool_call_id": "x", "query": "增值税零税率"})
    assert h1 == h2, (h1, h2)
    assert "增值税" not in h1 and "x" not in h1, h1

    # 7. enabled() 为 False（本地默认，5 个 DB_* 变量都没配）时 finish_run 不碰数据库，
    #    也就是说不抛任何异常
    run5 = begin_run("thread-5", "sunqi", "trace-5", "随便问点什么")
    finish_run(run5, "SUCCEEDED")

    print("ledger self-check ok")


if __name__ == "__main__":
    _demo()
