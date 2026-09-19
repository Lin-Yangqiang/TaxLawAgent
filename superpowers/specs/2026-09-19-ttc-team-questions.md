# 致 TTC 法规库团队：对接问题清单

> 提出方：税法检索与解读 Agent（下称 Agent）
> 日期：2026-09-19
> 状态：草稿，联调前发出

---

## 0. 背景与我们的立场

我们在做一个税法检索与解读的 Agent，让用户用自然语言提问，由模型调用检索工具从 TTC 法规库取条文，再基于取回的条文作答，**每一句结论都必须附带真实存在、且本轮确实被检索到的 `tlpNumber` 作为引用**——答案里出现任何一个没被工具返回过的条款号，都会被我们的引用校验拦下。

Agent 目前只读，用两个接口：

| 我们的工具 | TTC 接口 | 路径 |
|---|---|---|
| `search_regulation` | `taxClauseSearch` | `POST /taxClause/taxClauseSearch/page/{pageSize}/{curPage}` |
| `fetch_clause` | `queryTaxClause` | `POST /taxClause/queryTlpInfo` |

**我们已经给自己定的纪律**（先说这些，是想让你们知道下面的问题不是在试探边界）：

- **不自建向量库 / ES 索引**。预建索引会把某一时刻某个人的权限视图固化下来，之后你们改了权限我们也不知道。检索一律实时调你们的接口。
- **不跨用户缓存检索结果**。
- **不使用自签 JWT**。我们清楚 `x-jwt-ms-token` 由 IAM SDK 用远程公钥验签，自签必然被拒；我们也不会来要签名密钥。
- **不接受"调用方声称的用户身份"**。用户是谁只认 token 里的内容，不认请求体里传来的用户名。
- **不做具体税额计算，不出具确定性合规结论**。Agent 只做"找到条文 + 解释条文说了什么"。
- 已发现的问题**不利用、不绕路、上报**（见第 5 节）。

我们已经按公开文档把适配层写完了，本地用构造的假响应跑通了契约测试。**下面每个问题，都对应我们代码里一处"文档没写清楚、只能先猜一个、并留了注释等你们确认"的地方。**每题都附了我们当前的猜测和兜底做法——如果猜对了，你们回一个"对"就行。

---

## 1. 鉴权与用户身份（最阻塞，其余问题都可以后置）

### 1.1 网关签发的 `x-jwt-ms-token` 是否绑定目标服务？

**我们的方案**：前端调 Agent 时把用户自己的 IAM token 放在 `x-jwt-ms-token` 头里带过来，Agent **原样转发**给 TTC。这样 TTC 侧看到的就是真实用户，Jalor 的维度权限照常生效，Agent 不需要任何密钥，也不存在"代用户越权"的风险。

**问题**：这个 token 里有没有 `aud`（audience）或等价的目标服务声明？如果用户拿到的 token 是面向前端应用签发的，转发给 TTC 时会不会因为受众不匹配被 IAM SDK 拒掉？

- 如果**不绑定**：我们直接就能用，这是最干净的形态。
- 如果**绑定**：我们需要知道该找谁申请一个面向 TTC 的 token，或者是否有 token 交换（token exchange）机制。

> 这一条联调时一个请求就能证伪，但答案决定我们是走透传还是退回应用级凭证，所以先问。

### 1.2 用户标识取哪个 claim？

你们的 `JwtRequestFilter` 解析出来放进 `RequestContext` 的用户标识，对应 JWT payload 里的哪个字段？

我们现在按 `uid` → `userAccount` → `sub` 的顺序取第一个非空，**只是猜的**。取错了会导致我们的会话归属校验（判断"这个会话是不是你的"）和你们侧的权限判断认的不是同一个人。

### 1.3 `account_type` 有无限制？

我们了解到 IAM SDK 验签时会检查 `account_type`。人员账号、机机账号是否都接受？有没有哪类账号即使 token 合法也会被拒？

