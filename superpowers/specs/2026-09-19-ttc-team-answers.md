# TTC 法规库团队答复（2026-09-19）

> 来源：TTC 法规库团队对 `2026-09-19-ttc-team-questions.md` 的正式答复
> 性质：**权威事实来源**。标注"代码确认"的条目是他们逐行核对 master 分支源码得出的结论，
> 优先级高于我们从公开文档推断的任何内容。标注"需环境确认"的留到 P1.4 实测。

---

## 1. 鉴权与用户身份

### 1.1 `x-jwt-ms-token` 是否绑定目标服务 —— 需环境确认，**倾向不绑定**
- Jalor 的 `JwtRequestFilter` 读 `x-jwt-gw-token` / `x-jwt-ms-token` 后**只做验签 + 用户解析，不校验 audience**。
- 结论：按不绑定处理，原样透传。联调首个请求即可证伪；若被拒再走 token exchange。
- **前提**：网关侧必须配置把 `x-jwt-ms-token` 放行到 TTC，不要被网关剥离。

### 1.2 用户标识取哪个 claim —— **别解析 JWT，认网关透传头**
- TTC 侧的用户标识来自网关透传头 **`x-jalor-userAccount`**（对应 `RequestContext.getCurrent().getUser().getUserAccount()`），
  另有 `x-jalor-userCN` / `x-jalor-uuid`。**不是** JWT payload 里的某个 claim。
- 我们原来按 `uid` → `userAccount` → `sub` 解析 JWT 是猜的，**不能用于会话归属校验**：
  TTC 认的是网关头构造的 `UserVO`，与我们从 token 解析出的字段天然可能不是同一个人。
- **以网关头为准。**

### 1.3 `account_type` 有无限制 —— 代码确认：**无限制**
- 全仓库（含 Jalor jar）没有 `account_type` 的认证授权校验。
- 框架用的是 `UserVO.userType`（`IAM_OP_SERVICE` / `IAM_SERVICE` / `Virtual`），仅作为"API 网关鉴权通道"标记，不拒绝任何账号类型。
- 接口层访问控制只有两级：`AllSystemUser`（任何已登录用户可调）和普通资源操作（`READ` 等，需权限集）。
- 注意：虚拟账号（`Virtual`）在部分链路查不到真实维度数据，不影响透传真实用户 token 的场景。

### 1.4 token 有效期与长对话 —— 代码确认：**过期/失败一律 HTTP 200，靠 body 区分**
- `GlobalExceptionHandler.toResponse` 把所有异常（含 `AuthenticationException` / `AuthorizationException`）都返回 **HTTP 200**，错误放 body。
- 失败 body 是 `TaxRuleFaultVO`：`{status: 0, errorCode, message, tracerId}`。
- token 过期通常表现为**网关层 401**（进不到 TTC）或 TTC 内 `AuthenticationException`（200 + `status=0`）。
- **无法单靠 HTTP 状态码区分"过期"和"没权限"**。建议联调时抓一份过期 token 的真实响应，把 `errorCode` 固定下来作为"请重新登录"的判定键。
- 无刷新机制，约定就是"过期即失败，由前端重新取 token 再发起"——我们的兜底是对的。

### 1.5 APIC 应用级凭证能调什么 —— 代码确认：**调不了我们用的两个接口**
- `taxClauseSearch` 标注 `@JalorOperation(code = SYSTEM)`，`queryTlpInfo` 标注 `READ`。两者都不是公有 API，**APIC 动态 token 调不通**。
- 能走 APIC 的公有 API：`queryTtcClauseDataList`、`extTaxRegulationSearch` / `extTaxClauseSearch`。
- `ExtTaxClausePageListVo` 含 `tlpNumber` / `tlpContent`（全文）/ `version` / `tlpStatus` / `cmplTlpId`，
  但**没有 `effectiveFrom` / `effectiveTo`**——字段集比 `TaxClauseVO` 小。
