"""诊断 v2: 修掉 v1 里 r 与 1/r 用反的锅, 重新量化差额 + 找差额来源。

关系梳理(别再搞错):
    新浪给的是相邻事件的**因子比** ratio = f_i / f_{i-1}
    我们公式里的 r = (C - fenhong + ...) / ((1+song+peigu) * C)   ∈ (0,1]
    两者关系: ratio = 1 / r          (除权日 factor 变大 => ratio > 1 => r < 1)
    所以  X = C - r*(1+song+peigu)*C + peigujia*peigu,  且 r = 1/ratio
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app._vendor.easy_tdx.commands.xdxr_info import GetXdxrInfoCmd  # noqa: E402
from app._vendor.easy_tdx.models.enums import Market  # noqa: E402
from app._vendor.easy_tdx.transport.sync import TdxConnection  # noqa: E402
from probe_xdxr_validate import sina_hfq, tx_raw, xdxr_events  # noqa: E402

START = "2015-01-01"
END = datetime.now().strftime("%Y-%m-%d")
SAMPLES = [
    (Market.SZ, "000651", "sz000651", "格力电器"),      # 回购注销大户 -> 已知有偏差
    (Market.SZ, "000333", "sz000333", "美的集团"),      # 回购注销大户
    (Market.SH, "600276", "sh600276", "恒瑞医药"),      # 多次转增 + 回购
    (Market.SH, "600519", "sh600519", "贵州茅台"),      # 纯分红, 已 0.000%
    (Market.SZ, "000858", "sz000858", "五粮液"),        # 纯分红
    (Market.SZ, "002032", "sz002032", "苏泊尔"),        # 常年回购注销
    (Market.SH, "600585", "sh600585", "海螺水泥"),
    (Market.SH, "601318", "sh601318", "中国平安"),
]


def solve_x(c: float, ratio: float, ev: dict) -> float:
    r = 1.0 / ratio
    return c - r * (1.0 + ev["songzhuangu"] + ev["peigu"]) * c + ev["peigujia"] * ev["peigu"]


def main() -> None:
    host = "175.178.128.227"
    for market, code, tcode, label in SAMPLES:
        print(f"=== {code} {label} ===")
        raw = dict(tx_raw(tcode))
        dates = sorted(raw)
        rows = sorted(sina_hfq(tcode))
        events = xdxr_events(market, code, host)

        base_prev = None
        devs: list[tuple[str, float, float, float]] = []
        for d, f in rows:
            if not (START <= d <= END) or d not in events:
                continue
            if base_prev is None:
                base_prev = f
                continue
            ratio = f / base_prev
            base_prev = f
            j = next((k for k, dd in enumerate(dates) if dd >= d), None)
            if j is None or j == 0:
                continue
            c = raw[dates[j - 1]]
            x = solve_x(c, ratio, events[d])
            dev = x - events[d]["fenhong"]                 # 元/股
            devs.append((d, dev, c, events[d]["fenhong"]))

        worst = max(devs, key=lambda t: abs(t[1])) if devs else None
        print(f"    比对 {len(devs)} 条, 最差偏离 ~{abs(worst[1]) / worst[2] * 100:.4f}% of price"
              f" (差 {worst[1]:+.4f} 元, 分红 {worst[3]:.4f} 元)")
        for d, dev, c, fh in devs:
            if abs(dev) / c * 100 >= 0.05:               # 只列显著偏离
                print(f"      ⚠ {d}  差 {dev:+.4f} 元 ({dev / c * 100:+.3f}% of C)  fenhong={fh}")
        print()

    # 格力近年来全部的 xdxr 事件(所有类别), 看差额是不是与"回购注销/股本变化"同天
    print("=== 格力 2020-2026 全部 xdxr 事件(含非除权类) ===")
    with TdxConnection(host, timeout=8.0) as conn:
        recs = conn.execute(GetXdxrInfoCmd(Market.SZ, "000651"))
    for r in recs:
        if f"{r.year:04d}-{r.month:02d}-{r.day:02d}" < "2020-01-01":
            continue
        line = (f"  {r.year:04d}-{r.month:02d}-{r.day:02d} cat={r.category:>2} {r.name}"
                f"  fenhong={r.fenhong} song={r.songzhuangu}")
        if r.category != 1:
            line += (f"  前总股本={r.qian_zongguben} 后总股本={r.hou_zongguben}"
                     f"  前流通={r.panqian_liutong} 后流通={r.panhou_liutong}")
        print(line)


if __name__ == "__main__":
    main()
