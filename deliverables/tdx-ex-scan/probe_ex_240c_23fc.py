"""经 MacExClient（含 0x2454 登录）实测：0x23FC 港股逐笔 / 0x240C 历史分时 / 0x240D 区间K线。"""
import sys

sys.path.insert(0, r"E:\ai_codes\ai_personal_panel\参考项目\stock\easy-tdx-main\src")

import importlib.metadata as _md  # noqa: E402

_orig_version = _md.version
_md.version = lambda name: "1.20.8" if name == "easy-tdx" else _orig_version(name)

from easy_tdx.ex.commands.get_history_bars_range import GetExHistoryInstrumentBarsRangeCmd  # noqa: E402
from easy_tdx.ex.commands.get_minute_time import GetExHistoryMinuteTimeDataCmd, GetExMinuteTimeDataCmd  # noqa: E402
from easy_tdx.ex.commands.get_transaction import GetExHistoryTransactionDataCmd, GetExTransactionDataCmd  # noqa: E402
from easy_tdx.ex.mac_client import MacExClient  # noqa: E402
from easy_tdx.mac.enums import ExMarket, Period  # noqa: E402

HOSTS = ["116.205.135.205", "121.37.232.167"]
HK = int(ExMarket.HK_MAIN_BOARD)
US = int(ExMarket.US_STOCK)


def connect():
    last = None
    for h in HOSTS:
        try:
            c = MacExClient(host=h, port=7727, timeout=10)
            c.connect()
            print(f"[ok] connected+login {h}")
            return c
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[..] {h} failed: {type(exc).__name__}: {exc}")
    raise RuntimeError(f"all failed: {last}")


def probe(name, fn):
    try:
        out = fn()
        n = len(out) if out is not None else 0
        print(f"[ok] {name}: n={n}")
        if n:
            sample = out[0] if isinstance(out, list) else out.iloc[0].to_dict()
            print(f"     sample={sample}")
        return n
    except Exception as exc:  # noqa: BLE001
        print(f"[XX] {name}: {type(exc).__name__}: {exc}")
        return -1


c = connect()
try:
    probe("baseline goods_kline HK DAILY 10", lambda: c.goods_kline(HK, "00700", Period.DAILY, 0, 10))
    probe("baseline goods_kline US MIN_1 700", lambda: c.goods_kline(US, "AAPL", Period.MIN_1, 0, 700))

    print("--- 港股逐笔 0x23FC ---")
    probe("goods_transaction HK 00700", lambda: c.goods_transaction(HK, "00700", None, 0, 10))
    probe("raw 0x23FC HK", lambda: c._execute(GetExTransactionDataCmd(HK, "00700", 0, 10)))
    probe("raw 0x2406 HK 20261012", lambda: c._execute(GetExHistoryTransactionDataCmd(HK, "00700", 20261012, 0, 10)))
    probe("US 逐笔 0x122F AAPL", lambda: c.goods_transaction(US, "AAPL", None, 0, 10))

    print("--- 历史分时 0x240C / 当日 0x240B ---")
    probe("raw 0x240C HK 20261012", lambda: c._execute(GetExHistoryMinuteTimeDataCmd(HK, "00700", 20261012)))
    probe("raw 0x240C HK 20261009", lambda: c._execute(GetExHistoryMinuteTimeDataCmd(HK, "00700", 20261009)))
    probe("raw 0x240C US AAPL 20261009", lambda: c._execute(GetExHistoryMinuteTimeDataCmd(US, "AAPL", 20261009)))
    probe("raw 0x240B US AAPL today", lambda: c._execute(GetExMinuteTimeDataCmd(US, "AAPL")))

    print("--- 区间K线 0x240D ---")
    probe("raw 0x240D HK 20260101-20261012", lambda: c._execute(GetExHistoryInstrumentBarsRangeCmd(HK, "00700", 20260101, 20261012)))
    probe("raw 0x240D US AAPL 20260101-20261012", lambda: c._execute(GetExHistoryInstrumentBarsRangeCmd(US, "AAPL", 20260101, 20261012)))
finally:
    c.close()
