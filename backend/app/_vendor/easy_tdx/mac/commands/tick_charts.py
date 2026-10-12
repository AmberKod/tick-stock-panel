"""MAC 多日分时图命令（0x123E）。

获取单只股票多日的分时数据。
"""

import struct
from datetime import date, time

from ..._binary import unpack_from
from ...codec.mac_frame import build_mac_request
from ...commands.base import BaseCommand
from ..models import MacMultiTickChart, MacMultiTickDay, MacTick

_MSG_ID = 0x123E


class TickChartsCmd(BaseCommand[MacMultiTickChart]):
    """获取多日分时图。

    Args:
        market:     市场代码。
        code:       6 位股票代码。
        start_date: 起始日期（None 表示从最新交易日开始）。
        days:       天数（最多 5）。
    """

    def __init__(
        self,
        market: int,
        code: str,
        start_date: date | None = None,
        days: int = 5,
    ) -> None:
        self._market = market
        self._code = code
        if start_date is not None:
            self._start_ymd = start_date.year * 10000 + start_date.month * 100 + start_date.day
        else:
            self._start_ymd = 0
        self._days = days

    def build_request(self) -> bytes:
        body = struct.pack(
            "<H22sIHH6x",
            self._market,
            self._code.encode("gbk"),
            self._start_ymd,
            self._days,
            1,
        )
        return build_mac_request(_MSG_ID, body)

    def parse_response(self, body: bytes) -> MacMultiTickChart:
        # 头部
        (market, code_raw) = unpack_from("<H22s", body, 0, "tick_charts header")

        # 每日日期(5 x I) + 每日前收(5 x f) = 40 bytes
        date_ints = unpack_from("<5I", body, 24, "tick_charts dates")
        pre_close_floats = unpack_from("<5f", body, 44, "tick_charts pre_closes")

        # count(2) + send_last(1) + page_size(2) + total(2)
        (count, send_last, page_size, total) = unpack_from("<HBHH", body, 64, "tick_charts header2")

        # TSP 改动(2026-10-12): 用 **实际存在的 tick 数**而不是 count*page_size。
        # 上游 "count 个块、每块 page_size 根" 只是名义值 —— 实测美股
        # count=5 / page_size=390 (=1950) 而 total 只有 1561 根, 按名义值读会在
        # 最后一块越界("数据不足...偏移 22037, 实际剩余 8 字节")。以 body 长度和
        # total 取小者为真实根数, 顺带把尾部元数据的偏移也按真实根数算。
        max_by_len = max(0, (len(body) - 71) // 14)
        real_ticks = min(total, max_by_len)

        days: list[MacMultiTickDay] = []
        for d in range(count):
            ticks: list[MacTick] = []
            for t in range(page_size):
                index = d * page_size + t
                if index >= real_ticks:
                    break
                offset = 71 + index * 14
                (minutes, price, avg, vol, tick_reserved) = unpack_from(
                    "<HffHH", body, offset, f"tick_charts tick[{d}][{t}]"
                )
                # TSP 改动(2026-10-12): minutes 可 > 1439(美股会话跨北京时间零点,
                # 实测最大 1678 = 次日 03:58)。原写法 time(minutes//60, ...) 会抛
                # ValueError; 钟点取 mod 24, 原始分钟数保留在 MacTick.minutes。
                hour = (minutes // 60) % 24
                ticks.append(
                    MacTick(
                        time=time(hour, minutes % 60),
                        price=price,
                        avg=avg,
                        vol=vol,
                        minutes=minutes,
                    )
                )

            ymd = date_ints[d]
            day_date = date(ymd // 10000, (ymd % 10000) // 100, ymd % 100)
            days.append(
                MacMultiTickDay(
                    date=day_date,
                    pre_close=pre_close_floats[d],
                    ticks=ticks,
                )
            )

        # 尾部元数据 —— 偏移必须跟着真实根数走(见上方 TSP 改动说明)
        tail_offset = 71 + real_ticks * 14
        (
            name_raw,
            _decimal,
            _category,
            _vol_unit,
            _date_raw,
            _time_raw,
            pre_close,
            open,
            high,
            low,
            close,
            _momentum,
            vol,
            amount,
            _tail_pad2,
            turnover,
            avg,
            _industry,
        ) = unpack_from("<44sBHf5x2I5ffIf12s2fI", body, tail_offset, "tick_charts tail")

        return MacMultiTickChart(
            market=market,
            code=code_raw.decode("gbk", errors="ignore").replace("\x00", ""),
            name=name_raw.decode("gbk", errors="ignore").replace("\x00", ""),
            pre_close=pre_close,
            open=open,
            high=high,
            low=low,
            close=close,
            vol=int(vol),
            amount=amount,
            turnover=turnover,
            avg=avg,
            charts=days,
        )
