"""落地前的事实核对：0x240D(GetExHistoryInstrumentBarsRangeCmd) 到底给什么。

要回答的问题(H ->  硬必须)：
H1 港美股能不能返回数据？ n=?
H2 粒度是什么 —— 分钟级 还是 日级？同一天返回多少根？
H3 start_date/end_date 是不是 YYYYMMDD int？
H4 有没有真实 OHLC(开高低 != 收盘)？
H5 与 0x240C 的 price 序列是否一致(closure)？与 goods_kline MIN_5 呢？
H6 单次上限多少根(记忆说 1500)？跨多天怎么截断？
H7 未来日期会不会也有 price=0 填充包(0x240C 那个坑)？
H8 美股会话跨零点时, date 标签怎么打？
"""
import sys

sys.path.insert(0, r"E:\ai_codes\ai_personal_panel\tsp-fresh\backend")

from app._vendor.easy_tdx.ex.commands.get_history_bars_range import (  # noqa: E402
    GetExHistoryInstrumentBarsRangeCmd,
)
from app._vendor.easy_tdx.ex.commands.get_minute_time import (  # noqa: E402
    GetExHistoryMinuteTimeDataCmd,
)
from app._vendor.easy_tdx.ex.commands.login import MacExLoginCmd  # noqa: E402
from app._vendor.easy_tdx.ex.transport.sync import ExTdxConnection  # noqa: E402
from app._vendor.easy_tdx.mac.enums import ExMarket, Period  # noqa: E402

HOST, PORT, TIMEOUT = "116.205.135.205", 7727, 10.0
HK = int(ExMarket.HK_MAIN_BOARD)
US = int(ExMarket.US_STOCK)


def run(cmd_factory):
    """每条命令独立建连 + login(EX 对畸形请求直接断 TCP, 不能复用连接)。"""
    c = ExTdxConnection(host=HOST, port=PORT, timeout=TIMEOUT, mac_ex_mode=True)
    c.connect()
    try:
        c.execute(MacExLoginCmd())
        return c.execute(cmd_factory())
    finally:
        try:
            c.close()
        except Exception:
            pass


def fmt(b):
    return f"{b.year:04d}-{b.month:02d}-{b.day:02d} {b.hour:02d}:{b.minute:02d}"


print("=" * 70)
print("H1/H2/H3/H4: 单日 range, 看粒度与 OHLC")
print("=" * 70)
for label, mkt, code in [("HK 00700", HK, "00700"), ("US AAPL", US, "AAPL")]:
    for d in (20261012,):
        bars = run(lambda d=d: GetExHistoryInstrumentBarsRangeCmd(mkt, code, d, d))
        n = len(bars)
        print(f"\n--- {label} date={d}: n={n}")
        if not n:
            continue
        print(f"    first={fmt(bars[0])}  last={fmt(bars[-1])}")
        # 日期分布
        seen: dict[str, int] = {}
        for b in bars:
            k = f"{b.year:04d}-{b.month:02d}-{b.day:02d}"
            seen[k] = seen.get(k, 0) + 1
        print(f"    dates={seen}")
        # OHLC 是否有真实幅度
        diff = [b for b in bars if not (b.high == b.low == b.open == b.close)]
        print(f"    真实OHLC根数={len(diff)}/{n}")
        for r in bars[:3] + bars[-2:]:
            print(f"    {fmt(r)} o={r.open!r} h={r.high!r} l={r.low!r} c={r.close!r} "
                  f"vol={r.trade} amt={r.amount!r} pos={r.position}")

