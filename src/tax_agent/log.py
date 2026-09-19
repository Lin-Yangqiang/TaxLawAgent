"""loguru 的 sink 配置。

单独起名 `log.py` 而非 `logging.py`：后者会跟 stdlib 的 `logging` 撞名，
`from tax_agent import logging` 在部分导入路径下会遮蔽标准库。

只有 `server.py` 和 `cli.py` 这两个入口调用 `setup()`；库模块（agent/tools/audit/
identity/sources）直接 `from loguru import logger` 用，不各自配置 sink，
否则多个模块各配一份会导致同一条日志被打印多次。
"""

from __future__ import annotations

import os
import sys
from contextvars import ContextVar

from loguru import logger

_configured = False

# HIS 平台约定的链路字段叫 tracer_id，与业界 trace_id 同义。
# 自己定义而不复用 pyxis.logger.logger.TRACER_ID_CTX：log.py 要能被 cli.py
# 这种没往 sys.path 插 assets/site-packages 的入口 import，不能依赖内网 SDK。
TRACE_ID: ContextVar[str] = ContextVar("tax_agent_trace_id", default="")


def setup() -> None:
    """配置 loguru 输出到 stderr，级别读 `TAX_AGENT_LOG_LEVEL`（默认 INFO）。

    幂等：重复调用（多个入口都 import 到同一份 sys.modules 缓存）不会叠加 sink，
    否则同一条日志会被打印 N 次。
    """
    global _configured
    if _configured:
        return
    _configured = True

    level = os.getenv("TAX_AGENT_LOG_LEVEL", "INFO").upper()
    # patcher 在每条记录的 extra 里补 trace 字段：这样 tools.py / ttc_client.py
    # 里现有的日志调用一行不用改，就自动带上当前请求的 trace_id。
    logger.configure(patcher=lambda record: record["extra"].update(trace=TRACE_ID.get()))
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss.SSS}</green> | <dim>{extra[trace]}</dim> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )


def _demo() -> None:
    setup()
    setup()  # 幂等：不应该重复添加 sink
    assert len(logger._core.handlers) == 1, logger._core.handlers

    # 必须比对格式化后的文本：loguru 的 sink 默认 catch=True，format 里的
    # {extra[trace]} 真的 KeyError 也只会往 stderr 打一条错误，调用方看不出异常。
    # 光"调一次 logger.info 没炸"证明不了 patcher 生效。
    captured: list[str] = []
    sink_id = logger.add(captured.append, format="{extra[trace]}|{message}")
    TRACE_ID.set("abc123")
    logger.info("有 trace")
    TRACE_ID.set("")
    logger.info("无 trace")
    logger.remove(sink_id)
    assert [line.strip() for line in captured] == ["abc123|有 trace", "|无 trace"], captured

    logger.info("log self-check ok")


if __name__ == "__main__":
    _demo()
