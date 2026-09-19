"""检索源协议与本地样本实现。

RegulationSource 把"从哪里取条款"与"工具怎么包装结果"分开：本地 JSON 和 P2 的 TTC
是同一协议的两个实现，tools.py 只依赖协议，换源不改工具签名和证据字段。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, NamedTuple, Protocol

# 检索默认只看现行有效版本。历史版本和废止条款是二期"感知"的输入，
# 但在解读场景里默认返回会直接导致错误结论，所以要显式开关才可见。
ACTIVE_STATUSES = frozenset({"PUBLISHED"})

# ponytail: bigram 打分的经验阈值。先判整体是否命中（低于 MIN_TOP_SCORE 就是库里没有，
# 必须报 NO_EVIDENCE 而不是让模型拿弱相关条款硬凑），再裁掉尾部噪声。
# 接入 TTC ES / 向量召回后，由检索引擎自己的相关性分取代这两个常量。
MIN_TOP_SCORE = 0.3
MIN_HIT_SCORE = 0.15


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _bigrams(text: str) -> set[str]:
    """中文按字符 bigram 切分。ponytail: 样本量下够用，换 TTC 的 ES/向量召回后这段整体丢弃。"""
    clean = "".join(ch for ch in text if ch.isalnum())
    return {clean[i : i + 2] for i in range(len(clean) - 1)} or {clean}


def score(query: str, title: str, content: str) -> float:
    """按字符 bigram 给条款打相关性分，标题命中权重高于正文。"""
    q = _bigrams(query)
    if not q:
        return 0.0
    title_hit = len(q & _bigrams(title)) / len(q)
    body_hit = len(q & _bigrams(content)) / len(q)
    # 标题命中比正文命中更能说明相关性，加权而非简单取并集
    return round(0.6 * title_hit + 0.4 * body_hit, 4)


def snippet(query: str, content: str, width: int = 120) -> str:
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


class RegulationSource(Protocol):
    def search(
        self, query: str, tax_category: str | None, include_historical: bool, limit: int
    ) -> SearchResult: ...

    def fetch(self, clause_id: str) -> dict[str, Any] | None: ...


# 任何 RegulationSource 实现返回的每条命中都必须带的证据字段。
EVIDENCE_FIELDS = frozenset({
    "clause_id", "tlp_number", "title", "tax_category", "jurisdiction",
    "clause_status", "revision", "effective_from", "effective_to", "content_hash",
})


def check_source_contract(source: RegulationSource, hit_query: str, known_id: str, missing_id: str) -> None:
    """两个 RegulationSource 实现必须过的同一组断言。

    Args:
        source: 待检验的实现。
        hit_query: 一个确定能命中的查询。
        known_id: 该实现里确定存在的 clause_id。
        missing_id: 确定不存在的 clause_id。
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

    full = source.fetch(known_id)
    assert full is not None and "content" in full, full
    assert EVIDENCE_FIELDS <= set(full), full
    assert content_hash(full["content"]) == full["content_hash"], full

    assert source.fetch(missing_id) is None


class LocalJsonSource:
    def __init__(self, data_file: Path) -> None:
        # 样本库随进程启动读一次即可；lru_cache 挂在实例方法上会把 self 钉死在缓存里
        raw = json.loads(data_file.read_text(encoding="utf-8"))
        self._clauses: tuple[dict[str, Any], ...] = tuple(raw["clauses"])

    def search(
        self, query: str, tax_category: str | None, include_historical: bool, limit: int
    ) -> SearchResult:
        pool = [
            c
            for c in self._clauses
            if (include_historical or c["clause_status"] in ACTIVE_STATUSES)
            and (tax_category is None or c["tax_category"] == tax_category)
        ]
        scored = sorted(
            ((score(query, c["title"], c["content"]), c) for c in pool), key=lambda p: p[0], reverse=True
        )
        hits = (
            [
                {**self._evidence(c), "score": s, "snippet": snippet(query, c["content"])}
                for s, c in scored[: max(limit, 1)]
                if s >= MIN_HIT_SCORE
            ]
            if scored and scored[0][0] >= MIN_TOP_SCORE
            else []
        )
        return SearchResult(hits, len(pool))

    def fetch(self, clause_id: str) -> dict[str, Any] | None:
        # 同一 clause_id 可能对应多条 revision（样本里 AD-VAT-CN-00003 有 1、2 两版）；
        # 对齐 TTC queryTlpInfo 的语义，取 revision 最大的一条，历史版本留给 P1.3 的 fetch_history
        candidates = [c for c in self._clauses if self._clause_id(c) == clause_id]
        if not candidates:
            return None
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

    assert source.search("遗产税起征点", None, False, 5).hits == []
    assert source.fetch("AD-VAT-CN-99999") is None

    # AD-VAT-CN-00003 有两版；fetch 按 TTC queryTlpInfo 语义只返回 revision 最大的一版
    full = source.fetch("AD-VAT-CN-00003")
    assert full["revision"] == 2, full
    assert full["supersedes_revision"] == 1, full
    assert full["content_hash"] == content_hash(full["content"])

    check_source_contract(source, "跨境研发服务零税率需要什么条件", "AD-VAT-CN-00001", "AD-VAT-CN-99999")
    print("sources self-check ok")


if __name__ == "__main__":
    _demo()
