"""探针: 美股 0x240C 的日期标签到底按什么口径?

现象: 北京时间 2026-10-13 00:19 打 /multiday/AAPL.US 被守卫①拦下 ——
  0x240C 最近一天是 10-13, 而日K 日历最新只有 10-12。

要回答: 同一个美股交易日(美东 10-12), 0x240C 在盘中和盘后分别用哪个 date 标签?
  - 若 date=20261012 现在能返回完整 390 根(末价 = 日K 10-12 收盘) →
    说明上游按"会话开始日期"分组, 那 10-13 那一包是另一个东西(不能要);
  - 若 date=20261012 现在返回空/短, 而 10-13 是完整 390 根 →
    说明上游按"会话结束时的北京日期"分组, 跨零点后标签会 +1 天。
结论决定 multi_day_ticks 该用"自然日倒扫"还是"日K 日历驱动"。
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app.data_providers import tdx_ex_provider as prov  # noqa: E402
from app._vendor.easy_tdx.ex.commands.get_minute_time import (  # noqa: E402
    GetExHistoryMinuteTimeDataCmd,
)


def probe(sym: str, dates: list[int]) -> None:
    prov.clear_cache()
    market, code = prov._split(sym)
    print(f"\n=== {sym} (market={market}) ===")
    for ymd in dates:
        bars = prov._exec(
            lambda y=ymd: GetExHistoryMinuteTimeDataCmd(prov._market_code(market), code, y))
        if not bars:
            print(f"  {ymd}: 空")
            continue
        first, last = bars[0], bars[-1]
        print(f"  {ymd}: {len(bars)} 根  "
              f"首 {first.hour:02d}:{first.minute:02d} {round(first.price, 4)}  "
              f"末 {last.hour:02d}:{last.minute:02d} {round(last.price, 4)}")

    print("  --- 日K 日历最近 6 个交易日 ---")
    for d, c in prov._daily_closes(market, code, 30)[-6:]:
        print(f"    {d}  close={c}")


if __name__ == "__main__":
    probe("AAPL.US", [20261008, 20261009, 20261012, 20261013])
    probe("00700.HK", [20261009, 20261012, 20261013])