- **他们的建议：不要退到公有 API。** 无用户身份场景本来就不该取用户维度数据；用户场景必须透传用户 token。

### 1.6 各环境 base_url —— **需运维确认**
- 代码里只有应用上下文 `/fin/ttc`，内部 RPC 前缀 `${taxRegulationService}` = `http://${application.appId}:TTC/fin/ttc/services`。
- `publicservices` 段与 TTC 代码侧路由无关，是**网关侧映射配置**。各环境完整对外路径要问网关/运维。

---

## 2. 接口契约

### 2.1 响应包装 —— 代码确认：**一个裸、一个包**
| 接口 | 响应 |
|---|---|
| `taxClauseSearch` | **裸 `TaxClauseSearchVO`**，无 `ResultInfo` 包装 |
| `queryTlpInfo` | **`ResultInfo<TaxClauseVO>`**，有 `status` 键 |

- 我们"按有无 `status` 键分支"的判断**恰好是对的，可以保留**。
- **异常路径两类接口统一**返回 `TaxRuleFaultVO`：`{status: 0, errorCode, message, tracerId}`。
  → 即裸 VO 的接口在**失败时也会带 `status` 键**，所以 `_unwrap` 的容忍不是保险，是错误路径的必需品。

### 2.2 业务失败的 HTTP 状态码 —— 代码确认：**一律 200**
- 参数校验、业务异常、认证、授权、系统异常全部 `Response.status(200)`。
- 成功/失败只看 body：成功 `status=1`，失败 `status=0` + `errorCode`。
- **解析 body 之前不要依赖状态码**（即不要 `raise_for_status()`）。

### 2.3 日期序列化 —— 代码确认：**`yyyy-MM-dd` 字符串，GMT+8**
- `TaxClauseVO` 的 `releaseDate` / `executeDate` / `effectiveFrom` / `effectiveTo` 均带
  `@JsonFormat(pattern = "yyyy-MM-dd", timezone = "GMT+8")`。**不是毫秒时间戳。**
- `*Str` 字段是 **ES 链路特有**的填充字段；DB 链路（`queryTlpInfo`）只填 `Date` 字段，`*Str` 为 `null`。
- 我们"优先取 `*Str`、取不到回退 `Date`"的策略正确。

### 2.4 `tlpStatus` 与 `effectiveState` —— 代码确认（**我们原来的映射是错的**）

| 判定依据 | 取值 |
|---|---|
| `tlpStatus` | **`DRAFT` / `RELEASED`，仅此两个**（没有 `ARCHIVED`） |
| `archiveFlag` | `Y` / `N`，归档状态**独立于** `tlpStatus` |
| `effectiveState` | `EXPIRING`(已失效) / `SOONTOEXPIRATION`(即将失效) / 空(现行) |
| `effectiveTo` | 无失效日 = 最大日期 `TrConstants.MAX_DATE` |

判定逻辑（`EsSearchAbility.setInvalidFlag`）：`effectiveTo` 为空按最大日期算；
`effectiveTo <= 今天` → `EXPIRING`；`effectiveTo <= 今天 + 最大临近期（数据字典配置）` → `SOONTOEXPIRATION`；否则空。

**对我们映射表的修正：**
- `SUPERSEDED`（被新版替代）：判定是 **`archiveFlag == "Y"`**，不是 `tlpStatus == "ARCHIVED"`（该值不存在）。
- `REVOKED`（已失效）：**`effectiveState == "EXPIRING"`**，与自己拿 `effectiveTo` 比今天等价；建议直接用 `effectiveState`，与 TTC 口径一致。
- `PUBLISHED`：`tlpStatus == "RELEASED"` 且 `effectiveState` 为空。默认只返回这类——正确。
- `DRAFT`：`tlpStatus == "DRAFT"`，任何开关下都不返回——正确。

**"今天还有效"的权威依据**：看 `effectiveState`，辅以 `tlpStatus == "RELEASED"` 保证是发布态。

