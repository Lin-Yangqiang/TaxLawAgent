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

from loguru import logger

_configured = False


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
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )


def _demo() -> None:
    setup()
    setup()  # 幂等：不应该重复添加 sink
    assert len(logger._core.handlers) == 1, logger._core.handlers
    logger.info("log self-check ok")


if __name__ == "__main__":
    _demo()
