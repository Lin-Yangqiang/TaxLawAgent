"""税法 Agent 本地 CLI。

用法：
    python cli.py                      交互对话（进程内直连 agent，本地调试用）
    python cli.py "跨境研发服务零税率要满足什么条件"   单轮提问
    python cli.py --url http://127.0.0.1:8000/tax-agent "..."  走 HTTP 服务（url 要带服务名前缀）

直连模式保留，是因为它不经过身份链路，调检索和提示词时少一层干扰。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，中文和省略号会炸


def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    import os

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def ask(agent, question: str, thread_id: str) -> None:
    from tax_agent.audit import audit_citations

    result = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"configurable": {"thread_id": thread_id}},
    )
    messages = result["messages"]
    answer = messages[-1].content
    print(f"\n{answer}\n")

    tool_calls = [m.name for m in messages if type(m).__name__ == "ToolMessage"]
    print(f"[工具调用] {', '.join(tool_calls) if tool_calls else '无'}")
    for problem in audit_citations(str(answer), messages):
        print(f"[引用校验] 不通过：{problem}")


def ask_http(url: str, question: str, state: dict) -> None:
    """走 /chat SSE。用 urllib 而非 httpx：CLI 不值得为此多一个运行时依赖。"""
    import json
    import os
    import urllib.request

    body = {"question": question, "session_id": state.get("session_id")}
    headers = {"Content-Type": "application/json"}
    if token := os.getenv("TAX_AGENT_USER_TOKEN"):
        headers["x-jwt-ms-token"] = token

    req = urllib.request.Request(f"{url}/chat", json.dumps(body).encode(), headers)
    print()
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            line = line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[len("data: ") :])
            if event["type"] == "token":
                print(event["text"], end="", flush=True)
            else:
                state["session_id"] = event["session_id"]
                print(f"\n\n[工具调用] {', '.join(event['tool_calls']) or '无'}")
                for problem in event["citation_problems"]:
                    print(f"[引用校验] 不通过：{problem}")


def main() -> int:
    from tax_agent.log import setup

    setup()

    if "--selfcheck" in sys.argv:
        from tax_agent import audit

        audit._demo()
        return 0

    argv = sys.argv[1:]
    url = None
    if "--url" in argv:
        i = argv.index("--url")
        url = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]

    if url:
        state: dict = {}
        turn = lambda q: ask_http(url, q, state)  # noqa: E731
    else:
        _load_env()
        from tax_agent.agent import build_agent

        try:
            agent = build_agent()
        except RuntimeError as exc:
            print(f"启动失败：{exc}")
            return 1
        thread_id = str(uuid.uuid4())
        turn = lambda q: ask(agent, q, thread_id)  # noqa: E731

    if argv:
        turn(" ".join(argv))
        return 0

    print("税法检索与解读 Agent（本地样本库）。输入问题，空行或 Ctrl-C 退出。")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not question:
            return 0
        turn(question)


if __name__ == "__main__":
    raise SystemExit(main())
