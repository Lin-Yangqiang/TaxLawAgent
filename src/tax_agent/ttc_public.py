"""RegulationSource 的 TTC 公有 API 实现，**仅限本地开发**。

用应用级凭证（APIC/APIGW）调 TTC 公有接口 queryTtcClauseDataList，不需要用户 token，
本地自测时替代 data/clauses.json 样本，拿到真实数据跑通整条链路。

与 `ttc_client.py`（`TtcSource`，生产路径）分开成独立模块，是为了物理隔离这条边界：
公有 API **不能用于生产用户请求路径**——应用级凭证没有用户身份，维度权限无从生效；
且公有 API 不返回失效/归档标志，已废止条款会呈现为现行有效，这正是本 Agent 最该
防住的失败模式。拆开之后，将来删掉这个模块不会牵连 `TtcSource`，也不会有人在改
生产路径时误改到这个仅限本地开发的实现。
"""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path
from typing import Any, Callable

from loguru import logger

# 直接 `python src/tax_agent/ttc_public.py` 跑自检时 sys.path[0] 是本文件目录而非 src/
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tax_agent import nettls  # noqa: E402
from tax_agent.auth import AuthProvider  # noqa: E402
from tax_agent.sources import (  # noqa: E402
    EVIDENCE_FIELDS,
    SearchResult,
    best_snippet,
    check_source_contract,
    content_hash,
    relevance_score,
)

PUBLIC_PATH = "/fin/ttc/publicservices/taxRegulation/taxClause/queryTtcClauseDataList"

# ExtClauseQueryParam 是 extra="forbid"，多发一个字段整个请求会被拒。
# 内网实测通过的全集就是这 11 个，我们只用 keyWord / tlpNumber 两个。
PUBLIC_QUERY_FIELDS = frozenset({
    "keyWord", "tlpNumber", "taxJurisdictionCodeList", "taxJurisdictionProvinceCodeList",
    "regionCodeList", "taxCategoryCodeList", "taxTypeCodeList",
    "taxElementL1CodeList", "taxElementL2CodeList", "orderBy", "orderDesc",
})


def _public_project(item: dict[str, Any]) -> dict[str, Any]:
    """ExtTaxClausePageListVo -> sources.EVIDENCE_FIELDS 窄模型。

    公有 API 只返回 5 个字段（cmplTlpId/tlpNumber/tlpContent/tlpStatus/version），
    拿不到的证据字段一律填 None——EVIDENCE_FIELDS 要求键齐全，但不要求值非空，
    留 None 比编一个占位值诚实，模型看到 None 才知道这条信息缺失。
    """
    tlp_number = item["tlpNumber"]
    return {
        "clause_id": tlp_number,
        "tlp_number": tlp_number,
        "title": None,
        "tax_category": None,
        "jurisdiction": None,
        # 只有 tlpStatus 可用：archiveFlag / effectiveState / effectiveTo 公有 API 都不返回，
        # 所以 SUPERSEDED 和 REVOKED 在这个源下根本判不出来，见类 docstring 的警告
        "clause_status": "PUBLISHED" if item.get("tlpStatus") == "RELEASED" else "DRAFT",
        "revision": item["version"],
        "effective_from": None,
        "effective_to": None,
        "content_hash": content_hash(item["tlpContent"]),
    }


def _public_unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """校验公有 API 的响应形状，失败响应转成带 tracerId 的异常。

    成功是 `{"pageVO": {...}, "result": [...]}`；失败 HTTP 仍是 200，body 变成
    TaxRuleFaultVO（status/errorCode/message/tracerId）。判别方式与旧版实测代码一致：
    既无 pageVO 又无 result 就当失败响应。

    Raises:
        RuntimeError: 失败响应，或既非成功形状也非已知失败形状。
    """
    if "pageVO" in payload or "result" in payload:
        return payload
    error_code = payload.get("errorCode", payload.get("code"))
    raise RuntimeError(
        f"TTC 公有 API 返回失败：errorCode={error_code} message={payload.get('message')} "
        f"tracerId={payload.get('tracerId')}"
    )