print()
print("=" * 70)
print("H5: 0x240D vs 0x240C vs goods_kline MIN_5 —— 同一分钟三种口径")
print("=" * 70)
for label, mkt, code in [("HK 00700", HK, "00700"), ("US AAPL", US, "AAPL")]:
    bars240d = run(lambda: GetExHistoryInstrumentBarsRangeCmd(mkt, code, 20261012, 20261012))
    bars240c = run(lambda: GetExHistoryMinuteTimeDataCmd(mkt, code, 20261012))
    print(f"\n--- {label}: 240D n={len(bars240d)}  240C n={len(bars240c)}")
    m24 = {f"{b.hour % 24:02d}:{b.minute:02d}": b for b in bars240d}
    print(f"    240D 时刻样本: {sorted(m24)[:5]} ... {sorted(m24)[-3:]}" if m24 else "    240D 空")
    if bars240c:
        print(f"    240C 时刻样本: {[f'{b.hour % 24:02d}:{b.minute:02d}' for b in bars240c[:5]]}"
              f" ... {[f'{b.hour % 24:02d}:{b.minute:02d}' for b in bars240c[-3:]]}")
    if not m24 or not bars240c:
        continue
    # 按 240C 的时刻去对齐比对 close vs price
    hit = miss = 0
    worst = 0.0
    pairs = []
    mapped = {}
    for b in bars240c:
        mapped[f"{b.hour % 24:02d}:{b.minute:02d}"] = b
    for k, b240d in m24.items():
        b240c = mapped.get(k)
        if b240c is None:
            miss += 1
            continue
        hit += 1
        p = b240c.price
        rel = abs(b240d.close - p) / p * 100 if p else 0.0
        worst = max(worst, rel)
        if len(pairs) < 3:
            pairs.append((k, b240d.close, p, rel))
    print(f"    240D时刻 ⊂ 240C时刻: hit={hit} miss={miss} 最大相对偏差={worst:.4f}%")
    for k, c240d, p240c, rel in pairs:
        print(f"      {k}: 240D.close={c240d!r} 240C.price={p240c!r} diff={rel:.4f}%")

print()
print("=" * 70)
print("H6: 单次上限 —— 跨多天 range 看截断")
print("=" * 70)
for label, mkt, code in [("HK 00700", HK, "00700"), ("US AAPL", US, "AAPL")]:
    for s, e, tag in [(20261001, 20261013, "13天"), (20260901, 20261013, "43天"),
                      (20250101, 20261013, "跨21个月")]:
        bars = run(lambda s=s, e=e: GetExHistoryInstrumentBarsRangeCmd(mkt, code, s, e))
        seen: dict[str, int] = {}
        for b in bars:
            k = f"{b.year:04d}-{b.month:02d}-{b.day:02d}"
            seen[k] = seen.get(k, 0) + 1
        print(f"{label} {s}~{e}({tag}): n={len(bars)} dates={len(seen)}")
        if seen:
            ks = sorted(seen)
            print(f"    首={ks[0]}({seen[ks[0]]}根) 末={ks[-1]}({seen[ks[-1]]}根)")

print()
print("=" * 70)
print("H7: 未来日期是否回全 0 填充包 (0x240C 那个坑)")
print("=" * 70)
from datetime import date, timedelta  # noqa: E402

today = date.today()
future = today + timedelta(days=1)
t = today.strftime("%Y%m%d")
f = future.strftime("%Y%m%d")
print(f"today={today} future={future}")
for label, mkt, code in [("HK 00700", HK, "00700"), ("US AAPL", US, "AAPL")]:
    for d, tag in [(int(t), "今日"), (int(f), "明日")]:
        bars = run(lambda d=d: GetExHistoryInstrumentBarsRangeCmd(mkt, code, d, d))
        pos = [b for b in bars if (b.close or 0) > 0]
        print(f"{label} {d}({tag}): n={len(bars)} price>0根数={len(pos)}"
              + (f"  样本={fmt(bars[0])} c={bars[0].close!r}" if bars else ""))

print()
print("=" * 70)
print("H8: 美股跨零点会话的 date/hour 标签")
print("=" * 70)
bars = run(lambda: GetExHistoryInstrumentBarsRangeCmd(US, "AAPL", 20261009, 20261009))
print(f"US AAPL 20261009 n={len(bars)}")
if bars:
    hours = sorted({b.hour for b in bars})
    print(f"    hour 取值集合={hours}")
    print(f"    first={fmt(bars[0])} last={fmt(bars[-1])}")
    zerox = [fmt(b) for b in bars if b.hour >= 24]
    print(f"    hour>=24 的根数={len(zerox)} 样本={zerox[:3]} ... {zerox[-2:] if zerox else []}")
print("\nDONE")
