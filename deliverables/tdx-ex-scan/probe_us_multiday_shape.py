"""探针: 美股多日分时的日期自洽性。

要回答三件事(决定能不能把 /multiday 的美股 fail-closed 分支换成 0x240C 按日拼):

1. 0x240C 按日循环拿到的**日期**, 是不是真的等于日K 的交易日日历?
2. 0x240C 每日**最后一根 price**, 是不是等于那一天日K 的 close?
3. 相邻两日是否满足 "较晚日的昨收 == 较早日的收盘" —— 即昨收能不能由日K 真源给出?
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app._vendor.easy_tdx.mac.enums import Period  # noqa: E402
from app.data_providers import tdx_ex_provider as prov  # noqa: E402


def main() -> None:
    for sym in ("AAPL.US", "00700.HK"):
        prov.clear_cache()
        print(f"\n{'=' * 70}\n{sym}")

        # ── 1. 日K 日历(真源基准)
        rows = prov._fetch(*prov._split(sym), Period.DAILY, 20)
        daily = []
        for r in prov._rows_from_frame(rows):
            dt = str(r.get("datetime", ""))[:10].replace("-", "")
            if dt.isdigit() and r.get("close") is not None:
                daily.append((dt, float(r["close"])))
        daily = sorted(daily)[-8:]
        print("  日K 最近交易日:", [(d, c) for d, c in daily])

        # ── 2. 0x240C 按日循环
        prov.clear_cache()
        mr = prov.minute_range(sym, days=5, period="m1")
        print("  0x240C 日序列:", [(d["date"], len(d["bars"])) for d in mr["days"]])
        print("  0x240C 每日首/末根:")
        for d in mr["days"]:
            bars = d["bars"]
            print(f"    {d['date']}  首 {bars[0]['datetime'][11:]} {bars[0]['price']}"
                  f"   末 {bars[-1]['datetime'][11:]} {bars[-1]['price']}")

        # ── 3. 交叉验证
        dmap = {d: c for d, c in daily}
        print("  逐日核对(0x240C 末根 vs 日K close):")
        ok = True
        for d in mr["days"]:
            ymd = d["date"].replace("-", "")
            bars = d["bars"]
            last = bars[-1]["price"]
            ref = dmap.get(ymd)
            if ref is None:
                print(f"    {d['date']}  ❌ 日K 日历里没有这一天")
                ok = False
                continue
            diff = abs(last - ref) / ref * 100 if ref else 0.0
            flag = "OK " if diff < 0.5 else "!! "
            if diff >= 0.5:
                ok = False
            print(f"    {flag}{d['date']}  0x240C末={last}  日Kclose={ref}  偏差 {diff:.3f}%")
        print(f"  => 日期/收盘自洽: {ok}")

        # ── 4. 昨收链: 用日K 日历推 prev_close
        print("  昨收链(日K 真源):")
        for i, d in enumerate(mr["days"]):
            ymd = d["date"].replace("-", "")
            prev = next((c for dd, c in reversed(daily) if dd < ymd), None)
            print(f"    {d['date']}  prev_close={prev}")


if __name__ == "__main__":
    main()
