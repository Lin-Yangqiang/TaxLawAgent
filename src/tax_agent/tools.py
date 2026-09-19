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

from tax_agent.sources import LocalJsonSource, RegulationSource, content_hash  # noqa: E402

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


@tool
def search_regulation(
    query: str,
    tax_category: str | None = None,
    include_historical: bool = False,
    limit: int = 5,
) -> dict[str, Any]:
    """按自然语言描述检索税法条款，返回条款摘要与证据引用。

    Args:
        query: 检索内容，用业务语言描述，例如"跨境研发服务零税率的条件"。
        tax_category: 可选税种过滤，如"增值税"、"企业所得税"、"个人所得税"、"印花税"。
        include_historical: 是否包含已废止（REVOKED）和已被替代（SUPERSEDED）的历史版本，默认 False。
        limit: 返回条数上限，默认 5。

    Returns:
        status 为 OK 或 NO_EVIDENCE；hits 中每项含 clause_id、snippet、score 和证据字段。
        snippet 是片段，需要完整条款正文时用 clause_id 调用 fetch_clause。
    """
    result = _SOURCE.search(query, tax_category, include_historical, limit)
    if not result.hits and tax_category is not None:
        # 模型对税种的判断可能是错的（如把"税收协定"条款误判为"企业所得税"而过滤掉），
        # 漏检和编造一样有害，所以去掉税种过滤重试一次，而不是指望模型读懂 NO_EVIDENCE 里的提示
        relaxed = _SOURCE.search(query, None, include_historical, limit)
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
                "applied_filters": {"tax_category": None, "include_historical": include_historical},
                "relaxed_filter": f"按 tax_category={tax_category!r} 过滤后无命中，已忽略该过滤条件重新检索",
            }
    applied_filters = {"tax_category": tax_category, "include_historical": include_historical}
    logger.info("search_regulation query={!r} filters={} hits={}", query, applied_filters, len(result.hits))
    if not result.hits:
        return {
            "status": "NO_EVIDENCE",
            "hits": [],
            "applied_filters": applied_filters,
            "message": "样本库中没有匹配条款。可放宽税种过滤或换用其他表述重试。",
        }
    return {"status": "OK", "hits": result.hits, "total_candidates": result.total_candidates}


@tool
def fetch_clause(clause_id: str) -> dict[str, Any]:
    """按 clause_id 获取条款完整正文与证据信息。

    Args:
        clause_id: search_regulation 返回的条款标识，即 TTC 的 tlpNumber，如 `AD-VAT-CN-00001`。

    Returns:
        status 为 OK 或 NOT_FOUND；OK 时含完整 content、content_hash 及版本信息。
        只能引用本工具或 search_regulation 实际返回过的 clause_id，不得自行拼造。
    """
    result = _SOURCE.fetch(clause_id)
    if result is None:
        logger.warning("fetch_clause NOT_FOUND：clause_id={!r}（多半是模型拼造的条款号）", clause_id)
        return {
            "status": "NOT_FOUND",
            "clause_id": clause_id,
            "message": "该 clause_id 在法规库中不存在，不得作为依据引用。",
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

    # 真正查无此物：放宽税种也不该无中生有
    assert search_regulation.invoke({"query": "遗产税起征点"})["status"] == "NO_EVIDENCE"
    assert search_regulation.invoke({"query": "遗产税起征点", "tax_category": "企业所得税"})["status"] == "NO_EVIDENCE"
    print("tools self-check ok")


if __name__ == "__main__":
    _demo()
