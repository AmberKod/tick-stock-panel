"""验收: 自建 xdxr 系数 vs 腾讯自己算的系数, 逐日比对。

腾讯那条路能用(mode 参数必须是 qfq/hfq, 不是 qfqa/hfqa), 但它复权是黑盒。
两条独立链路对同一批日期给出同一个数, 才说明"从事件推导"这条链没跑偏。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app.data_providers.tdx_xdxr_provider import adj_ratios  # noqa: E402

sys.path.insert(0, str(BACKEND / ".." / ".." / "backend"))

CODES = ["600519", "000651", "300750", "000001", "600036", "002594"]


def main() -> None:
    from app.data_providers.jiuzhang_astock_provider import adjust_ratios as tx_ratios

    for code in CODES:
        print(f"=== {code} ===")
        for mode in ("qfq", "hfq"):
            try:
                mine = adj_ratios(code, mode=mode, limit=640)["data"]["ratios"]
            except Exception as exc:  # noqa: BLE001
                print(f"  自建({mode}) 失败: {type(exc).__name__}: {exc}")
                continue
            try:
                theirs = tx_ratios(code, mode=mode, limit=640)["data"]["ratios"]
            except Exception as exc:  # noqa: BLE001
                print(f"  腾讯({mode}) 失败: {type(exc).__name__}: {exc}")
                continue

            base = {r["date"]: r["ratio"] for r in theirs}
            diffs = []
            worst = None
            for row in mine:
                b = base.get(row["date"])
                if b is None or b == 0:
                    continue
                pct = abs(row["ratio"] - b) / b * 100.0
                diffs.append(pct)
                if worst is None or pct > worst[1]:
                    worst = (row["date"], pct, row["ratio"], b)
            if not diffs:
                print(f"  {mode}: 无重叠日期")
                continue
            n = len(diffs)
            print(f"  {mode}: n={n}  mean={sum(diffs) / n:.6f}%  max={max(diffs):.6f}%"
                  f"  (>0.5% 的 {sum(1 for x in diffs if x > 0.5)} 天)"
                  f"  最差 {worst[0]} 自建={worst[2]:.6f} 腾讯={worst[3]:.6f}")
        # 事件边上看一次事实ables
        ev = adj_ratios(code, "qfq", 640)["data"]["events_used"]
        if ev:
            print(f"  区间内用到 {len(ev)} 次除权: 最近 3 次 "
                  + ", ".join(f"{e['date']}(分红{e['cash_per_share']} 送{e['bonus_share_ratio']} "
                              f"r={e['r']:.6f})" for e in ev[-3:]))
        print()


if __name__ == "__main__":
    main()
