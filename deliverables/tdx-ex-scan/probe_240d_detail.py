"""0x240D 二段核对：三方时间戳口径 / 分片截断方向 / 聚合 m5 vs 原生 MIN_5 偏差。"""
import sys

sys.path.insert(0, r"E:\ai_codes\ai_personal_panel\tsp-fresh\backend")

from app._vendor.easy_tdx.ex.commands.get_history_bars_range import (  # noqa: E402
    GetExHistoryInstrumentBarsRangeCmd,
)
from app._vendor.easy_tdx.ex.commands.get_minute_time import (  # noqa: E402
    GetExHistoryMinuteTimeDataCmd,
)
from app._vendor.easy_tdx.ex.mac_client import MacExClient  # noqa: E402
from app._vendor.easy_tdx.mac.enums import Adjust, ExMarket, Period  # noqa: E402

HOST, PORT, TIMEOUT = "116.205.135.205", 7727, 12.0
HK = int(ExMarket.HK_MAIN_BOARD)
US = int(ExMarket.US_STOCK)


def run(factory):
    with MacExClient(host=HOST, port=PORT, timeout=TIMEOUT, auto_reconnect=False) as c:
        return c._execute(factory())


def kline(mkt, code, period, count):
    with MacExClient(host=HOST, port=PORT, timeout=TIMEOUT, auto_reconnect=False) as c:
        return c.goods_kline(mkt, code, period, start=0, count=count, adjust=Adjust.NONE)


print("=" * 72)
print("(a) 三方时间戳口径: goods_kline MIN_1 是 09:30(start) 还是 09:31(end)?")
print("=" * 72)
for label, mkt, code in [("HK 00700", HK, "00700"), ("US AAPL", US, "AAPL")]:
    m1 = kline(mkt, code, Period.MIN_1, 700)
    dts = [str(x)[:16] for x in m1["datetime"].tolist()]
    b240d = run(lambda: GetExHistoryInstrumentBarsRangeCmd(mkt, code, 20261012, 20261012))
    b240c = run(lambda: GetExHistoryMinuteTimeDataCmd(mkt, code, 20261012))
    print(f"\n--- {label}")
    print(f"    MIN_1  n={len(dts)}  first={dts[0]} last={dts[-1]}")
    print(f"    MIN_1  日期分布={ {d[:10]: sum(1 for x in dts if x[:10] == d[:10]) for d in dts} }")
    d_lab = [f"{b.hour:02d}:{b.minute:02d}" for b in b240d]
    c_lab = [f"{b.hour:02d}:{b.minute:02d}" for b in b240c]
    print(f"    240D   n={len(d_lab)} first={d_lab[0]} last={d_lab[-1]}")
    print(f"    240C   n={len(c_lab)} first={c_lab[0]} last={c_lab[-1]}")
    # MIN_1 里落在 20261012 的哪些
    today_1 = [d for d in dts if d[:10] == "2026-10-12"]
    print(f"    MIN_1 落在 10-12 的根数={len(today_1)} first={today_1[0] if today_1 else '-'}"
          f" last={today_1[-1] if today_1 else '-'}")

print()
print("=" * 72)
print("(b) 截断方向确认: 起点固定时是不是从最早往回取的")
print("=" * 72)
for label, mkt, code in [("HK 00700", HK, "00700"), ("US AAPL", US, "AAPL")]:
    for s, e, tag in [(20261008, 20261012, "5天"), (20261009, 20261012, "4天")]:
        bars = run(lambda s=s, e=e: GetExHistoryInstrumentBarsRangeCmd(mkt, code, s, e))
        cnt: dict[str, int] = {}
        for b in bars:
            k = f"{b.year:04d}-{b.month:02d}-{b.day:02d}"
            cnt[k] = cnt.get(k, 0) + 1
        ks = sorted(cnt)
        print(f"{label} {s}~{e} ({tag}): n={len(bars)} -> {[(k, cnt[k]) for k in ks]}")

print()
print("=" * 72)
print("(c) 240D 聚合 m5 vs 上游原生 MIN_5")
print("=" * 72)
for label, mkt, code in [("HK 00700", HK, "00700"), ("US AAPL", US, "AAPL")]:
    m5 = kline(mkt, code, Period.MIN_5, 700)
    b240d = run(lambda: GetExHistoryInstrumentBarsRangeCmd(mkt, code, 20261012, 20261012))
    rows5 = m5.to_dict("records")
    print(f"\n--- {label}: MIN_5 n={len(rows5)}  240D(m1) n={len(b240d)}")
    if not rows5 or not b240d:
        continue
    # 240D 顺序即会话顺序; 按 5 根一组聚合, 组内时间戳取最后一根
    def agg(grp):
        return {
            "dt": f"{grp[-1].hour:02d}:{grp[-1].minute:02d}",
            "open": grp[0].open,
            "high": max(g.high for g in grp),
            "low": min(g.low for g in grp),
            "close": grp[-1].close,
        }
    ag = [agg(b240d[i:i + 5]) for i in range(0, len(b240d) - 4, 5)]
    # MIN_5 过滤出 20261012
    day5 = [r for r in rows5 if str(r["datetime"])[:10] == "2026-10-12"]
    print(f"    MIN_5 落在 10-12 的根数={len(day5)} 聚合根数={len(ag)}")
    print(f"    MIN_5 前3=[{[(str(r['datetime'])[11:16], round(r['close'], 3)) for r in day5[:3]]}]")
    print(f"    聚合前3=[{[(a['dt'], round(a['close'], 3)) for a in ag[:3]]}]")
    n = min(len(ag), len(day5))
    if n:
        dev = {"o": [], "h": [], "l": [], "c": []}
        for i in range(n):
            nat = day5[i]
            a = ag[i]
            for k, src in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
                base = nat[src]
                if base:
                    dev[k].append(abs(a[src] - base) / base * 100)
        out = {k: (round(sum(v) / len(v), 5), round(max(v), 5)) for k, v in dev.items() if v}
        print(f"    偏差(平均%, 最大%) = {out}")
        print(f"    对齐校验: 聚合末根 dt={ag[n-1]['dt']} 原生末根 dt={str(day5[n-1]['datetime'])[11:16]}")

print("\nDONE")
