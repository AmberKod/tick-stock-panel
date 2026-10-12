"""按日循环 0x240C：验证能否把分钟序列拉长到任意天数（突破单次 700 根）。"""
import sys

sys.path.insert(0, r"E:\ai_codes\ai_personal_panel\参考项目\stock\easy-tdx-main\src")

import importlib.metadata as _md  # noqa: E402

_orig_version = _md.version
_md.version = lambda name: "1.20.8" if name == "easy-tdx" else _orig_version(name)

from easy_tdx.ex.commands.get_minute_time import GetExHistoryMinuteTimeDataCmd  # noqa: E402
from easy_tdx.ex.mac_client import MacExClient  # noqa: E402
from easy_tdx.mac.enums import ExMarket  # noqa: E402

HK = int(ExMarket.HK_MAIN_BOARD)
US = int(ExMarket.US_STOCK)
DAYS = [20261005, 20261006, 20261007, 20261008, 20261009, 20261012]

c = MacExClient(host="116.205.135.205", port=7727, timeout=10)
c.connect()
try:
    for label, mkt, code in [("HK 00700", HK, "00700"), ("US AAPL", US, "AAPL")]:
        total = 0
        print(f"--- {label} 按日循环 0x240C ---")
        for d in DAYS:
            try:
                bars = c._execute(GetExHistoryMinuteTimeDataCmd(mkt, code, d))
            except Exception as exc:  # noqa: BLE001
                print(f"  {d}: EXC {type(exc).__name__}: {exc}")
                continue
            if not bars:
                print(f"  {d}: 0 根（非交易日/无数据）")
                continue
            first, last = bars[0], bars[-1]
            total += len(bars)
            print(
                f"  {d}: {len(bars):4d} 根  {first.hour:02d}:{first.minute:02d}"
                f" -> {last.hour:02d}:{last.minute:02d}"
                f"  first px={first.price:.2f} last px={last.price:.2f}"
                f"  vol_first={first.volume} vol_last={last.volume}"
            )
        print(f"  合计 {total} 根（单次 goods_kline 上限是 700）")
finally:
    c.close()