### 1.4 token 有效期与长对话

IAM token 默认 3600 秒。Agent 的一次对话可能持续很久（用户问完一轮，隔半小时接着追问）。

- 你们侧对过期 token 的响应是什么（HTTP 状态码 + 响应体）？我们需要能和"没权限"区分开，才能给用户"请重新登录"而不是"你没有这条法规的权限"。
- 有没有刷新机制，还是约定就是"过期即失败，由前端重新取 token 再发起"？

我们当前的兜底是后者：过期就返回明确错误，不做静默重试。

### 1.5 APIC 应用级凭证能调到哪些接口？

无用户身份的场景（本地开发、后台任务），我们准备用 APIC 动态 token 兜底：`app_id + static_secret` 换动态 token，再以 `Authorization: Basic base64(appId:dynamicToken)` 调用。

**问题**：这条凭证能调 `taxClauseSearch` 和 `queryTlpInfo` 吗？还是只有 `queryTtcClauseDataList` 这类标着"公有 API"的接口可用？

如果只能调公有 API，我们需要知道 `queryTtcClauseDataList` 返回的 `ExtTaxClausePageListVo` 里有没有 `tlpContent` 全文和 `version`——这两个字段我们是必须的（前者是作答依据，后者是版本标注）。

### 1.6 各环境的完整路径与 base_url

我们知道公有 API 的路径必须带 `publicservices` 段，写成 `/fin/ttc/...` 会报 `resource group cannot be found`。

请确认：

- SIT / UAT / 生产的 `base_url` 分别是什么？
- `taxClauseSearch` 和 `queryTlpInfo` 在这三个环境下的**完整路径**（含所有前缀段）是什么？走 APIC 直连和走网关，路径是否不同？

---

## 2. 接口契约（决定我们解析层怎么写）

### 2.1 `taxClauseSearch` 的响应有没有 `ResultInfo` 包装？

REST 文档写它的响应类型是 `TaxClauseSearchVO`（裸的），但 Jalor 的惯例是统一套一层 `ResultInfo`。两种我们都写了，靠"响应里有没有 `status` 键"来分支——**但这是猜的，不该长期留着**。

**最有价值的一件事**：能否给我们**两个真实响应的 JSON 样例（脱敏即可）**，一个 `taxClauseSearch`、一个 `queryTlpInfo`？有了它，下面 2.2 到 2.7 大部分问题会自动消失。

### 2.2 业务失败时的 HTTP 状态码

`ResultInfo.status` 是 `1=成功 / 0=失败`。业务失败（比如参数不合法、无权限）时，HTTP 状态码是 200 还是 4xx/5xx？我们需要知道要不要在解析 body 之前先看状态码。

### 2.3 日期字段的序列化形态

`TaxClauseVO` 里 `effectiveFrom` / `effectiveTo` / `releaseDate` 声明是 `Date`，同时又有 `effectiveFromStr` / `releaseDateStr` 这些 ES 用的字符串变体。

- JSON 里 `effectiveFrom` 实际是**毫秒时间戳整数**还是**字符串**？如果是字符串，格式是 `yyyy-MM-dd` 还是 `yyyy-MM-dd HH:mm:ss`？
- 如果是毫秒时间戳，它代表的是**哪个时区的业务日期**？我们按 **UTC+8** 折算（假设是北京时间的业务日）。按 UTC 折算会整体差一天，这个差异会直接体现在我们给用户的"生效日期"上。
- `*Str` 字段是**任何接口**都会填充，还是只有走 ES 的接口才有？我们现在优先取 `*Str`，取不到才回退到时间戳。

### 2.4 `tlpStatus` 与 `effectiveState` 的枚举

我们从文档里看到条文状态流转是 `DRAFT → RELEASED → ARCHIVED`，而 `effectiveState`（失效状态标识）没有找到枚举值说明。

