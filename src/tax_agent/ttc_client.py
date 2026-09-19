"""TTC（内网 Jalor 法规检索服务）适配器。

RegulationSource 协议把"从哪里取条款"与"工具怎么包装结果"分开，见 sources.py 顶部
说明。这个模块是该协议的第二个实现：把本地样本换成 TTC 的真实 HTTP 接口，search_regulation
/ fetch_clause 两个工具和 clause_id 规则不用改一个字。

TaxClauseVO 本身约 90 个字段，是 TTC 工作流内部的完整对象；这里只投影出
sources.EVIDENCE_FIELDS 定义的窄集合再往上传，一是避免把内部字段撑爆模型上下文，
二是让两个实现返回的证据形状完全一致，才谈得上"同一份契约测试"。

字段语义与响应形状以 superpowers/specs/2026-09-19-ttc-team-answers.md（TTC 团队逐行核对
源码的权威答复）为准，不再是从公开文档猜的。
"""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path
from typing import Any, Callable

import httpx
from loguru import logger

# 直接 `python src/tax_agent/ttc_client.py` 跑自检时 sys.path[0] 是本文件目录而非 src/
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tax_agent import nettls  # noqa: E402
from tax_agent.auth import AuthProvider  # noqa: E402
from tax_agent.sources import (  # noqa: E402
    EVIDENCE_FIELDS,
    SearchResult,
    check_source_contract,
    content_hash,
    covers,
    score,
    snippet,
)


def _clause_status(vo: dict[str, Any]) -> str:
    """把 TTC 的状态字段映射到我们模型的 PUBLISHED/SUPERSEDED/REVOKED/DRAFT。

    依据 TTC 团队 2026-09-19 的代码确认答复：`tlpStatus` 只有 DRAFT/RELEASED 两个取值，
    没有 ARCHIVED；归档是独立字段 `archiveFlag`（Y/N）；是否失效看 `effectiveState`
    （EXPIRING=已失效，SOONTOEXPIRATION=即将失效，空=现行）。
    `SOONTOEXPIRATION` 按 PUBLISHED 处理——条款确实还有效，具体失效日期在证据的
    effective_to 里，模型看得到，不需要我们提前拦截。
    非 RELEASED 一律归 DRAFT 而不是报错：DRAFT 在任何开关下都不作为证据返回，
    万一 TTC 将来加了新的 tlpStatus 取值，失败方向是"少给一条"而不是"拿它作答"。
    """
    if vo.get("tlpStatus") != "RELEASED":
        return "DRAFT"
    if vo.get("archiveFlag") == "Y":
        return "SUPERSEDED"
    if vo.get("effectiveState") == "EXPIRING":
        return "REVOKED"
    return "PUBLISHED"


def _date(vo: dict[str, Any], field: str) -> str | None:
    """取日期字段，优先 ES 链路填充的 `<field>Str`，回退 DB 链路（queryTlpInfo）填充的 `<field>`。

    TTC 确认所有日期字段都带 @JsonFormat(pattern="yyyy-MM-dd")，一律是字符串，
    不存在毫秒时间戳，所以这里不再需要时间戳解析分支。
    """
    as_str = vo.get(field + "Str")
    if as_str is not None:
        return as_str
    raw = vo.get(field)
    return str(raw)[:10] if raw is not None else None


def _project(vo: dict[str, Any]) -> dict[str, Any]:
    """TaxClauseVO -> sources.EVIDENCE_FIELDS 窄模型。"""
    tlp_number = vo["tlpNumber"]
    return {
        "clause_id": tlp_number,
        "tlp_number": tlp_number,
        "title": vo["articleOrRegulationTitle"],
        "tax_category": vo.get("taxCategoryName"),
        "jurisdiction": vo.get("taxJurisdictionName") or vo.get("taxJurisdictionCode"),
        "clause_status": _clause_status(vo),
        "revision": vo["version"],
        "effective_from": _date(vo, "effectiveFrom"),
        "effective_to": _date(vo, "effectiveTo"),
        "content_hash": content_hash(vo["tlpContent"]),
    }