### 2.5 `queryTlpInfo` 用 `tlpNumber` —— 代码确认：**够用**
- `tlpNumber` 是 SQL 里的精确查询条件，不必传 `cmplTlpId`。
- `releaseFlag="Y"` 的语义是"只查最新已发布"，但**只在 ES 链路生效**；
  DB 链路（`queryTlpInfo`）靠 **`operationType = "relation_tlp_info"`** 强制 `tlpStatus=RELEASED`。
- **建议传**：`{"tlpNumber": "...", "releaseFlag": "Y", "operationType": "relation_tlp_info"}`。
- 查不到时：`data` 为 `null`、**`status=1`**（成功但无数据），不会返回 `status=0`。
- `taxClauseSearch` **一定返回 `cmplTlpId`**。

### 2.6 `tlpNumber` 与 `version` —— 代码确认：**我们理解正确**
- `tlpNumber` 是身份（跨版本不变），`version` 是属性。SQL 里多处 `PARTITION BY tlp_number ORDER BY VERSION DESC`。
- **`tlpNumber` 不会被回收重分配**（软删 + 按同 tlpNumber 维护版本）。作为引用唯一标识安全。
- "被哪一版取代"用 `queryTlpHistoricalList`（按 `tlpNumber` 查全版本，`creation_date DESC`）；
  `preId` 只是链表式前驱，**不能**用于判断被谁取代。
- 编号形态：前段是业务团队/税种类型缩写，中段 `CN` / `General` 区分国家/通用。**大小写形态稳定，可放心写正则。**

### 2.7 排序与关键词 —— 代码确认：**默认不按相关性排序**
- **`taxClauseSearchESParamProcess` 显式设 `sortField=TLP_NUMBER`、`sortOrder=ASC`。**
  即默认按**条文编号升序**，不是 ES 相关性。
- `TaxClauseSearchParam` 里也有 `sortField` / `sortOrder` 可覆盖（经 Transfer 映射为 `orderBy`/`orderDesc`）——**需联调实测**。
- ES 原始实体有 `es_score`，但 **MapStruct 没把它映射进 `TaxClauseVO`**，对外响应里没有相关性分。
- → **取前 N 条在默认排序下是"按编号顺序"，不是"按相关度"。**"不自己编分数"的纪律对，但取前 N 条的做法与相关性无关。
- `keyword` → ES 的 `search_txt`（全文检索）；`searchValueTerm` → 内容精确搜索（精确短语匹配）。日常用 `keyword` 正确。
- `curPage` 从 **1** 开始；`pageSize` 代码层无上限，但**建议不超过 100**（ES 端按 100 分片串行拉取，超大 pageSize 会多次串行调用）。

---

## 3. 数据字典

### 3.1 `taxCategoryCode` 权威枚举（`TrConstants.java`，源 `tc_tax_category_t`）

| 编码 | 含义 | 编码 | 含义 |
|---|---|---|---|
| `CIT` | 所得税（企业所得税） | `DST` | 数字服务税 |
| `WHT` | 代扣所得税（预提所得税） | `IIR` | 数字服务税（IIR） |
| `ITX` | **间接税（增值税属此类）** | `DMTT` | 数字服务税（DMTT） |
| `MISC` | 小税种 | `PE` | PE |

- **`VAT` 与 `ITX` 的关系**：`ITX` 是**税种类型**（`taxCategoryCode`，间接税大类）；
  `VAT` 是**税种**（`taxTypeCode`，二级），归属于 `ITX`。
  条文号 `AD-ITX-CN-xxxxx` 前缀是税种类型，`AD-VAT-CN-xxxxx` 前缀是税种——**两种都真实存在，不冲突**。
- 个人所得税 / 印花税 / 关税：代码常量里没有对应 `taxCategoryCode`。个人所得税可能归 `CIT` 或 `MISC`，
  印花税/关税大概率归 `MISC`。**需用 `getAllTaxCategory` 字典接口确认全集。**

### 3.2 字典接口 —— 有，但在 `trcFoundationService`（另一个服务）