class TtcPublicSource:
    """RegulationSource 的 TTC 公有 API 实现，**仅限本地开发**。

    用途：本地自测时用应用级凭证（APIC / APIGW）拿 TTC 真实数据，替代
    data/clauses.json 样本。走公有接口 queryTtcClauseDataList，不需要用户 token。

    **不能用于生产用户请求路径**，两个原因：

    1. 应用级凭证没有用户身份，TTC 侧解析不到 x-jalor-userAccount，维度权限无从生效；
    2. ExtTaxClausePageListVo 只有 5 个字段，**判不出条款是否已失效或被新版替代**——
       archiveFlag / effectiveState / effectiveTo 一个都不返回。也就是说
       **一条已废止的条款在这个源下看起来和现行条款完全一样**。拿它作答会给出过期结论，
       这正是本 Agent 最该防住的失败模式。

    3. 没有生效日期就没有时点检索，`as_of` 在这个源下只能直接拒绝（抛 NotImplementedError）。

    ponytail: 失效状态不可判定、无标题/税种/生效日期、时点检索不可用，是公有 API
    字段集的硬上限。升级路径是换回私有接口（TtcSource + 透传用户 token），
    不是在这里补猜测逻辑。

    Args:
        base_url: TTC 服务根地址（不含公有 API 路径段）。
        auth: 请求凭证提供者，本地开发传 ApicProvider 或 ApigwProvider。
        timeout: 单次请求超时秒数，默认 30 秒，与 TtcSource 同一依据。
        post: 可注入的传输层，签名 `post(url, body, headers) -> dict`；离线契约测试
            用假实现替换，默认才真正发 HTTP 请求。
    """

    def __init__(
        self,
        base_url: str,
        auth: AuthProvider,
        timeout: float = 30.0,
        post: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._post = post or partial(nettls.post, timeout=timeout)

    def search(
        self,
        query: str,
        tax_category: str | None,
        include_historical: bool,
        limit: int,
        as_of: str | None = None,
    ) -> SearchResult:
        if as_of is not None:
            # 这里不能照 tax_category 那样"警告后忽略"。忽略税种过滤的后果是结果过宽，
            # 模型还看得见多出来的条款；忽略 as_of 的后果是拿今天的条款冒充当年的条款，
            # 从返回值里完全看不出来——那正是本 Agent 最该防住的失败模式。
            raise NotImplementedError(
                "公有 API 的 ExtTaxClausePageListVo 不返回 effectiveFrom/effectiveTo，"
                "无法按时点检索；时点问题需要私有接口（TAX_AGENT_SOURCE=ttc_user）"
            )
        if tax_category is not None:
            # 响应里没有 taxCategoryName，客户端过滤无从下手。照常返回而不是静默丢弃：
            # 假装过滤了会让上层以为结果已经收窄，漏检和编造一样有害。
            logger.warning(
                "公有 API 返回里没有税种字段，tax_category={!r} 过滤被忽略", tax_category
            )
        # include_historical 无差异化行为：判不出历史版本，无从"额外放出"。
        # DRAFT 仍然在任何开关下都不返回——草稿不是法规。
        logger.warning(
            "TtcPublicSource 仅限本地开发：公有 API 不返回 archiveFlag/effectiveState/"
            "effectiveTo，已废止与被替代的条款无法识别，全部呈现为 PUBLISHED"
        )

        page_size = min(limit * 10, 100)
        url = f"{self._base_url}{PUBLIC_PATH}/page/{page_size}/1"
        payload = self._post(url, {"keyWord": query}, self._auth.headers())
        vo = _public_unwrap(payload)

        scored: list[tuple[float, dict[str, Any]]] = []
        filtered_out = 0
        for item in vo.get("result") or []:
            evidence = _public_project(item)
            if evidence["clause_status"] != "PUBLISHED":
                filtered_out += 1
                continue
            # 没有标题可打分，只能按正文算；本地重排的理由与 TtcSource 同源——
            # 公有 API 同样不返回相关性分，取前 N 条与"相关"无关
            s = relevance_score(query, "", item["tlpContent"])
            scored.append(
                (s, {**evidence, "score": s, "snippet": best_snippet(query, item["tlpContent"])})
            )

        scored.sort(key=lambda p: p[0], reverse=True)
        hits = [h for _, h in scored[:limit]]
        total_candidates = (vo.get("pageVO") or {}).get("totalRows", len(scored))

        logger.info(
            "TtcPublicSource.search query={!r} 命中={} totalRows={} 过滤掉={}",
            query, len(hits), total_candidates, filtered_out,
        )
        return SearchResult(hits, total_candidates)

    def fetch(self, clause_id: str, as_of: str | None = None) -> dict[str, Any] | None:
        if as_of is not None:
            raise NotImplementedError(
                "公有 API 不返回生效日期，无法判定哪一版在该时点有效；"
                "时点问题需要私有接口（TAX_AGENT_SOURCE=ttc_user）"
            )
        url = f"{self._base_url}{PUBLIC_PATH}/page/100/1"
        payload = self._post(url, {"tlpNumber": clause_id}, self._auth.headers())
        vo = _public_unwrap(payload)
        # 公有 API 没有"只取最新已发布版"的开关（releaseFlag/operationType 是私有接口的），
        # 按 tlpNumber 查会拿到全部版本，自己取 version 最大的，对齐 LocalJsonSource.fetch
        items = [i for i in (vo.get("result") or []) if i["tlpNumber"] == clause_id]
        if not items:
            logger.warning("TtcPublicSource.fetch NOT_FOUND：clause_id={!r}", clause_id)
            return None
        item = max(items, key=lambda i: i["version"])
        logger.info("TtcPublicSource.fetch 命中：clause_id={!r}", clause_id)
        return {**_public_project(item), "content": item["tlpContent"]}

    # 同样不实现 known_clause_ids：无法枚举整个法规库


def _demo() -> None:
    released = {
        "cmplTlpId": 1001,
        "tlpNumber": "AD-VAT-CN-00001",
        "tlpStatus": "RELEASED",
        "version": 1,
        "tlpContent": "境内单位向境外单位提供的跨境研发服务，同时满足服务在境外消费、"
        "签订书面合同等条件的，适用增值税零税率。",
    }
    released_v3 = {
        **released,
        "cmplTlpId": 1002,
        "version": 3,
        "tlpContent": "境内单位向境外单位提供的跨境研发服务零税率第三版正文。",
    }
    draft = {
        "cmplTlpId": 1003,
        "tlpNumber": "AD-VAT-CN-00099",
        "tlpStatus": "DRAFT",
        "version": 1,
        "tlpContent": "本条款仍在起草阶段，尚未生效。",
    }
    unrelated = {
        "cmplTlpId": 1004,
        "tlpNumber": "AD-CIT-CN-00500",
        "tlpStatus": "RELEASED",
        "version": 1,
        "tlpContent": "纳税人子女教育、继续教育等六项专项附加扣除按规定标准执行。",
    }
    # 无关条文放在最前：公有 API 也不给相关性分，不本地重排就会把它当第一名
    all_items = [unrelated, released, draft, released_v3]

    calls: list[dict[str, Any]] = []

    def fake_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        calls.append({"url": url, "body": body, "headers": headers})
        matched = (
            [i for i in all_items if i["tlpNumber"] == body["tlpNumber"]]
            if "tlpNumber" in body
            else all_items
        )
        return {
            "pageVO": {"totalRows": len(matched), "curPage": 1, "pageSize": 100},
            "result": matched,
        }

    class _FixedAuth:
        def headers(self) -> dict[str, str]:
            return {"X-HW-ID": "hw-id-for-test", "X-HW-APPKEY": "hw-appkey-for-test"}

    source = TtcPublicSource("https://ttc.internal", _FixedAuth(), post=fake_post)

    hits = source.search("跨境研发服务零税率", None, False, 5).hits
    assert "AD-VAT-CN-00099" not in {h["clause_id"] for h in hits}, hits  # DRAFT 永不出现
    assert hits[0]["clause_id"] == "AD-VAT-CN-00001", hits  # 本地重排把无关条文压下去
    # 拿不到的证据字段留 None 而不是编占位值，但键必须齐全
    assert EVIDENCE_FIELDS <= set(hits[0]), hits[0]
    assert hits[0]["title"] is None and hits[0]["effective_to"] is None, hits[0]

    # 请求体只含非 None 字段，且用的是 keyWord（大写 W）——ExtClauseQueryParam 是 extra=forbid
    assert calls[0]["body"] == {"keyWord": "跨境研发服务零税率"}, calls[0]
    assert set(calls[0]["body"]) <= PUBLIC_QUERY_FIELDS, calls[0]
    assert calls[0]["url"] == f"https://ttc.internal{PUBLIC_PATH}/page/50/1", calls[0]
    assert all(c["headers"] == _FixedAuth().headers() for c in calls), calls

    # 税种过滤在此源下被忽略，但不能因此清空结果
    assert source.search("跨境研发服务零税率", "增值税", False, 5).hits, "税种过滤不应清空结果"

    # as_of 则相反：没有日期字段就必须拒绝，不能像 tax_category 那样忽略后照常返回
    try:
        source.search("跨境研发服务零税率", None, False, 5, "2024-06-01")
        raise AssertionError("公有 API 没有生效日期，as_of 必须显式拒绝")
    except NotImplementedError as exc:
        assert "ttc_user" in str(exc), exc

    # 同一 tlpNumber 多版本时取 version 最大的一条
    full = source.fetch("AD-VAT-CN-00001")
    assert full["revision"] == 3, full
    assert full["content_hash"] == content_hash(full["content"]), full
    assert source.fetch("AD-VAT-CN-99999") is None

    # TaxRuleFaultVO 失败形状：HTTP 200 但既无 pageVO 也无 result，必须抛错且带 tracerId
    def fail_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return {"status": 0, "errorCode": "TTC.0002", "message": "无权限", "tracerId": "xyz789"}

    try:
        TtcPublicSource("https://ttc.internal", _FixedAuth(), post=fail_post).search(
            "x", None, False, 5
        )
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError as exc:
        assert "无权限" in str(exc) and "xyz789" in str(exc), exc

    check_source_contract(source, "跨境研发服务零税率", "AD-VAT-CN-00001", "AD-VAT-CN-99999", "2024-06-01")
    print("ttc_public self-check ok")


if __name__ == "__main__":
    _demo()
