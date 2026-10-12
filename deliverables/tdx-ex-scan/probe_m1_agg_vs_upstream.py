"""验证：0x240C 的 m1 价格序列聚合成 m5 后，与上游 goods_kline MIN_5 的 OHLC 差多少。

决定"分钟K扩容"能不能真正产出蜡烛；差太多就只能当价格线用（fail-closed）。
"""
import sys

sys.path.insert(0, r"E:\ai_codes\ai_personal_panel\参考项目\stock\easy-tdx-main\src")

import importlib.metadata as _md  # noqa: E402

_orig_version = _md.version
_md.version = lambda name: "1.20.8" if name == "easy-tdx" else _orig_version(name)

from easy_tdx.ex.commands.get_minute_time import GetExHistoryMinuteTimeDataCmd  # noqa: E402
from easy_tdx.ex.mac_client import MacExClient  # noqa: E402
from easy_tdx.mac.enums import ExMarket, Period  # noqa: E402

HK = int(ExMarket.HK_MAIN_BOARD)
US = int(ExMarket.US_STOCK)


def agg(bars, n):
    """m1 close 序列 → N 分钟 OHLC（open=组内首根、close=末根、high/low=组内极值）。"""
    out = []
    for i in range(0, len(bars) - n + 1, n):
        grp = bars[i:i + n]
        px = [b.price for b in grp]
        out.append({
            "open": px[0], "high": max(px), "low": min(px), "close": px[-1],
            "n": len(px),
        })
    return out


c = MacExClient(host="116.205.135.205", port=7727, timeout=10)
c.connect()
try:
    for label, mkt, code, d in [("HK 00700 20261009", HK, "00700", 20261009),
                                ("US AAPL 20261009", US, "AAPL", 20261009)]:
        m1 = c._execute(GetExHistoryMinuteTimeDataCmd(mkt, code, d))
        up = c.goods_kline(mkt, code, Period.MIN_5, 0, 700)
        # 上游 m5 里挑出同一天的
        up_rows = [
            r for r in up.to_dict("records")
            if str(r["datetime"])[:10].replace("-", "") == str(d)
        ]
        mine = agg(m1, 5)
        print(f"=== {label} ===")
        print(f"  0x240C m1={len(m1)} → 聚合 m5={len(mine)}；上游 m5 同日={len(up_rows)}")
        k = min(len(mine), len(up_rows))
        if k == 0:
            print("  无重叠，跳过")
            continue
        # 上游 m5 顺序可能是旧→新，取最后 k 根对齐
        up_tail = up_rows[-k:]
        diffs = {"open": [], "high": [], "low": [], "close": []}
        for a, b in zip(mine[:k], up_tail):
            for f in diffs:
                ua, ub = a[f], float(b[f])
                diffs[f].append(abs(ua - ub) / ub * 100 if ub else 0.0)
        for f, arr in diffs.items():
            print(f"  {f:5s}: 平均偏差 {sum(arr)/len(arr):.3f}%  最大 {max(arr):.3f}%")
        print(f"  样本 mine[0]={mine[0]}")
        print(f"  样本 up  [0]={{'open': {float(up_tail[0]['open']):.2f}, 'high': {float(up_tail[0]['high']):.2f}, "
              f"'low': {float(up_tail[0]['low']):.2f}, 'close': {float(up_tail[0]['close']):.2f}}}")
        print(f"  时间 up[0]={up_tail[0]['datetime']}  up[-1]={up_tail[-1]['datetime']}")
        print()
finally:
    c.close()
