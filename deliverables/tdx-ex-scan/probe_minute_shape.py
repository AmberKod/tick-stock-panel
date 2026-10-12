"""落地前的事实核对：(1) goods_kline MIN_1 的时间戳口径 (2) 0x240C volume 是否累计。"""
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

c = MacExClient(host="116.205.135.205", port=7727, timeout=10)
c.connect()
try:
    print("=== (1) goods_kline MIN_1 时间戳口径 ===")
    for label, mkt, code in [("US AAPL", US, "AAPL"), ("HK 00700", HK, "00700")]:
        df = c.goods_kline(mkt, code, Period.MIN_1, 0, 700)
        dts = [str(x) for x in df["datetime"].tolist()]
        print(f"{label}: n={len(dts)}")
        print(f"   first={dts[0]}  last={dts[-1]}")
        # 跨零点的日期分布
        seen = {}
        for d in dts:
            seen[d[:10]] = seen.get(d[:10], 0) + 1
        print(f"   dates={seen}")
        # 找一条跨零点的样本
        for i in range(1, len(dts)):
            if dts[i - 1][:10] != dts[i][:10]:
                print(f"   跨日点: {dts[i-1]} -> {dts[i]}")
                break

    print()
    print("=== (2) 0x240C volume 语义（是否单调递增=累计） ===")
    for label, mkt, code, d in [("HK 00700 20261009", HK, "00700", 20261009),
                                ("US AAPL 20261009", US, "AAPL", 20261009)]:
        bars = c._execute(GetExHistoryMinuteTimeDataCmd(mkt, code, d))
        vols = [b.volume for b in bars]
        inc = sum(1 for i in range(1, len(vols)) if vols[i] >= vols[i - 1])
        print(f"{label}: n={len(bars)} 首={vols[0]} 末={vols[-1]} "
              f"max={max(vols)} 单调递增步数={inc}/{len(vols)-1}")
        print(f"   vol 前5={vols[:5]}")
        print(f"   vol 后5={vols[-5:]}")
finally:
    c.close()