- `tlpStatus` 的**完整**取值有哪些？
- `effectiveState` 的取值是什么，语义是什么？
- **判断"这条条文今天还有效"的权威依据是哪个字段**——是 `effectiveTo` 和当天比，还是 `effectiveState`，还是两者都要看？

我们当前的映射（**请重点核对这一条**，它直接决定用户会不会拿到已失效的条文当依据）：

| 我们的状态 | 判定规则 | 对用户的影响 |
|---|---|---|
| `SUPERSEDED`（已被新版替代） | `tlpStatus == "ARCHIVED"` | 默认不返回，要显式开历史开关 |
| `REVOKED`（已失效） | `effectiveTo` 早于今天 | 同上，且答案里会显著标注 |
| `PUBLISHED`（现行有效） | `tlpStatus == "RELEASED"` 且未失效 | 默认只返回这类 |
| `DRAFT`（草稿） | 其余情况 | **任何开关下都不返回**——草稿不是法规 |

### 2.5 `queryTlpInfo` 用 `tlpNumber` 查得到吗？

我们传 `{"tlpNumber": "...", "releaseFlag": "Y"}`，期望拿到该条文**最新的已发布版本**。

- `tlpNumber` 够不够，还是必须传 `cmplTlpId`？如果必须传 ID，我们只能从搜索结果里带出来，那就要确认 `taxClauseSearch` 一定返回 `cmplTlpId`。
- `releaseFlag="Y"` 的语义是不是"只查最新已发布"？
- 查不到时，`data` 是 `null` 还是返回 `status=0`？

### 2.6 `tlpNumber` 与 `version` 的关系

我们的理解：`tlpNumber`（如 `AD-ITX-CN-00275`、`AD-TA-General-00652`）是条文的**身份**，跨版本不变；`version` 是它的**属性**。同一个 `tlpNumber` 可以有多个 `version`，通常只有最新的是 `RELEASED`，旧的是 `ARCHIVED`。

请确认，并回答：

- `tlpNumber` 会不会被回收、重新分配给另一条条文？（我们把它当作引用的唯一标识给用户看，如果会变，引用就失去意义了。）
- 要知道"这一版被哪一版取代"，应该调 `queryTlpHistoricalList`，还是看 `preId`？
- 段内混大小写（`General` vs `CN`）是稳定的吗？我们的引用校验正则要按这个形态写。

### 2.7 相关性排序与关键词字段

- `taxClauseSearch` 返回的结果**是按 ES 相关性降序排的吗**？`TaxClauseVO` 里我们没找到相关性分字段——确实没有吗？（我们不会自己编一个分数出来，那是假证据；但如果顺序不是相关性序，我们取前 N 条的做法就是错的。）
- `TaxClauseSearchParam` 里 `keyword` 和 `searchValueTerm`（精确搜索关键词）的区别是什么？分别适合什么场景？我们现在只用 `keyword`。
- `curPage` 从 1 开始，对吗？`pageSize` 有上限吗？

---

## 3. 数据字典

我们的用户说的是"增值税"、"企业所得税"这样的**中文名**，而 `TaxClauseSearchParam` 吃的是 `taxCategoryCodeList` 这样的**编码**。我们手上没有映射表。

**当前的兜底做法**：不给 TTC 传税种过滤条件，而是多取几条结果回来，在我们这边按 `taxCategoryName` 过滤。这能跑，但只在当前页内生效，会漏。

### 3.1 `taxCategoryCode` 的权威枚举

文档的一张图里我们看到：`CIT`(企业所得税) / `WHT`(预提所得税) / `VAT`、`ITX`(增值税) / `DST` / `PE` / `MISC` / `IIR` / `DMTT`。

