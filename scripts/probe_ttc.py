"""联调冒烟脚本：用应用级凭证打一次真实 TTC 公有 API，确认链路通。

字段形状已经从内网实测版本确认过了（见 ttc_public.TtcPublicSource），所以这个脚本
不再用来探字段，只回答一个问题：**凭证 + base_url + 网关路径这一串配对了没有**。
拿到内网访问权限时跑一次，比启动整个 Agent 再猜哪一层断了快得多。

用法：
    .venv/Scripts/python.exe scripts/probe_ttc.py [关键词]

需要在 `.env`（或环境变量）里配置：
    TTC_BASE_URL              必填，TTC 服务根地址（不含公有 API 路径段）
    TTC_APIGW_ID / TTC_APIGW_APPKEY                          APIGW 静态 AK/SK（优先）
    TTC_APIC_TOKEN_URL / TTC_APIC_APP_ID / TTC_APIC_SECRET   APIC 应用级凭证（次选）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

# 复用 config.py 的 .env 解析，不为一个冒烟脚本引 python-dotenv
from tax_agent.config import load_dotenv  # noqa: E402

DEFAULT_QUERY = "增值税"


def main() -> None:
    load_dotenv()

    import os

    if not os.getenv("TTC_BASE_URL"):
        print("未设置 TTC_BASE_URL，无法探测。")
        sys.exit(1)

    # 选源逻辑与 Agent 运行时共用，避免"脚本能通但 Agent 不通"这种假阳性
    os.environ["TAX_AGENT_SOURCE"] = "ttc_public"
    # tools 模块在 import 时就建源（_SOURCE = _build_source()），所以 import 本身
    # 就会因缺凭证而抛错，try 要把 import 一起包住
    try:
        from tax_agent.tools import _build_source

        source = _build_source()
    except (KeyError, ValueError) as exc:
        # 异常消息里只有变量名，没有值；不打 traceback，配错变量不是 bug
        print(f"凭证配置不完整：{exc}")
        sys.exit(1)
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    result = source.search(query, None, False, 5)

    print(f"\n查询：{query!r}")
    print(f"totalRows={result.total_candidates} 本地重排后返回={len(result.hits)}")
    if not result.hits:
        print("没有命中。若 totalRows 也是 0，说明关键词在该环境无数据，不是链路问题。")
        return
    print("\n第一条证据：")
    for key, value in sorted(result.hits[0].items()):
        # 正文片段可能很长，截断；条款正文全文不进日志也不整条打印
        shown = str(value)
        print(f"  {key} = {shown[:120] + '…' if len(shown) > 120 else shown}")


if __name__ == "__main__":
    main()
