"""引用校验：答案里的每个 clause_id 都必须真实存在，且本轮确实被工具返回过。

这是本 Agent 的核心质量门禁——模型编造一个看起来合理的条款号是最危险的失败模式，
而它恰好是最难被人眼发现的。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from loguru import logger

# 直接 `python src/tax_agent/audit.py` 跑自检时 sys.path[0] 是本文件目录而非 src/
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# TTC 的 tlpNumber 形如 AD-ITX-CN-00275 / AD-TA-General-00652 / AD-PE-CN/CO-00003：
# 首段全大写，其后至少一段，段内可能混大小写（General），也可能带斜杠（跨税地条款的
# CN/CO）——这两种都是内网真实语料里抄回来的形态，不是假想。斜杠那种旧正则识别不了，
# 于是既不算引用也不算"检索过"，编造一个带斜杠的条款号可以静默绕过整道门禁。
# 不再拼版本号，clause_id 就是 tlpNumber 本身。
# 最后一段收紧到 ≥3 位数字，是为了不让 ToolMessage JSON 里的 "CN-SG"、日期之类误命中。
CLAUSE_ID = r"[A-Z]{2,}(?:-[A-Za-z0-9/]+)+-\d{3,}"
CITATION = re.compile(rf"\[({CLAUSE_ID})\]")


def audit_citations(answer: str, messages: list) -> list[str]:
    """校验答案里的条款引用是否可追溯。

    Args:
        answer: 模型最终给用户的回答全文。
        messages: 本轮对话的消息列表，其中的 ToolMessage 是"本轮检索过什么"的唯一依据。

    Returns:
        问题描述列表，全部通过时为空列表。
    """
    from langchain_core.messages import ToolMessage

    from tax_agent.tools import known_clause_ids

    cited = set(CITATION.findall(answer))
    if not cited:
        return ["回答中没有任何条款引用"] if len(answer) > 80 else []

    retrieved: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolMessage):
            retrieved.update(re.findall(CLAUSE_ID, str(msg.content)))

    problems = []
    known = known_clause_ids()
    if known is None:
        # TTC 源无法枚举整个法规库，"条款是否真实存在"这道只能放过。
        # 下面那道对任何源都成立，而且它才是关键的一道：模型编造的条款号本轮必然没被检索过，
        # 所以照样会被拦下，只是报出来的原因变成"未经检索"而不是"不存在"。
        logger.info("检索源不支持枚举 clause_id，跳过「条款是否真实存在」校验")
    elif fabricated := sorted(cited - known):
        problems.append(f"引用了法规库中不存在的条款：{', '.join(fabricated)}")
    in_scope = cited if known is None else cited & known
    if unretrieved := sorted(in_scope - retrieved):
        problems.append(f"引用了本轮未经检索的条款：{', '.join(unretrieved)}")
    return problems


def _demo() -> None:
    from langchain_core.messages import ToolMessage

    retrieved = [ToolMessage(content='{"clause_id": "AD-VAT-CN-00001"}', tool_call_id="1")]
    assert audit_citations("零税率需备案[AD-VAT-CN-00001]", retrieved) == []
    # 库里不存在的条款号——模型编造，最危险的失败模式
    assert "不存在" in audit_citations("依据[AD-VAT-CN-99999]", retrieved)[0]
    # 真实存在但本轮没检索过——凭记忆作答
    assert "未经检索" in audit_citations("依据[AD-ITX-CN-00101]", retrieved)[0]
    assert audit_citations("本轮没有法规事实，" * 10, retrieved) == ["回答中没有任何条款引用"]

    # 段内混大小写（TTC 的 General 税地）必须能识别，这是旧正则漏掉的形态
    mixed = [ToolMessage(content="AD-TA-General-00401", tool_call_id="1")]
    assert audit_citations("协定优先[AD-TA-General-00401]", mixed) == []

    # 段内带斜杠（跨税地条款）同样是真实形态。第二条才是关键：旧正则下它连引用都算不上，
    # 编造的斜杠条款号会被静默放过，门禁形同虚设
    slashed = [ToolMessage(content="AD-PE-CN/CO-00601", tool_call_id="1")]
    assert audit_citations("构成固定营业场所[AD-PE-CN/CO-00601]", slashed) == []
    assert "未经检索" in audit_citations("构成固定营业场所[AD-PE-CN/CO-00601]", retrieved)[0]

    # 不该命中的东西：段数不够/末段数字不够的代码、日期、纯大写单词
    for noise in ("[CN-SG]", "[2026-01-01]", "[PUBLISHED]"):
        assert CITATION.findall(noise) == [], noise

    # 检索源不可枚举（两个 TTC 源）时，"不存在"那道放过，但编造的条款号仍必须被拦下——
    # 它本轮没被检索过，会落到"未经检索"那道
    import tax_agent.tools as tools_module

    original = tools_module.known_clause_ids
    tools_module.known_clause_ids = lambda: None
    try:
        assert audit_citations("零税率需备案[AD-VAT-CN-00001]", retrieved) == []
        problems = audit_citations("依据[AD-VAT-CN-99999]", retrieved)
        assert len(problems) == 1 and "未经检索" in problems[0], problems
    finally:
        tools_module.known_clause_ids = original
    print("citation audit self-check ok")


if __name__ == "__main__":
    _demo()