def _unwrap(payload: dict[str, Any], *, expect_key: str) -> Any:
    """兼容 taxClauseSearch（裸 VO）与 queryTlpInfo（ResultInfo 包装）两种成功形状。

    TTC 团队代码确认：taxClauseSearch 成功时返回裸 TaxClauseSearchVO（无 status 键），
    queryTlpInfo 返回 ResultInfo<TaxClauseVO>（有 status 键）；但两者失败时统一返回
    带 status 的 TaxRuleFaultVO（status/errorCode/message/tracerId），HTTP 状态码恒为 200。
    所以裸 VO 分支对 status 键的容忍不是保险，是错误路径的必需品——没有它裸 VO 接口的
    失败响应会被当成畸形响应处理，丢掉 errorCode/tracerId。

    Args:
        payload: 原始响应体。
        expect_key: 裸 VO 成功分支下用来做形状校验的键名，避免把畸形响应当正常结果解析。
    """
    if "status" in payload:
        # 成功标志：1=成功，0=失败（含 taxClauseSearch/queryTlpInfo 的失败路径）
        if payload["status"] != 1:
            error_code = payload.get("errorCode", payload.get("code"))
            raise RuntimeError(
                f"TTC 返回失败：errorCode={error_code} message={payload.get('message')} "
                f"tracerId={payload.get('tracerId')}"
            )
        return payload["data"]
    if expect_key not in payload:
        raise RuntimeError(f"TTC 响应形状不符：既无 status 也无 {expect_key!r}")
    return payload


