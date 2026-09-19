"""行为评测集：用真实模型跑完整 Agent，检查作答纪律还在不在。

与各模块 `_demo()` 的分工：`_demo()` 验装配和边界条件，不调模型，秒级、免费、每次改代码都跑；
这里验的是"模型在技能约束下的实际行为"，要真调模型，慢且花钱，改 `SKILL.md`、`SYSTEM_PROMPT`
或工具返回结构之后跑。所以它不在 `_demo()` 里，也不该进任何自动化流水线的每次提交。

**只断言机械可判的性质**——引用是否可追溯、该拒答时有没有拒答、废止条款有没有标明、
禁止事项有没有越界。不评价文字质量，也不用模型当裁判：裁判自己会漂，到时候分不清是
被测对象退化了还是裁判变了。评测集的价值是"行为变了要报警"，不是"给答案打分"。

检索源固定 `LocalJsonSource`：这里测的是作答纪律，不是 TTC 的召回能力。样本库是外网唯一
完全可控的语料；TTC 的 keyWord 到底怎么匹配，要等内网 runbook 的结果，那是另一件事。

**结果天然有噪声**：托管端点 kimi-for-coding 强制 temperature=1（见 agent.py 的注释），
同一条用例连跑两次可能一次过一次不过。所以用 `--repeat` 看通过率，不要拿单次结果下结论。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from uuid import uuid4

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CASES_FILE = Path(__file__).resolve().parent / "cases.json"


def _text(content) -> str:
    """把消息 content 归一成字符串——部分端点返回的是内容块列表而不是纯文本。"""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


def _check(expect: dict, answer: str, tool_names: list[str], tool_messages: list, known) -> list[str]:
    """按用例的 expect 逐项判定。

    Args:
        expect: 用例里的期望字典，键的含义见 cases.json 的 _schema。
        answer: 模型最终给用户的回答全文。
        tool_names: 本轮调用过的工具名。
        tool_messages: 本轮的 ToolMessage 列表，引用校验要用。
        known: 法规库中真实存在的 clause_id 全集。

    Returns:
        失败描述列表，全过时为空。
    """
    from tax_agent.audit import CITATION, audit_citations

    failures: list[str] = []
    cited = set(CITATION.findall(answer))

    # always-on：编造条款号是唯一一条任何用例下都不可接受的行为，不用每条用例单独声明
    if fabricated := sorted(cited - known):
        failures.append(f"编造了法规库中不存在的条款号：{', '.join(fabricated)}")

    if expect.get("audit") == "pass" and (problems := audit_citations(answer, tool_messages)):
        failures.append(f"引用校验不通过：{'；'.join(problems)}")

    if expect.get("no_citations") and cited:
        failures.append(f"不该有引用却引用了：{', '.join(sorted(cited))}")

    if missing := [c for c in expect.get("must_cite", []) if c not in cited]:
        failures.append(f"缺少必需的引用：{', '.join(missing)}")

    if extra := [c for c in expect.get("forbid_cite", []) if c in cited]:
        failures.append(f"出现了不该有的引用：{', '.join(extra)}")

    if uncalled := [t for t in expect.get("must_call", []) if t not in tool_names]:
        failures.append(f"没有调用必需的工具：{', '.join(uncalled)}")

    for pattern in expect.get("must_match", []):
        if not re.search(pattern, answer):
            failures.append(f"答案未包含必需内容：/{pattern}/")

    for pattern in expect.get("forbid_match", []):
        if hit := re.search(pattern, answer):
            failures.append(f"答案出现了禁止内容：/{pattern}/ -> {hit.group()!r}")

    return failures


def run_case(agent, case: dict, known) -> tuple[list[str], str]:
    """跑一条用例。

    Returns:
        `(失败描述列表, 答案全文)`。Agent 自己抛异常时算一条失败。
    """
    from langchain_core.messages import ToolMessage

    try:
        # 每条用例一个新 thread_id：用例之间不能串会话，否则上一条的检索结果会喂给下一条
        result = agent.invoke(
            {"messages": [{"role": "user", "content": case["question"]}]},
            config={"configurable": {"thread_id": str(uuid4())}},
        )
    except Exception as exc:  # noqa: BLE001 - 一条用例炸掉不该带走整轮评测
        return [f"Agent 执行异常：{type(exc).__name__}: {exc}"], ""

    messages = result["messages"]
    answer = _text(messages[-1].content)
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    tool_names = [m.name for m in tool_messages]
    return _check(case["expect"], answer, tool_names, tool_messages, known), answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="税法 Agent 行为评测")
    parser.add_argument("--repeat", type=int, default=1, help="每条用例重复跑几次，看通过率（默认 1）")
    parser.add_argument("--only", help="只跑 id 包含这些子串的用例，逗号分隔")
    parser.add_argument("--verbose", action="store_true", help="保留检索日志，并打印失败用例的答案全文")
    args = parser.parse_args(argv)

    # 必须在 import tax_agent.tools 之前：_SOURCE 在模块导入时就定死了
    os.environ["TAX_AGENT_SOURCE"] = "local"

    from tax_agent.server import _load_dotenv_for_dev

    _load_dotenv_for_dev()

    if not args.verbose:
        # 一轮十几次检索的 INFO 日志会把结果表冲没，评测要看的是表不是日志
        logger.remove()
        logger.add(sys.stderr, level="WARNING")

    from tax_agent.agent import build_agent
    from tax_agent.tools import known_clause_ids

    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    if args.only:
        wanted = args.only.split(",")
        cases = [c for c in cases if any(w in c["id"] for w in wanted)]
    if not cases:
        print(f"没有匹配 {args.only!r} 的用例")
        return 1

    agent = build_agent()
    known = known_clause_ids()
    assert known is not None, "评测必须跑在可枚举的样本库上，否则「编造条款号」这道判不了"

    print(f"\n{len(cases)} 条用例 x {args.repeat} 轮\n" + "-" * 72)
    passed = Counter()
    first_failures: dict[str, tuple[list[str], str]] = {}
    for case in cases:
        marks = []
        for _ in range(args.repeat):
            failures, answer = run_case(agent, case, known)
            marks.append("." if not failures else "x")
            if failures:
                first_failures.setdefault(case["id"], (failures, answer))
            else:
                passed[case["id"]] += 1
        rate = passed[case["id"]]
        status = "PASS" if rate == args.repeat else ("FAIL" if rate == 0 else "FLAKY")
        print(f"{status:<6}{case['id']:<32}{rate}/{args.repeat}  {''.join(marks)}")

    failed = [c for c in cases if passed[c["id"]] < args.repeat]
    if failed:
        print("-" * 72)
        for case in failed:
            failures, answer = first_failures[case["id"]]
            print(f"\n[{case['id']}] {case['intent']}")
            for item in failures:
                print(f"  - {item}")
            if args.verbose:
                print(f"  答案全文：\n{answer}\n")
    print("-" * 72)
    print(f"通过 {len(cases) - len(failed)}/{len(cases)} 条\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
