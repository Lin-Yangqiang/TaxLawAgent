# tax-law-agent

税法检索与解读 Agent。计划见 `superpowers/specs/2026-09-19-tax-law-agent-p1-plan.md`。

## 注释与 docstring

- 公开的类、函数、方法写 **Google 风格 docstring**：一行摘要，空行，然后按需写 `Args:` / `Returns:` / `Raises:`。
  私有小工具（`_` 开头且逻辑一眼看穿）可以只写一行摘要。
- 模块顶部写模块级 docstring，说明这个模块负责什么、以及**为什么这样划分**。
- 行内注释只写"为什么"，不写"是什么"。`# 遍历条款` 是噪声；
  `# lru_cache 挂在实例方法上会把 self 钉死在缓存里` 是下一个人需要的信息。
- 每个非平凡的取舍（阈值、降级、绕过某个坑）都要留一句注释交代来由，
  故意留下的能力上限用 `# ponytail: <上限>，<何时升级>` 标注。
- 注释和 docstring 一律中文，与代码库其余部分保持一致。

## 条款号格式

`clause_id` 就是 TTC 的 `tlpNumber` 本身，不拼版本号，形如
`AD-ITX-CN-00275`、`AD-TA-General-00652`——**段内可能混大小写**（`General`）。
`revision`（整数）是条款的独立属性，随证据一起返回，但不进 clause_id：
TTC 自己就是身份（tlpNumber）与属性（version）分开的。`data/clauses.json`
的样本必须保持同构，否则本地测不出真实形态的解析 bug。
引用校验正则在 `src/tax_agent/audit.py`。

## 日志

用 `loguru`。关键路径都要留日志，否则这个 Agent 出问题时只能看到"模型答得不对"，
看不到是检索没召回、税种过滤误杀，还是引用校验拦下了。

**绝对不许进日志的东西**：用户 token（`x-jwt-ms-token`）、模型 `api_key`、APIC secret、
条款正文全文。token 记长度或前 4 位足够定位问题，记原文等于把凭证写进日志文件。

## 协作约定

写代码派 sonnet 子 agent 做（成本），设计决策、方案取舍、代码复核自己来。
派活时把文件路径、改动点、验收标准写全，子 agent 看不到本次对话。

## 本地运行

`.venv/Scripts/python.exe`（Windows）。每个模块自带 `_demo()` 自检，
`python src/tax_agent/<模块>.py` 直接跑，不用测试框架。