def _default_post(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    # TLS/代理配置集中在 nettls：内网自签证书要么喂根 CA，要么显式降级，默认不放过
    return httpx.post(
        url, json=body, headers=headers, timeout=timeout, **nettls.client_kwargs()
    ).json()


class TtcSource:
    """RegulationSource 的 TTC 实现。

    Args:
        base_url: TTC 服务根地址。
        auth: 请求凭证提供者，见 auth.AuthProvider。
        timeout: 单次请求超时秒数，默认 30 秒是 TTC 团队的建议值——ES 链路大 pageSize
            会多次串行调 ES，10 秒偏紧。
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
        # timeout 只对默认传输层有意义，绑进去而不是存成字段，免得留一个没人读的属性
        self._post = post or partial(_default_post, timeout=timeout)

    def search(
        self,
        query: str,
        tax_category: str | None,
        include_historical: bool,
        limit: int,
        as_of: str | None = None,
    ) -> SearchResult:
        # TTC 确认 taxClauseSearchESParamProcess 显式设 sortField=TLP_NUMBER、sortOrder=ASC，
        # 默认按条文编号升序返回，不是相关性序；es_score 也没有映射进 TaxClauseVO。
        # 取前 N 条 = 取编号最小的 N 条，与"相关"无关，所以要拉一个更大的候选页本地重排。
        # pageSize 建议不超过 100（超过会在 ES 端多次串行拉取）。
        page_size = min(limit * 10, 100)
        url = f"{self._base_url}/taxClause/taxClauseSearch/page/{page_size}/1"
        payload = self._post(url, {"keyword": query}, self._auth.headers())
        vo = _unwrap(payload, expect_key="pagedResult")

        # as_of 下按生效区间说话，当前状态不参与——今天已 SUPERSEDED 的那版正是当年的正确依据
        allowed = (
            {"PUBLISHED", "SUPERSEDED", "REVOKED"}
            if include_historical or as_of is not None
            else {"PUBLISHED"}
        )
        total_candidates = vo["pagedResult"]["totalCount"]
        scored: list[tuple[float, dict[str, Any]]] = []
        filtered_out = 0
        undated = 0
        for raw in vo["pagedResult"]["data"]:
            evidence = _project(raw)
            # DRAFT 永远丢弃：草稿不是法规，历史开关管的是已发布过的旧版本
            if evidence["clause_status"] not in allowed:
                filtered_out += 1
                continue
            if tax_category is not None and evidence["tax_category"] != tax_category:
                filtered_out += 1
                continue
            if as_of is not None:
                covered = covers(evidence, as_of)
                if covered is None:
                    # TTC 里日期可空。判不了单独计数往上报，不要混进 filtered_out——
                    # "我们不知道"和"当时不适用"是两个结论，只有前者需要告诉用户
                    undated += 1
                    continue
                if not covered:
                    filtered_out += 1
                    continue
            s = score(query, raw["articleOrRegulationTitle"], raw["tlpContent"])
            scored.append((s, {**evidence, "score": s, "snippet": snippet(query, raw["tlpContent"])}))

        # 不套 MIN_TOP_SCORE/MIN_HIT_SCORE 阈值：ES 已经判定这些条文相关了，我们的朴素
        # bigram 分只用来排序，拿它当门槛会把 ES 认为相关、但字面不重合的好结果误杀。
        # sorted 是稳定排序，同分保留 TTC 的编号升序，结果确定可复现。
        scored.sort(key=lambda p: p[0], reverse=True)
        hits = [h for _, h in scored[:limit]]

        logger.info(
            "TtcSource.search query={!r} as_of={} 命中={} 总候选={} 过滤掉={} 生效日缺失={}",
            query, as_of, len(hits), total_candidates, filtered_out, undated,
        )
        return SearchResult(hits, total_candidates, undated)

    def fetch(self, clause_id: str, as_of: str | None = None) -> dict[str, Any] | None:
        if as_of is not None:
            # queryTlpInfo 靠 releaseFlag=Y + operationType 锁定"最新已发布版"，没有按版本或
            # 按日期取历史正文的入参。硬着头皮不带 as_of 调，会拿回现行版正文冒充当年的条款——
            # 检索命中的是 revision 1，正文却是 revision 2，结论正好反过来且看不出错。
            # 内网 runbook 里要问清 TTC 有没有取历史版本正文的接口，有就在这里接上。
            raise NotImplementedError(
                "TTC queryTlpInfo 只返回最新已发布版本，取不到历史版本正文；"
                "时点检索的片段可作引用，但完整正文需要 TTC 提供按版本取正文的接口"
            )
        url = f"{self._base_url}/taxClause/queryTlpInfo"
        # releaseFlag="Y" 只在 ES 链路生效；DB 链路（queryTlpInfo）靠
        # operationType="relation_tlp_info" 强制 tlpStatus=RELEASED。两个都传才保证
        # 拿到最新已发布版本，对齐 LocalJsonSource.fetch 取最大 revision 的语义。
        body = {"tlpNumber": clause_id, "releaseFlag": "Y", "operationType": "relation_tlp_info"}
        payload = self._post(url, body, self._auth.headers())
        vo = _unwrap(payload, expect_key="tlpNumber")
        if vo is None:
            logger.warning("TtcSource.fetch NOT_FOUND：clause_id={!r}", clause_id)
            return None
        logger.info("TtcSource.fetch 命中：clause_id={!r}", clause_id)
        return {**_project(vo), "content": vo["tlpContent"]}

    # 故意不实现 known_clause_ids：接入 TTC 后无法枚举整个法规库，任何实现都给不出全集，
    # 与 tools.py 里 known_clause_ids() 只依赖 LocalJsonSource 的做法一致


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
        self._post = post or partial(_default_post, timeout=timeout)

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
            s = score(query, "", item["tlpContent"])
            scored.append(
                (s, {**evidence, "score": s, "snippet": snippet(query, item["tlpContent"])})
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


def _public_demo() -> None:
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


def _demo() -> None:
    released = {
        "tlpNumber": "AD-VAT-CN-00001",
        "tlpStatus": "RELEASED",
        "version": 2,
        "taxCategoryName": "增值税",
        "taxJurisdictionName": "中国",
        "articleOrRegulationTitle": "跨境应税行为适用增值税零税率和免税政策的规定",
        "tlpContent": "境内单位向境外单位提供的跨境研发服务，同时满足服务在境外消费、签订书面合同"
        "、取得境外收汇凭证等条件的，适用增值税零税率；未办理备案的按免税处理。",
        "effectiveFromStr": "2016-05-01",
        "effectiveToStr": None,
    }
    superseded = {
        "tlpNumber": "AD-TA-General-00401",
        "tlpStatus": "RELEASED",
        "archiveFlag": "Y",
        "version": 1,
        "taxCategoryName": "税收协定",
        "taxJurisdictionName": "中国",
        "articleOrRegulationTitle": "常设机构认定标准",
        "tlpContent": "常设机构是指企业进行全部或部分营业活动的固定场所。",
        "effectiveFrom": "2016-05-01",  # 非 Str 字段：验证 DB 链路只填 Date 字段时的回退路径
        "effectiveTo": None,
    }
    revoked = {
        "tlpNumber": "AD-TA-General-00402",
        "tlpStatus": "RELEASED",
        "effectiveState": "EXPIRING",
        "version": 1,
        "taxCategoryName": "税收协定",
        "taxJurisdictionName": "中国",
        "articleOrRegulationTitle": "常设机构认定标准（已失效旧版）",
        "tlpContent": "本条款已失效，不再适用。",
        "effectiveFromStr": "2010-01-01",
        "effectiveToStr": "2020-01-01",
    }
    soon_expiring = {
        "tlpNumber": "AD-TA-General-00403",
        "tlpStatus": "RELEASED",
        "effectiveState": "SOONTOEXPIRATION",
        "version": 1,
        "taxCategoryName": "税收协定",
        "taxJurisdictionName": "中国",
        "articleOrRegulationTitle": "常设机构认定标准（即将失效）",
        "tlpContent": "常设机构认定标准，即将被新版取代但目前仍然现行有效。",
        "effectiveFromStr": "2020-01-01",
        "effectiveToStr": "2026-12-31",
    }
    draft = {
        "tlpNumber": "AD-VAT-CN-00099",
        "tlpStatus": "DRAFT",
        "version": 1,
        "taxCategoryName": "增值税",
        "taxJurisdictionName": "中国",
        "articleOrRegulationTitle": "增值税征管办法（草案）",
        "tlpContent": "本条款仍在起草阶段，尚未生效。",
        "effectiveFromStr": None,
        "effectiveToStr": None,
    }
    undated_vo = {
        # effectiveFrom/effectiveTo 整个字段缺失：TTC 里日期是可空的，这是时点检索的边界情形
        "tlpNumber": "AD-TA-General-00404",
        "tlpStatus": "RELEASED",
        "version": 1,
        "taxCategoryName": "税收协定",
        "taxJurisdictionName": "中国",
        "articleOrRegulationTitle": "常设机构认定标准（未维护生效日期）",
        "tlpContent": "本条文在 TTC 中没有维护生效起始日期。",
    }
    all_vo = [released, superseded, revoked, soon_expiring, draft, undated_vo]

    calls: list[dict[str, Any]] = []

    def fake_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        calls.append({"url": url, "body": body, "headers": headers})
        if url.endswith("/taxClause/queryTlpInfo"):
            match = next((vo for vo in all_vo if vo["tlpNumber"] == body["tlpNumber"]), None)
            return {"status": 1, "data": match, "code": "0", "message": "ok"}
        # search：不做真实关键字过滤，契约测试只关心状态/税种过滤和字段投影
        return {
            "status": 1,
            "data": {"pagedResult": {"totalCount": len(all_vo), "data": all_vo}},
            "code": "0",
            "message": "ok",
        }

    class _FixedAuth:
        def headers(self) -> dict[str, str]:
            return {"x-jwt-ms-token": "fixed-token-for-test"}

    source = TtcSource("https://ttc.internal", _FixedAuth(), post=fake_post)

    # 默认只返回 PUBLISHED：released（RELEASED 无归档无失效）、soon_expiring（即将失效仍现行），
    # 以及 undated_vo——没传 as_of 时"不知道生效日"不等于"无效"，不该因此丢掉
    default_hits = source.search("跨境研发服务", None, False, 10).hits
    assert {h["clause_id"] for h in default_hits} == {
        "AD-VAT-CN-00001",
        "AD-TA-General-00403",
        "AD-TA-General-00404",
    }, default_hits

    historical_hits = source.search("跨境研发服务", None, True, 10).hits
    statuses = {h["clause_id"]: h["clause_status"] for h in historical_hits}
    assert statuses.get("AD-TA-General-00401") == "SUPERSEDED", statuses  # archiveFlag=Y，不是 tlpStatus=ARCHIVED
    assert statuses.get("AD-TA-General-00402") == "REVOKED", statuses  # effectiveState=EXPIRING
    assert statuses.get("AD-TA-General-00403") == "PUBLISHED", statuses  # SOONTOEXPIRATION 仍按现行处理
    assert "AD-VAT-CN-00099" not in statuses, statuses  # DRAFT 永远不出现

    superseded_hit = next(h for h in historical_hits if h["clause_id"] == "AD-TA-General-00401")
    assert superseded_hit["effective_from"] == "2016-05-01", superseded_hit

    # 时点检索：2018-06-01 当天有效的是 revoked 那条（2010-01-01~2020-01-01）。
    # 它今天已失效，默认检索拿不到，但当年它就是正确依据——这正是 as_of 存在的理由
    dated = source.search("常设机构认定", None, False, 10, "2018-06-01")
    dated_ids = {h["clause_id"] for h in dated.hits}
    assert "AD-TA-General-00402" in dated_ids, dated.hits
    assert "AD-TA-General-00403" not in dated_ids, dated.hits  # 2020-01-01 才生效
    assert "AD-VAT-CN-00099" not in dated_ids, dated.hits  # DRAFT 在 as_of 下同样不出现
    # 生效日缺失的那条既不算命中也不算过滤掉，单独计数报上去
    assert "AD-TA-General-00404" not in dated_ids, dated.hits
    assert dated.undated_excluded == 1, dated

    assert [h["tax_category"] for h in source.search("x", "增值税", True, 10).hits] == ["增值税"]
    # page_size = min(limit*10, 100)，与是否传 tax_category 无关
    assert calls[-1]["url"].endswith("/page/100/1"), calls[-1]
    assert source.search("x", "不存在的税种", True, 10).hits == []

    full = source.fetch("AD-VAT-CN-00001")
    assert full["content_hash"] == content_hash(full["content"]), full

    assert source.fetch("AD-VAT-CN-99999") is None

    # 真实的 TaxRuleFaultVO 失败形状：status=0 必须抛 RuntimeError，且消息里同时带 message 和 tracerId
    def fail_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return {"status": 0, "errorCode": "TTC.0001", "message": "keyword 不能为空", "tracerId": "abc123"}

    try:
        TtcSource("https://ttc.internal", _FixedAuth(), post=fail_post).search("x", None, False, 5)
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError as exc:
        assert "keyword 不能为空" in str(exc), exc
        assert "abc123" in str(exc), exc

    # 裸 TaxClauseSearchVO（没有 status 键）也要能解析
    def bare_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return {"pagedResult": {"totalCount": 1, "data": [released]}}

    bare_hits = TtcSource("https://ttc.internal", _FixedAuth(), post=bare_post).search("x", None, False, 5).hits
    assert bare_hits[0]["clause_id"] == "AD-VAT-CN-00001", bare_hits

    search_call = calls[0]
    assert search_call["url"] == "https://ttc.internal/taxClause/taxClauseSearch/page/100/1", search_call
    fetch_call = next(c for c in calls if c["url"].endswith("/taxClause/queryTlpInfo"))
    assert fetch_call["body"] == {
        "tlpNumber": "AD-VAT-CN-00001",
        "releaseFlag": "Y",
        "operationType": "relation_tlp_info",
    }, fetch_call
    assert all(c["headers"] == {"x-jwt-ms-token": "fixed-token-for-test"} for c in calls), calls

    # B3 核心断言：TTC 默认按编号升序返回，候选池第一条与查询字面几乎不相关，
    # 靠后一条标题高度重合；重排必须把它排到第一位，否则说明退回成了"取前 N 条"
    rerank_query = "跨境电子商务综合试验区增值税免税政策"
    rerank_pool = [
        {
            "tlpNumber": "AD-VAT-CN-20001",
            "tlpStatus": "RELEASED",
            "version": 1,
            "taxCategoryName": "增值税",
            "taxJurisdictionName": "中国",
            "articleOrRegulationTitle": "个人所得税专项附加扣除办法",
            "tlpContent": "纳税人子女教育、继续教育等六项支出可按规定标准扣除。",
            "effectiveFromStr": "2019-01-01",
            "effectiveToStr": None,
        },
        {
            "tlpNumber": "AD-VAT-CN-20002",
            "tlpStatus": "RELEASED",
            "version": 1,
            "taxCategoryName": "增值税",
            "taxJurisdictionName": "中国",
            "articleOrRegulationTitle": "土地增值税清算管理规程",
            "tlpContent": "房地产开发项目达到清算条件的，纳税人应在规定期限内办理清算。",
            "effectiveFromStr": "2018-01-01",
            "effectiveToStr": None,
        },
        {
            "tlpNumber": "AD-VAT-CN-20003",
            "tlpStatus": "RELEASED",
            "version": 1,
            "taxCategoryName": "增值税",
            "taxJurisdictionName": "中国",
            "articleOrRegulationTitle": "跨境电子商务综合试验区增值税免税政策",
            "tlpContent": "跨境电子商务综合试验区内符合条件的电商出口货物，适用增值税免税政策。",
            "effectiveFromStr": "2020-01-01",
            "effectiveToStr": None,
        },
    ]

    def rerank_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return {
            "status": 1,
            "data": {"pagedResult": {"totalCount": len(rerank_pool), "data": rerank_pool}},
            "code": "0",
            "message": "ok",
        }

    rerank_hits = TtcSource("https://ttc.internal", _FixedAuth(), post=rerank_post).search(
        rerank_query, None, False, 3
    ).hits
    assert rerank_hits[0]["clause_id"] == "AD-VAT-CN-20003", rerank_hits

    check_source_contract(source, "跨境研发服务", "AD-VAT-CN-00001", "AD-VAT-CN-99999", "2018-06-01")
    print("ttc_client self-check ok")


if __name__ == "__main__":
    _demo()
    _public_demo()