| 需要 | 接口 | 路径 |
|---|---|---|
| 税种类型全集 | `getAllTaxCategory` | `GET taxFoundation/taxBaseData/getAllTaxCategory` |
| 税种类型详情 | `getCategoryInfo` | `GET .../getCategoryInfo/{taxCategoryCode}` |
| 税种列表 | `getTaxTypes` | `GET .../getTaxTypes` |
| 税种+类型关联 | `getCategoryAndType` | `POST .../getCategoryAndType` |
| 税种详情 | `getTaxTypeInfo` | `GET .../getTaxTypeInfo/{taxTypeCode}` |
| 国家 | `country` | `POST /fin/ttc/services/taxRegulation/BaseData/taxRegulation/common/country` |
| 区域 | `region` | `POST .../taxRegulation/common/region` |
| 省/市/区县 | `province`/`city`/`county` | `BaseDataController` |

- `articleType` / `regulationLevel` / `issueOrgType` 走 lookup 值集（`findLookupList` 系），分类码联调时给。
- **注意**：这些是 `trcFoundationService` 的内部 RPC 映射，对我们是**另一个服务**的接口，需要网关开放；
  或用 `taxClauseSearch` 返回里的 `taxCategoryName`（已翻译中文名）兜底。
- 他们对我们"本地按名称过滤"的评价：可继续用，但**只能过滤当前页，翻页/排序场景会漏**；正式方案建议调字典接口 + 传 `taxCategoryCodeList`。

---

## 4. 调用约束 —— 需运维确认

- **限流**：代码层无 QPS 限制；网关层配额要问运维。我们单问题 2–5 次调用远低于告警线。
- **超时**：**10 秒偏紧，建议 30 秒**（ES 链路大 pageSize 会多次串行调 ES）。
- **联调数据**：稳定的 `tlpNumber` 样本需运维/数据组给；`queryTlpHistoricalList` 可用来找多版本编号做版本回归。

---

## 5. 只读侧数据可见性 —— **确认属实，已立项修复**

我们的代码走读结论**完全正确**，他们逐行确认：

- `taxClauseSearch` 链路（`taxClauseSearchESParamProcess`）确实**没有调用 `getDimensionByType`**，
  入参 `taxJurisdictionCodeList` 原样透传 ES。
- 对比 `queryTlpListByEs` 链路（`invokeClauseEsApi`）**有完整维度过滤**（`setJuDimension` 交集、无权限返回空、`@ALLCONDITION@` 全量判断）。

**关于我们"6 维度主要管编辑权"的理解：不准确。** 维度权限读写两侧都有作用：
- 写入/明细侧：`DimensionPermissionAbility` 强校验；
- 列表读取侧：**部分接口有**维度过滤（`queryTlpListByEs`、`taxRegulationSearch`），
  **部分没有**（`taxClauseSearch`、`extTaxRegulationSearch`、`queryTtcClauseDataList`）。

→ **读取侧本来就该做维度过滤，所以这个问题比我们判断的严重性要高。** 已确认缺失，修复方案是参考
`invokeClauseEsApi` 在 `taxClauseSearchESParamProcess` 注入同样的 `setJuDimension`。

我们透传用户身份的方案会让修复自动生效，无需改我们的代码。**修复上线前，已发布条文的越权读取风险确实存在。**

---

## 6. 他们全部接受的"我们不需要你们做的事"

不开新接口、不给密钥、不导数据、不改鉴权。

---

## 7. 仍需运维/环境确认的清单

1. 各环境（SIT/UAT/生产）的 `base_url` 与完整对外路径
2. 网关限流配额
3. 联调账号与稳定测试数据（一批 `tlpNumber`）
4. **过期 token 的真实响应样例**（用来固定 `errorCode` 作为"请重新登录"的判定键）
5. 两份真实响应 JSON 样例（他们会补，脱敏）
6. `taxClauseSearch` 传 `sortField` / `sortOrder` 能否覆盖默认的编号升序
