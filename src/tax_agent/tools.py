"""法规检索工具。

工具本身只做参数透传和返回壳（status / message / applied_filters），
真正的检索交给 sources.RegulationSource；P2 接入 TTC 时新增一个协议实现并替换
`_SOURCE`，工具签名、证据字段和 clause_id 规则保持不变，Agent 与 Skill 无需改动。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from loguru import logger

# 直接 `python src/tax_agent/tools.py` 跑自检时 sys.path[0] 是本文件目录而非 src/
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tax_agent.sources import (  # noqa: E402
    LocalJsonSource,
    RegulationSource,
    content_hash,
    parse_as_of,
)

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "clauses.json"


def _build_source() -> RegulationSource:
    """按 TAX_AGENT_SOURCE 选检索源。

    三个取值：`local`（缺省，样本 JSON）、`ttc_public`（应用级凭证调公有 API，
    **仅本地开发**）、`ttc_user`（透传用户 token 调私有接口，生产路径）。

    Returns:
        选中的 RegulationSource 实现。

    Raises:
        ValueError: TAX_AGENT_SOURCE 取值不认识。缺省行为是 local，不是报错，
            但写错一个值应当立刻炸掉——静默退回样本库会让人以为在查真实法规。
    """
    kind = os.getenv("TAX_AGENT_SOURCE", "local")
    if kind == "local":
        logger.info("检索源：LocalJsonSource（样本库 {}）", DATA_FILE.name)
        return LocalJsonSource(DATA_FILE)

    if kind not in {"ttc_public", "ttc_user"}:
        # 先校验取值再读 TTC_BASE_URL，否则写错源名字会报成"缺 TTC_BASE_URL"，指向错的地方
        raise ValueError(
            f"TAX_AGENT_SOURCE 取值不认识：{kind!r}，可选 local / ttc_public / ttc_user"
        )

    from tax_agent.ttc_client import TtcPublicSource, TtcSource

    base_url = os.environ["TTC_BASE_URL"]
    if kind == "ttc_user":
        from tax_agent.auth import UserTokenProvider

        logger.info("检索源：TtcSource（私有接口 + 透传用户 token）")
        return TtcSource(base_url, UserTokenProvider())

    logger.warning(
        "检索源：TtcPublicSource（公有 API + 应用级凭证）。仅限本地开发："
        "无用户身份、维度权限不生效，且已废止条款无法识别"
    )
    return TtcPublicSource(base_url, _build_app_auth())


def _build_app_auth():  # noqa: ANN202 - 返回 auth.AuthProvider，惰性 import 拿不到类型名
    """按环境变量选应用级凭证，APIGW 优先（静态 AK/SK，不用换 token）。

    Raises:
        ValueError: 两组凭证都没配全。
    """
    from tax_agent.auth import ApicProvider, ApigwProvider

    if os.getenv("TTC_APIGW_ID"):
        return ApigwProvider(os.environ["TTC_APIGW_ID"], os.environ["TTC_APIGW_APPKEY"])
    if os.getenv("TTC_APIC_APP_ID"):
        return ApicProvider(
            os.environ["TTC_APIC_TOKEN_URL"],
            os.environ["TTC_APIC_APP_ID"],
            os.environ["TTC_APIC_SECRET"],
        )
    raise ValueError(
        "ttc_public 需要一组应用级凭证：TTC_APIGW_ID + TTC_APIGW_APPKEY，"
        "或 TTC_APIC_TOKEN_URL + TTC_APIC_APP_ID + TTC_APIC_SECRET"
    )


_SOURCE = _build_source()


def _undated_note(count: int) -> dict[str, str]:
    """时点检索下判不了的条款要如实报出来，不能当它们不存在。"""
    if not count:
        return {}
    return {
        "undated_excluded": f"另有 {count} 条命中但法规库里没维护生效日期，无法判定其在该时点"
        f"是否有效，已排除。不要据此断言该时点不存在相关规定。"
    }


@tool
def search_regulation(
    query: str,
    tax_category: str | None = None,
    include_historical: bool = False,
    as_of: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """按自然语言描述检索税法条款，返回条款摘要与证据引用。

    Args:
        query: 检索内容，用业务语言描述，例如"跨境研发服务零税率的条件"。
        tax_category: 可选税种过滤，如"增值税"、"企业所得税"、"个人所得税"、"印花税"。
        include_historical: 是否把已废止（REVOKED）和已被替代（SUPERSEDED）的条款一并放出来，
            默认 False。回答"某政策现在还有没有效""什么时候废止的"时开它。
        as_of: 时点检索，格式 `YYYY-MM-DD`。回答"某年某月那笔业务当时适用什么规定"时传它——
            返回的是生效区间覆盖该日期的条款，当时有效、今天已被替代或废止的那一版才是
            正确依据，用现行条款回答过去的业务是错的。用户只说了年月没说是哪天时，先问清，
            不要自己补成 1 号。传了 as_of 就不必再传 include_historical，时点本身已经
            决定了看哪一版。
        limit: 返回条数上限，默认 5。

    Returns:
        status 为 OK / NO_EVIDENCE / INVALID_AS_OF / UNSUPPORTED_AS_OF。
        OK 时 hits 中每项含 clause_id、snippet、score 和证据字段；snippet 是片段，
        需要完整条款正文时用 clause_id 调用 fetch_clause。
    """
    if as_of is not None:
        try:
            as_of = parse_as_of(as_of)
        except ValueError as exc:
            logger.warning("as_of 非法：{}", exc)
            return {"status": "INVALID_AS_OF", "message": str(exc)}

    try:
        result = _SOURCE.search(query, tax_category, include_historical, limit, as_of)
        if not result.hits and tax_category is not None:
            # 模型对税种的判断可能是错的（如把"税收协定"条款误判为"企业所得税"而过滤掉），
            # 漏检和编造一样有害，所以去掉税种过滤重试一次，而不是指望模型读懂 NO_EVIDENCE 里的提示
            relaxed = _SOURCE.search(query, None, include_historical, limit, as_of)
            if relaxed.hits:
                logger.warning(
                    "税种过滤误杀，已放宽重试：query={!r} tax_category={!r} 放宽后命中={}",
                    query,
                    tax_category,
                    len(relaxed.hits),
                )
                return {
                    "status": "OK",
                    "hits": relaxed.hits,
                    "total_candidates": relaxed.total_candidates,
                    "applied_filters": {
                        "tax_category": None,
                        "include_historical": include_historical,
                        "as_of": as_of,
                    },
                    "relaxed_filter": f"按 tax_category={tax_category!r} 过滤后无命中，已忽略该过滤条件重新检索",
                    **_undated_note(relaxed.undated_excluded),
                }
    except NotImplementedError as exc:
        # 只有 TtcPublicSource 会走到这里：没有生效日期字段，时点检索无从谈起
        logger.warning("检索源不支持时点检索：{}", exc)
        return {"status": "UNSUPPORTED_AS_OF", "message": str(exc)}

    applied_filters = {
        "tax_category": tax_category,
        "include_historical": include_historical,
        "as_of": as_of,
    }
    logger.info(
        "search_regulation query={!r} filters={} hits={} 生效日缺失={}",
        query, applied_filters, len(result.hits), result.undated_excluded,
    )
    if not result.hits:
        payload = {
            "status": "NO_EVIDENCE",
            "hits": [],
            "applied_filters": applied_filters,
            "message": "法规库中没有匹配的现行条款。可放宽税种过滤或换用其他表述重试。",
            **_undated_note(result.undated_excluded),
        }
        # 死角：问"某政策现在还有效吗"时，正确答案（已废止的那条）恰好在被默认过滤掉的那堆里，
        # 于是模型收到 NO_EVIDENCE 后回"检索不到"——而事实是"检索到了，它已经废止了"。
        # 这里只探一次并回报条数，**不把历史条款当结果返回**：历史条款绝不能自动变成证据，
        # 要不要采信、是当现行依据还是当废止证明，必须由模型显式再查一次来决定。
        if as_of is None and not include_historical:
            probe = _SOURCE.search(query, None, True, limit)
            if probe.hits:
                logger.info("现行无命中但历史池命中 {} 条，已回报给模型", len(probe.hits))
                payload["historical_candidates"] = len(probe.hits)
                payload["message"] = (
                    f"现行有效条款中没有匹配，但有 {len(probe.hits)} 条已废止或已被替代的条款命中。"
                    "这通常意味着用户问的政策已经失效：用 include_historical=true 重新检索确认"
                    "并说明废止情况，或用 as_of=<YYYY-MM-DD> 查当时的版本。"
                    "在没有重新检索之前，不要断言'法规库中查不到'。"
                )
        return payload
    return {
        "status": "OK",
        "hits": result.hits,
        "total_candidates": result.total_candidates,
        **_undated_note(result.undated_excluded),
    }


@tool
def fetch_clause(clause_id: str, as_of: str | None = None) -> dict[str, Any]:
    """按 clause_id 获取条款完整正文与证据信息。

    Args:
        clause_id: search_regulation 返回的条款标识，即 TTC 的 tlpNumber，如 `AD-VAT-CN-00001`。
        as_of: 时点，格式 `YYYY-MM-DD`。**检索时传了 as_of 的，这里必须传同一个日期**——
            缺省取的是最新版本正文，而你检索命中的是当年那一版，两者结论可能完全相反。

    Returns:
        status 为 OK / NOT_FOUND / INVALID_AS_OF / UNSUPPORTED_AS_OF。
        OK 时含完整 content、content_hash 及版本信息。
        只能引用本工具或 search_regulation 实际返回过的 clause_id，不得自行拼造。
    """
    if as_of is not None:
        try:
            as_of = parse_as_of(as_of)
        except ValueError as exc:
            logger.warning("as_of 非法：{}", exc)
            return {"status": "INVALID_AS_OF", "clause_id": clause_id, "message": str(exc)}

    try:
        result = _SOURCE.fetch(clause_id, as_of)
    except NotImplementedError as exc:
        logger.warning("检索源取不到历史版本正文：{}", exc)
        return {"status": "UNSUPPORTED_AS_OF", "clause_id": clause_id, "message": str(exc)}

    if result is None:
        logger.warning("fetch_clause NOT_FOUND：clause_id={!r} as_of={}", clause_id, as_of)
        return {
            "status": "NOT_FOUND",
            "clause_id": clause_id,
            "message": (
                f"该条款在 {as_of} 这个时点尚未生效或已失效，不得作为该时点的依据。"
                if as_of
                else "该 clause_id 在法规库中不存在，不得作为依据引用。"
            ),
        }
    return {"status": "OK", **result}


TOOLS = [search_regulation, fetch_clause]


# 故意不放进 RegulationSource 协议：接入 TTC 后无法枚举整个法规库，任何实现都给不出全集。
# 引用校验真正关键的那道是"引用了本轮未经检索的条款"，那道对任何检索源都成立。
def known_clause_ids() -> set[str] | None:
    """供引用校验使用：法规库中真实存在的 clause_id 全集。

    Returns:
        全集；检索源无法枚举（两个 TTC 源都是）时返回 None。
    """
    enumerate_ids = getattr(_SOURCE, "known_clause_ids", None)
    return enumerate_ids() if enumerate_ids is not None else None


def _demo() -> None:
    hits = search_regulation.invoke({"query": "跨境研发服务零税率需要什么条件"})
    assert hits["status"] == "OK", hits
    assert hits["hits"][0]["clause_id"] == "AD-VAT-CN-00001", hits["hits"][0]

    # 默认不得返回已废止或被替代的版本，否则会给出过期结论
    statuses = {h["clause_status"] for h in search_regulation.invoke({"query": "进项税额抵扣凭证"})["hits"]}
    assert statuses == {"PUBLISHED"}, statuses
    historical = search_regulation.invoke({"query": "进项税额抵扣凭证", "include_historical": True})
    assert any(h["clause_status"] == "SUPERSEDED" for h in historical["hits"]), historical

    full = fetch_clause.invoke({"clause_id": "AD-VAT-CN-00003"})
    assert full["status"] == "OK" and full["revision"] == 2 and full["supersedes_revision"] == 1, full
    assert full["content_hash"] == content_hash(full["content"])

    assert fetch_clause.invoke({"clause_id": "AD-VAT-CN-99999"})["status"] == "NOT_FOUND"

    # 真实触发过的 bug：税种判断错误（"税收协定"条款被误传成 企业所得税）导致误过滤，
    # 不放宽会漏检；放宽后必须命中，且要带上放宽标记，让模型知道自己的税种判断可能错了
    relaxed = search_regulation.invoke({"query": "常设机构认定", "tax_category": "企业所得税"})
    assert relaxed["status"] == "OK", relaxed
    assert relaxed["hits"][0]["clause_id"] == "AD-TA-General-00401", relaxed["hits"][0]
    assert relaxed["applied_filters"]["tax_category"] is None, relaxed
    assert "relaxed_filter" in relaxed, relaxed

    # 真正查无此物：放宽税种也不该无中生有，也不该凭空冒出 historical_candidates
    dead = search_regulation.invoke({"query": "遗产税起征点"})
    assert dead["status"] == "NO_EVIDENCE" and "historical_candidates" not in dead, dead
    assert search_regulation.invoke({"query": "遗产税起征点", "tax_category": "企业所得税"})["status"] == "NO_EVIDENCE"

    # 时点检索：同一个 clause_id 在两个时点应当落到不同 revision
    past = search_regulation.invoke({"query": "进项税额抵扣凭证", "as_of": "2024-06-01"})
    assert past["status"] == "OK", past
    assert {(h["clause_id"], h["revision"]) for h in past["hits"]} >= {("AD-VAT-CN-00003", 1)}, past
    assert all(h["revision"] != 2 or h["clause_id"] != "AD-VAT-CN-00003" for h in past["hits"]), past
    assert past["hits"][0]["clause_status"] != "DRAFT", past

    # 取全文必须跟着同一个时点走，否则「按当年那版检索、按现行版取正文」，结论正好反过来
    past_full = fetch_clause.invoke({"clause_id": "AD-VAT-CN-00003", "as_of": "2024-06-01"})
    assert past_full["status"] == "OK" and past_full["revision"] == 1, past_full
    assert "暂不得抵扣" in past_full["content"], past_full
    assert fetch_clause.invoke({"clause_id": "AD-VAT-CN-00003"})["revision"] == 2
    # 该时点尚未生效的，要说清是"当时还没生效"，不能退回去给现行版
    not_yet = fetch_clause.invoke({"clause_id": "AD-VAT-CN-00003", "as_of": "2020-01-01"})
    assert not_yet["status"] == "NOT_FOUND" and "2020-01-01" in not_yet["message"], not_yet

    # 日期写错要给模型一条能自己改正的返回，而不是抛异常，也不能替它把"2024年3月"猜成 1 号
    for bad in ("2024-03", "2024年3月"):
        invalid = search_regulation.invoke({"query": "进项税额抵扣凭证", "as_of": bad})
        assert invalid["status"] == "INVALID_AS_OF", invalid

    # 死角：问已废止政策时默认检索返回 0，模型会回"检索不到"——事实是"检索到了，已废止"。
    # 探针必须报出历史池里有命中，但**不能**把历史条款当结果返回
    revoked = search_regulation.invoke({"query": "生产、生活性服务业进项税额加计抵减还能享受吗"})
    assert revoked["status"] == "NO_EVIDENCE", revoked
    assert revoked["historical_candidates"] >= 1, revoked
    assert revoked["hits"] == [], revoked
    # 模型按提示重查，这次才拿得到那条废止条款
    confirmed = search_regulation.invoke(
        {"query": "生产、生活性服务业进项税额加计抵减还能享受吗", "include_historical": True}
    )
    assert confirmed["status"] == "OK", confirmed
    assert any(h["clause_id"] == "AD-VAT-CN-00005" for h in confirmed["hits"]), confirmed

    # 源不支持时点检索时，转成模型能读懂的状态而不是让异常穿透工具层。
    # 必须改本模块的全局而不是 `import tax_agent.tools` 再改：直接跑这个文件时
    # __main__ 和 tax_agent.tools 是两份独立的模块对象，改后者等于没改
    global _SOURCE

    class _NoAsOf:
        def search(self, *_args, **_kwargs):
            raise NotImplementedError("该源没有生效日期字段")

        def fetch(self, *_args, **_kwargs):
            raise NotImplementedError("该源取不到历史版本正文")

    original_source = _SOURCE
    _SOURCE = _NoAsOf()
    try:
        unsupported = search_regulation.invoke({"query": "x", "as_of": "2024-06-01"})
        assert unsupported["status"] == "UNSUPPORTED_AS_OF", unsupported
        # 两个 TTC 源的 fetch 都取不到历史正文，异常不能穿透工具层炸在模型面前
        unsupported = fetch_clause.invoke({"clause_id": "AD-VAT-CN-00003", "as_of": "2024-06-01"})
        assert unsupported["status"] == "UNSUPPORTED_AS_OF", unsupported
    finally:
        _SOURCE = original_source
    print("tools self-check ok")


if __name__ == "__main__":
    _demo()
