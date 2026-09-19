"""会话绑定和 checkpointer 用内存实现还是 OpenGauss，这个决定收在这一处。

叫"选择层"而不是"持久化层"：两个 OpenGauss 实现（`OpenGaussSessionBinding` /
`OpenGaussAsyncSaver`）本身在 eurekax SDK 里已经写好了，这个模块不重新实现
任何数据库读写，只做两件事——判断该不该用它们、以及怎么把连接参数拼给它们。

OpenGauss 分支的 import 全部写在函数体里（惰性 import）：本地 venv 没装
`psycopg` / `langgraph.checkpoint.postgres`，这两个包是 `OpenGaussAsyncSaver`
的依赖。模块顶层 import 会让本地 `python persistence.py` 自检直接
ModuleNotFoundError，而本地分支其实永远走不到 OpenGauss 那一支。

`enabled()` 是全模块最重要的一条分支：5 个数据库环境变量必须"要么一个都没配
（本地开发，内存模式），要么全配齐（生产，OpenGauss 模式）"，配了一半必须炸。
不这样做的后果是生产上悄悄退回内存实现——会话丢失、多实例之间互相看不见
对方的会话，而且从表现上完全看不出来，只会偶发"上一轮说过的话它不记得"，
排查成本远高于启动时报错。
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
# 本地没有内网包，直接用仓库里的 SDK 源码；内网环境删除这两行
_VENDORED = ROOT / "assets" / "site-packages"
if _VENDORED.is_dir() and str(_VENDORED) not in sys.path:
    sys.path.insert(0, str(_VENDORED))

# 直接 `python src/tax_agent/persistence.py` 跑自检时 sys.path[0] 是本文件目录而非 src/
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eurekax.isolation.memory_session_binding import MemorySessionBinding  # noqa: E402
from loguru import logger  # noqa: E402
from pyxis.rdb.sqlalchemy_pool import SQLAlchemyPool  # noqa: E402

DB_VARS = (
    "TAX_AGENT_DB_HOST",
    "TAX_AGENT_DB_PORT",
    "TAX_AGENT_DB_USER",
    "TAX_AGENT_DB_PASSWORD",
    "TAX_AGENT_DB_NAME",
)

# 模块级单例：连接池要跨请求复用，也要能被显式释放。不用 lru_cache——
# 它会把返回值钉死在缓存里，连接池想释放（比如测试之间重建）都释放不掉。
_pool: SQLAlchemyPool | None = None


def enabled() -> bool:
    """判断该用 OpenGauss 还是内存实现。

    Returns:
        5 个数据库环境变量全部非空 -> True；一个都没配 -> False。

    Raises:
        RuntimeError: 配了 1~4 个（配了一半）。消息里列出缺的是哪几个，
            绝不允许静默退回内存——那样生产会悄悄丢会话且无法观测。
    """
    present = [v for v in DB_VARS if os.getenv(v)]
    if not present:
        return False
    missing = [v for v in DB_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"数据库环境变量配了一部分，缺：{'、'.join(missing)}（要么都不配用内存，要么全配齐）")
    return True


def sqlalchemy_pool() -> SQLAlchemyPool:
    """模块级单例连接池，供 `OpenGaussSessionBinding` 复用。

    Raises:
        RuntimeError: `enabled()` 为 False 时不该被调用。
    """
    global _pool
    if not enabled():
        raise RuntimeError("数据库未启用，不该调用 sqlalchemy_pool()")
    if _pool is None:
        _pool = SQLAlchemyPool(
            driver_name="postgresql+psycopg2",  # OpenGauss 走 PG 协议
            user=os.environ["TAX_AGENT_DB_USER"],
            password=os.environ["TAX_AGENT_DB_PASSWORD"],
            host=os.environ["TAX_AGENT_DB_HOST"],
            port=os.environ["TAX_AGENT_DB_PORT"],
            database=os.environ["TAX_AGENT_DB_NAME"],
            # SDK 的坑（pyxis/rdb/sqlalchemy_pool.py 第一行 connect_args.pop(...)）：
            # 不传这个参数、默认值是 None 时会直接 AttributeError，不是可以省略的冗余参数
            connect_args={},
        )
    return _pool


def build_session_binding():
    """返回 `SessionManager` 需要的 binding 对象：内存或 OpenGauss，由 `enabled()` 决定。"""
    if not enabled():
        logger.info("会话绑定：内存实现（MemorySessionBinding，未配置数据库环境变量）")
        return MemorySessionBinding()

    from eurekax.isolation.opengauss_session_binding import OpenGaussSessionBinding

    logger.info("会话绑定：OpenGauss 实现（host={} db={}）", os.environ["TAX_AGENT_DB_HOST"], os.environ["TAX_AGENT_DB_NAME"])
    return OpenGaussSessionBinding(sqlalchemy_pool(), schema=os.getenv("TAX_AGENT_DB_SCHEMA"))


def conn_string() -> str:
    """拼 `OpenGaussAsyncSaver.from_conn_string` 要的 psycopg 连接串。

    密码必须 URL 编码：密码里出现 `@` 或 `/` 时，不编码会把连接串的
    host/port/dbname 分段切错位置。
    """
    user = quote(os.environ["TAX_AGENT_DB_USER"], safe="")
    password = quote(os.environ["TAX_AGENT_DB_PASSWORD"], safe="")
    host = os.environ["TAX_AGENT_DB_HOST"]
    port = os.environ["TAX_AGENT_DB_PORT"]
    database = os.environ["TAX_AGENT_DB_NAME"]
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


@asynccontextmanager
async def checkpointer():
    """LangGraph checkpointer：内存或 OpenGauss，由 `enabled()` 决定。

    `OpenGaussAsyncSaver.from_conn_string` 本身是异步上下文管理器（带连接池），
    所以这个函数也是异步上下文管理器，调用方（server.py 的 lifespan）负责
    在 app 生命周期内 `async with` 它。
    """
    if not enabled():
        logger.info("checkpointer：内存实现（InMemorySaver，未配置数据库环境变量）")
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    from eurekax.langgraph_opengauss.opengauss_async_checkpoint import OpenGaussAsyncSaver

    logger.info("checkpointer：OpenGauss 实现（host={} db={}）", os.environ["TAX_AGENT_DB_HOST"], os.environ["TAX_AGENT_DB_NAME"])
    async with OpenGaussAsyncSaver.from_conn_string(conn_string()) as saver:
        await saver.setup()  # 建 checkpoint 相关表，第一次跑之后是幂等的 no-op
        yield saver


def _demo() -> None:
    import asyncio

    saved = {k: os.environ.get(k) for k in (*DB_VARS, "TAX_AGENT_DB_SCHEMA")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        # 五个变量全空 -> enabled() is False，build_session_binding() 返回内存实现
        assert enabled() is False
        assert isinstance(build_session_binding(), MemorySessionBinding)

        # 未启用时 sqlalchemy_pool() 必须拒绝：给出一个连不上的池比当场抛错难查得多
        try:
            sqlalchemy_pool()
            raise AssertionError("应当抛 RuntimeError")
        except RuntimeError as exc:
            assert "未启用" in str(exc), exc

        # 只配 3 个 -> enabled() 抛 RuntimeError，消息里含缺的那两个
        os.environ.update(
            TAX_AGENT_DB_HOST="h", TAX_AGENT_DB_PORT="5432", TAX_AGENT_DB_USER="u"
        )
        try:
            enabled()
            raise AssertionError("应当抛 RuntimeError")
        except RuntimeError as exc:
            assert "TAX_AGENT_DB_PASSWORD" in str(exc) and "TAX_AGENT_DB_NAME" in str(exc), exc

        # 配了一半时 sqlalchemy_pool() 同样过不去——它先过 enabled()，报的是"配了一部分"
        try:
            sqlalchemy_pool()
            raise AssertionError("应当抛 RuntimeError")
        except RuntimeError as exc:
            assert "配了一部分" in str(exc), exc

        # 五个全配 -> enabled() is True；密码含 @ 和 / 时 conn_string() 必须精确编码
        os.environ.update(
            TAX_AGENT_DB_HOST="db.internal",
            TAX_AGENT_DB_PORT="5432",
            TAX_AGENT_DB_USER="tax_agent",
            TAX_AGENT_DB_PASSWORD="p@ss/word",
            TAX_AGENT_DB_NAME="tax_agent_db",
        )
        assert enabled() is True
        assert conn_string() == "postgresql://tax_agent:p%40ss%2Fword@db.internal:5432/tax_agent_db", conn_string()

        # 未启用时 checkpointer() 产出 InMemorySaver 实例
        for k in DB_VARS:
            os.environ.pop(k, None)

        async def _get_checkpointer_type() -> str:
            async with checkpointer() as saver:
                return type(saver).__name__

        assert asyncio.run(_get_checkpointer_type()) == "InMemorySaver"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("persistence self-check ok")


if __name__ == "__main__":
    _demo()