- 这是全集吗？个人所得税、印花税、关税对应哪个编码？
- **`VAT` 和 `ITX` 是什么关系**——同一个税种的两个编码，还是分属不同体系（比如国内 vs 海外）？条文编号里出现的是 `AD-VAT-CN-xxxxx` 和 `AD-ITX-CN-xxxxx` 两种，这个差异我们没读懂。
- `taxCategoryCode`（税种类型）和 `taxTypeCode`（税种）的层级关系是什么？

### 3.2 有没有可直接调用的字典接口？

比起要一份静态表，我们更希望能调接口拿——你们加了新税种我们能自动跟上。有没有返回 `code + 中文名` 的字典接口？以下几个维度我们都需要：

- `taxCategoryCode`（税种类型）
- `taxJurisdictionCode`（税务管辖地，我们见过 `CN`，以及"通用"对应的值）
- `articleType`（文章类型）、`regulationLevel`（法规层级）、`issueOrgType`（发文机构）——这些是 `taxClauseSearch` 的筛选维度，用户的问题里可能隐含（"国务院发的"、"财政部的"）

---

## 4. 调用约束

- **限流**：有 QPS 或并发限制吗？Agent 回答一个问题通常会连续调 2–5 次检索（先搜、再取全文、有时换个说法再搜一次）。多个用户同时用的话，峰值我们心里要有数。
- **超时**：建议的客户端超时值是多少？我们现在设的 10 秒。
- **联调**：SIT/UAT 有没有可用的测试账号，以及一批稳定的测试数据（几个确定存在的 `tlpNumber`）？我们想用它们做联调后的回归。

---

## 5. 一个需要你们看一下的问题（只读侧数据可见性）

我们在读代码理解检索链路时注意到一处，**先说明：我们没有利用它，也不会绕开权限去用它，写在这里只是为了让你们知道。**

`taxClauseSearch` 的 ES 参数组装环节（`taxClauseSearchESParamProcess`）看起来没有调用维度过滤逻辑（`getDimensionByType` 一类），用户请求里传入的 `taxJurisdictionCodeList` 等筛选条件是**原样透传到 ES** 的。如果我们读得没错，效果是：任意已登录用户都可以查出自己维度权限之外的、已发布状态的条文。

对比之下，另一条链路（`invokeClauseEsApi` 里的 `setJuDimension`）是有做维度注入的。

**这只是代码走读的结论，没有在环境上验证过，也可能是我们漏看了上游的某处拦截。**建议你们核实一下；如果确实是缺失，参考 `invokeClauseEsApi` 的做法补上应该改动不大。

修复之后我们会自动继承——我们既然是透传用户身份调用，你们的权限修好了，我们这边的结果集就跟着收紧，不需要我们改代码。

另外，我们理解你们的 6 个维度（适用范围 / 税地 / 税种 / 业务团队 / 披露类型 / 法规适用范围）主要管的是**编辑权**，读取侧只用 `editFlag` 标记能否编辑。如果这个理解不对——也就是说读取侧本来就该做维度过滤——那上面这条的严重性要比我们判断的高，麻烦告知。

---

## 6. 我们**不**需要你们做的事

为了让回复的成本尽量低，明确一下：

- **不需要为我们开新接口**。现有的 `taxClauseSearch` + `queryTlpInfo` 就够了。
- **不需要给我们任何签名密钥**。我们不做自签。
- **不需要给我们导数据**。我们不建自己的索引。
- **不需要你们改鉴权**。如果 1.1 的答案是"token 不绑定目标服务"，我们什么都不用你们做。

---

## 7. 最想先拿到的三件事

如果时间有限，按这个顺序回就行：

1. **1.1 —— token 转发能不能通**（一句话即可；通不通决定我们走哪条路）
2. **2.1 —— 两个接口各一份真实响应的 JSON 样例（脱敏）**（一份样例能替我们消掉第 2 节大半问题）
3. **2.4 —— "这条条文今天还有效吗"该看哪个字段**（这一条判错，用户会拿到已失效的条文当依据，是我们最不能接受的错误）
