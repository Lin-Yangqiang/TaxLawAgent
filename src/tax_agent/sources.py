"""检索源协议与本地样本实现。

RegulationSource 把"从哪里取条款"与"工具怎么包装结果"分开：本地 JSON 和 P2 的 TTC
是同一协议的两个实现，tools.py 只依赖协议，换源不改工具签名和证据字段。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple, Protocol

# 检索默认只看现行有效版本。历史版本和废止条款是二期"感知"的输入，
# 但在解读场景里默认返回会直接导致错误结论，所以要显式开关才可见。
ACTIVE_STATUSES = frozenset({"PUBLISHED"})

# 草稿在任何开关下都不是证据：include_historical 管的是"已发布过的旧版本"，
# 不是"还没发布的新版本"。两者都不是现行有效，但只有前者曾经是法律。
NEVER_EVIDENCE = frozenset({"DRAFT"})

_AS_OF_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ponytail: bigram 打分的经验阈值。先判整体是否命中（低于 MIN_TOP_SCORE 就是库里没有，
# 必须报 NO_EVIDENCE 而不是让模型拿弱相关条款硬凑），再裁掉尾部噪声。
# 接入 TTC ES / 向量召回后，由检索引擎自己的相关性分取代这两个常量。
MIN_TOP_SCORE = 0.3
MIN_HIT_SCORE = 0.15


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_as_of(value: str) -> str:
    """校验时点日期。

    Args:
        value: 调用方给的时点，必须是 `YYYY-MM-DD`。

    Returns:
        原值（已确认是合法日期）。

    Raises:
        ValueError: 格式不对，或格式对但日期不存在（如 2024-13-45）。

    只认完整日期：把"2024 年 3 月"补成 3 月 1 日等于替用户猜了一个日子，
    而政策在月中切换、月初月末分属不同申报期的情况是真实存在的，宁可退回去问清。
    """
    if not _AS_OF_FORMAT.match(value):
        raise ValueError(f"as_of 必须是 YYYY-MM-DD 格式的完整日期，收到 {value!r}")
    date.fromisoformat(value)  # 格式对但不存在的日期（2024-02-31）只有这里拦得住
    return value


def covers(clause: dict[str, Any], as_of: str) -> bool | None:
    """条款的生效区间是否覆盖某个时点。

    Args:
        clause: 任何带 effective_from / effective_to 的条款或证据字典。
        as_of: 已过 parse_as_of 的日期。

    Returns:
        True 覆盖，False 不覆盖，None 表示生效起始日缺失、判不了。

    三态而不是布尔：把"判不了"并进 False，就等于替 TTC 里日期没维护的条款断言
    "它当时不适用"，这是凭空造出来的结论。调用方必须把这类条款单独计数报出来。
    ISO 日期字符串按字典序比较即为时间序，所以不需要转 date 对象。
    """
    start = clause.get("effective_from")
    if start is None:
        return None
    end = clause.get("effective_to")
    return start <= as_of and (end is None or as_of <= end)


def _bigrams(text: str) -> set[str]:
    """中文按字符 bigram 切分。ponytail: 样本量下够用，换 TTC 的 ES/向量召回后这段整体丢弃。"""
    clean = "".join(ch for ch in text if ch.isalnum())
    return {clean[i : i + 2] for i in range(len(clean) - 1)} or {clean}


def relevance_score(query: str, title: str, content: str) -> float:
    """按字符 bigram 给条款打相关性分，标题命中权重高于正文。"""
    q = _bigrams(query)
    if not q:
        return 0.0
    title_hit = len(q & _bigrams(title)) / len(q)
    body_hit = len(q & _bigrams(content)) / len(q)
    # 标题命中比正文命中更能说明相关性，加权而非简单取并集
    return round(0.6 * title_hit + 0.4 * body_hit, 4)


def best_snippet(query: str, content: str, width: int = 120) -> str:
    """截取与查询最相关的一段正文，避免把整条条款塞进模型上下文。"""
    grams = _bigrams(query)
    best_pos, best_hits = 0, -1
    for pos in range(0, max(len(content) - width, 0) + 1, 20):
        hits = len(grams & _bigrams(content[pos : pos + width]))
        if hits > best_hits:
            best_pos, best_hits = pos, hits
    cut = content[best_pos : best_pos + width]
    return ("…" if best_pos else "") + cut + ("…" if best_pos + width < len(content) else "")


class SearchResult(NamedTuple):
    hits: list[dict[str, Any]]
    total_candidates: int
    # 时点检索下因缺生效日期而判不了、被排除的条数。默认 0，不传的实现照旧工作
    undated_excluded: int = 0


class RegulationSource(Protocol):
    def search(
        self,
        query: str,
        tax_category: str | None,
        include_historical: bool,
        limit: int,
        as_of: str | None = None,
    ) -> SearchResult:
        """检索条款。

        `as_of` 与 `include_historical` 管的是两个不同的问题，不要混用：
        `include_historical` 按**当前状态**放行（回答"这政策还有效吗"），
        `as_of` 按**生效区间**过滤且当前状态完全不参与（回答"当时适用什么"）——
        一条今天已 SUPERSEDED 的条款，正是它当年的正确依据。

        Raises:
            NotImplementedError: 该源没有生效日期字段，无法按时点检索。
                必须显式拒绝而不是忽略 as_of 照常返回：忽略等于拿今天的法规
                回答"当年适用什么"，是实打实的错误结论。
        """
        ...

    def fetch(self, clause_id: str, as_of: str | None = None) -> dict[str, Any] | None:
        """取条款完整正文。

        `as_of` 必须与检索时用的时点一致。缺省取最新已发布版本——但在时点检索场景下
        那是**错的版本**：检索命中的是当年那一版，取全文却拿回现行版，结论会整个反过来。

        Raises:
            NotImplementedError: 该源取不到历史版本正文。
        """
        ...


# 任何 RegulationSource 实现返回的每条命中都必须带的证据字段。
EVIDENCE_FIELDS = frozenset({
    "clause_id", "tlp_number", "title", "tax_category", "jurisdiction",
    "clause_status", "revision", "effective_from", "effective_to", "content_hash",
})


def check_source_contract(
    source: RegulationSource, hit_query: str, known_id: str, missing_id: str, as_of_hit: str
) -> None:
    """每个 RegulationSource 实现必须过的同一组断言。

    Args:
        source: 待检验的实现。
        hit_query: 一个确定能命中的查询。
        known_id: 该实现里确定存在的 clause_id。
        missing_id: 确定不存在的 clause_id。
        as_of_hit: 用于时点检索的日期；源不支持 as_of 时该值不影响结果。
    """
    result = source.search(hit_query, None, False, 5)
    assert result.hits, result

    for hit in result.hits:
        assert EVIDENCE_FIELDS <= set(hit), hit
        assert "snippet" in hit, hit

    default_statuses = {h["clause_status"] for h in result.hits}
    assert default_statuses <= {"PUBLISHED"}, default_statuses

    # 草稿不是法规，哪怕主动要历史版本也不能把 DRAFT 当证据吐出来
    historical = source.search(hit_query, None, True, 5).hits
    assert all(h["clause_status"] != "DRAFT" for h in historical), historical

    # 时点检索只有两种合法反应：如实按生效区间过滤，或显式拒绝。
    # 第三种"忽略 as_of 照常返回"是错的，而且错得看不出来，所以这里必须二选一地钉死。
    try:
        dated = source.search(hit_query, None, False, 5, as_of_hit)
    except NotImplementedError:
        pass
    else:
        for hit in dated.hits:
            assert covers(hit, as_of_hit) is True, hit
        assert all(h["clause_status"] not in NEVER_EVIDENCE for h in dated.hits), dated.hits

    full = source.fetch(known_id)
    assert full is not None and "content" in full, full
    assert EVIDENCE_FIELDS <= set(full), full
    assert content_hash(full["content"]) == full["content_hash"], full

    # fetch 的时点必须和 search 的时点对得上，否则"按当年那一版检索、按现行版取全文"
    # 会给出恰好相反的结论，而且从返回值里看不出哪里错了
    try:
        dated_full = source.fetch(known_id, as_of_hit)
    except NotImplementedError:
        pass
    else:
        assert dated_full is None or covers(dated_full, as_of_hit) is True, dated_full

    assert source.fetch(missing_id) is None


class LocalJsonSource:
    def __init__(self, data_file: Path) -> None:
        # 样本库随进程启动读一次即可；lru_cache 挂在实例方法上会把 self 钉死在缓存里
        raw = json.loads(data_file.read_text(encoding="utf-8"))
        self._clauses: tuple[dict[str, Any], ...] = tuple(raw["clauses"])

    def search(
        self,
        query: str,
        tax_category: str | None,
        include_historical: bool,
        limit: int,
        as_of: str | None = None,
    ) -> SearchResult:
        pool: list[dict[str, Any]] = []
        undated = 0
        for c in self._clauses:
            # 这道原先漏了：`include_historical or ...` 会把 DRAFT 一并放出来。
            # 样本库里一条草稿都没有，契约里"草稿永不作为证据"那条断言就一直是空过的
            if c["clause_status"] in NEVER_EVIDENCE:
                continue
            if tax_category is not None and c["tax_category"] != tax_category:
                continue
            if as_of is not None:
                # 时点检索下不看当前状态：今天已 SUPERSEDED 的那版正是 as_of 当天的正确依据
                covered = covers(c, as_of)
                if covered is None:
                    undated += 1
                    continue
                if not covered:
                    continue
            elif not include_historical and c["clause_status"] not in ACTIVE_STATUSES:
                continue
            pool.append(c)
        scored = sorted(
            ((relevance_score(query, c["title"], c["content"]), c) for c in pool),
            key=lambda p: p[0],
            reverse=True,
        )
        hits = (
            [
                {**self._evidence(c), "score": s, "snippet": best_snippet(query, c["content"])}
                for s, c in scored[: max(limit, 1)]
                if s >= MIN_HIT_SCORE
            ]
            if scored and scored[0][0] >= MIN_TOP_SCORE
            else []
        )
        return SearchResult(hits, len(pool), undated)

    def fetch(self, clause_id: str, as_of: str | None = None) -> dict[str, Any] | None:
        # 同一 clause_id 可能对应多条 revision（样本里 AD-VAT-CN-00003 有 1、2 两版）。
        # 草稿在这里也要挡掉：search 给不出草稿的 clause_id，但用户可以直接把号报给模型
        candidates = [
            c
            for c in self._clauses
            if self._clause_id(c) == clause_id and c["clause_status"] not in NEVER_EVIDENCE
        ]
        if as_of is not None:
            candidates = [c for c in candidates if covers(c, as_of) is True]
        if not candidates:
            return None
        # 无 as_of 时对齐 TTC queryTlpInfo 的语义取最新版；有 as_of 时候选已被区间裁到
        # 当时有效的那些，再取最大 revision 就是当时的现行版
        clause = max(candidates, key=lambda c: c["revision"])
        result = {**self._evidence(clause), "content": clause["content"]}
        if clause.get("supersedes"):
            result["supersedes_revision"] = clause["supersedes"]
        return result

    def known_clause_ids(self) -> set[str]:
        return {self._clause_id(c) for c in self._clauses}

    @staticmethod
    def _clause_id(clause: dict[str, Any]) -> str:
        """条款证据的唯一键：就是 TTC 的 tlpNumber（source_id）本身，不含版本。"""
        return clause["source_id"]

    @classmethod
    def _evidence(cls, clause: dict[str, Any]) -> dict[str, Any]:
        return {
            "clause_id": cls._clause_id(clause),
            "tlp_number": clause["tlp_number"],
            "title": clause["title"],
            "tax_category": clause["tax_category"],
            "jurisdiction": clause["jurisdiction"],
            # 条款自身的生效状态，与工具返回的 status（调用结果）是两回事，不能同名
            "clause_status": clause["clause_status"],
            "revision": clause["revision"],
            "effective_from": clause["effective_from"],
            "effective_to": clause["effective_to"],
            "content_hash": content_hash(clause["content"]),
        }


def _demo() -> None:
    source = LocalJsonSource(Path(__file__).resolve().parents[2] / "data" / "clauses.json")

    found = source.search("跨境研发服务零税率需要什么条件", None, False, 5)
    assert found.hits[0]["clause_id"] == "AD-VAT-CN-00001", found.hits[0]
    assert found.total_candidates > 0, found

    # 默认不得返回已废止或被替代的版本，否则会给出过期结论
    statuses = {h["clause_status"] for h in source.search("进项税额抵扣凭证", None, False, 5).hits}
    assert statuses == {"PUBLISHED"}, statuses
    historical = source.search("进项税额抵扣凭证", None, True, 5).hits
    assert any(h["clause_status"] == "SUPERSEDED" for h in historical), historical

    # 草稿在任何开关下都不作为证据。样本里那条草稿与 00001 高度同题，不过滤会直接顶到前排
    for flag in (False, True):
        drafted = source.search("跨境研发服务零税率备案", None, flag, 10).hits
        assert all(h["clause_status"] != "DRAFT" for h in drafted), (flag, drafted)

    assert source.search("遗产税起征点", None, False, 5).hits == []
    assert source.fetch("AD-VAT-CN-99999") is None

    # 时点检索：2024-06 有效的是 AD-VAT-CN-00003 的 revision 1（今天已 SUPERSEDED），
    # 不是现行的 revision 2。状态过滤在 as_of 下必须让位，否则回答不了"当年适用什么"
    past = {(h["clause_id"], h["revision"]) for h in source.search("进项税额抵扣凭证", None, False, 5, "2024-06-01").hits}
    assert ("AD-VAT-CN-00003", 1) in past, past
    assert ("AD-VAT-CN-00003", 2) not in past, past
    now = {(h["clause_id"], h["revision"]) for h in source.search("进项税额抵扣凭证", None, False, 5, "2026-06-01").hits}
    assert ("AD-VAT-CN-00003", 2) in now, now
    assert ("AD-VAT-CN-00003", 1) not in now, now
    # 早于全库最早生效日：必须空手而归，不能退化成"忽略 as_of 照常返回"
    assert source.search("进项税额抵扣凭证", None, False, 5, "2000-01-01").hits == []

    assert parse_as_of("2024-06-01") == "2024-06-01"
    for bad in ("2024-06", "2024年6月1日", "20240601", "2024-02-31"):
        try:
            parse_as_of(bad)
            raise AssertionError(f"{bad!r} 应当被拒")
        except ValueError:
            pass

    assert covers({"effective_from": "2024-01-01", "effective_to": None}, "2024-06-01") is True
    assert covers({"effective_from": "2025-01-01", "effective_to": None}, "2024-06-01") is False
    # 判不了必须是 None 而不是 False：并进 False 等于替没维护日期的条款断言"当时不适用"
    assert covers({"effective_from": None, "effective_to": None}, "2024-06-01") is None

    # AD-VAT-CN-00003 有两版；fetch 按 TTC queryTlpInfo 语义只返回 revision 最大的一版
    full = source.fetch("AD-VAT-CN-00003")
    assert full["revision"] == 2, full
    assert full["supersedes_revision"] == 1, full
    assert full["content_hash"] == content_hash(full["content"])

    # 带时点取全文必须拿到当年那一版。这条不成立时 as_of 检索就是个陷阱：命中的是
    # revision 1 的片段，取回来的却是 revision 2 的正文，结论正好反过来
    assert source.fetch("AD-VAT-CN-00003", "2024-06-01")["revision"] == 1
    assert "暂不得抵扣" in source.fetch("AD-VAT-CN-00003", "2024-06-01")["content"]
    assert source.fetch("AD-VAT-CN-00003", "2026-06-01")["revision"] == 2
    # 该时点这条还没生效：NOT_FOUND，不能退回去给现行版
    assert source.fetch("AD-VAT-CN-00003", "2020-01-01") is None
    # 草稿不能凭 clause_id 直接取到——search 给不出这个号，但用户可以直接报给模型
    assert source.fetch("AD-VAT-CN-00099") is None

    check_source_contract(
        source, "跨境研发服务零税率需要什么条件", "AD-VAT-CN-00001", "AD-VAT-CN-99999", "2026-06-01"
    )
    print("sources self-check ok")


if __name__ == "__main__":
    _demo()
