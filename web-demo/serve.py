"""本地演示页面的组合根。

`src/tax_agent/server.py` 一行不改：这里只是把它产出的 app 拿过来，再挂上
demo 的静态页面和样本库目录。依赖是单向的（web-demo -> tax_agent），
服务本身不知道 demo 存在，生产照旧跑 `uvicorn tax_agent.server:create_app --factory`，
页面根本不在部署产物里（pyproject 的 packages.find 只收 src/）。

跑：`.venv/Scripts/python.exe web-demo/serve.py`，浏览器开 http://127.0.0.1:8011/

**这个页面能自己填 `x-jalor-userAccount`**——服务本来就无条件信任这个网关头
（见 identity.py），所以 demo 加不加都一样：真正的约束是**服务绝不能脱离网关暴露**，
否则任何人都能声称自己是任何人。本地跑 127.0.0.1 没问题，别往外绑。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from tax_agent.server import create_app  # noqa: E402


def build_demo_app(agent=None):
    """给服务 app 挂上 demo 的静态资源。

    Args:
        agent: 透传给 create_app，自检时注入 stub 避免真调模型。

    Returns:
        挂好静态目录的 FastAPI 实例。
    """
    app = create_app(agent=agent)
    # 主屏那个法规库工作台直接读样本库原文件，省掉一个只会转发 JSON 的路由
    app.mount("/demo-data", StaticFiles(directory=ROOT / "data"), name="demo-data")
    # 挂在 "/" 上：Starlette 按注册顺序匹配，/tax-agent/* 和 docs 都在前面，不会被盖住
    app.mount("/", StaticFiles(directory=HERE, html=True), name="demo")
    return app


def _demo() -> None:
    """只验路由装配：模型和 SSE 由 server.py 的自检管。"""
    from fastapi.testclient import TestClient

    class StubAgent:
        async def astream(self, _input, config=None, stream_mode=None):
            return
            yield  # pragma: no cover - 让它成为 async generator

    client = TestClient(build_demo_app(agent=StubAgent()))

    assert client.get("/").status_code == 200
    assert "副屏" in client.get("/").text
    assert client.get("/demo-data/clauses.json").json()["clauses"], "主屏拿不到样本库"
    # 挂 "/" 不能把业务路由盖掉——盖掉了页面会静默变成 404 页面而不是报错
    assert client.post("/tax-agent/chat", json={"question": "你好"}).status_code == 200

    print("web-demo self-check ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _demo()
    else:
        port = int(os.getenv("PORT", "8011"))
        print(f"演示页面：http://127.0.0.1:{port}/", file=sys.stderr, flush=True)
        uvicorn.run(build_demo_app(), host="127.0.0.1", port=port)
