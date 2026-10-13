"""诊断: 为什么津少数事件的因子对不上 —— 反向求解"等效分红"再看差额是什么。

已知成立的式子(茅台/平安/招行/伊利/比亚迪逐条 0.000000%):
    1/r = (C - fenhong + peigujia*peigu) / ((1 + song + peigu) * C)
        = 1 + (C - X) / C 形式不对... 直接反推:
    r_sina = f_i / f_{i-1},  X  = C * (1 - r_sina * (1 + song + peigu)) ... 见下推导

对纯现金分红(无送转配股):
    r = (C - fenhong) / C  →  X = fenhong = C * (1 - r)
所以把 X = C * (1 - r_event_needed) 反解出来, 与通达信给的 fenhong 比较,
差额就是"我漏掉的那部分调整"。

X 通用反解:
    r = (C - X + peigujia*peigu) / ((1 + song + peigu) * C)
    → X = C - r*(1+song+peigu)*C + peigujia*peigu
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from probe_xdxr_validate import sina_hfq, tx_raw, xdxr_events  # noqa: E402

START = "2015-01-01"
END = datetime.now().strftime("%Y-%m-%d")

TARGETS = [
    ("sz000651", "000651", "格力电器"),
    ("sz300750", "300750", "宁德时代"),
]


def main() -> None:
    host = "175.178.128.227"
    for tcode, code, label in TARGETS:
        market = 0 if tcode.startswith("sz") else 1
        print(f"=== {code} {label} ===")
        raw = dict(tx_raw(tcode))
        dates = sorted(raw)
        sina = dict(sina_hfq(tcode))
        events = xdxr_events(market, code, host)

        rows = sorted(sina.items())
        base_prev = None
        for d, f in rows:
            if not (START <= d <= END):
                continue
            if d not in events:
                continue
            if base_prev is None:
                base_prev = f
                continue
            r_sina = f / base_prev
            base_prev = f
            ev = events[d]
            j = next((k for k, dd in enumerate(dates) if dd >= d), None)
            if j is None or j == 0:
                continue
            prev_date = dates[j - 1]
            c = raw[prev_date]
            x = c - r_sina * (1.0 + ev["songzhuangu"] + ev["peigu"]) * c + ev["peigujia"] * ev["peigu"]
            diff_pct = abs(x - ev["fenhong"]) / c * 100.0
            if diff_pct < 0.01:
                continue
            print(f"  除权日 {d}  前一日K线 {prev_date}  C={c:.2f}")
            print(f"    通达信 fenhong={ev['fenhong']:.6f} song={ev['songzhuangu']} "
                  f"peigu={ev['peigu']} peigujia={ev['peigujia']}")
            print(f"    新浪比率={r_sina:.8f}  反解所需 X={x:.6f}  差 {x - ev['fenhong']:+.6f} "
                  f"({diff_pct:.4f}% of C)")
            # 看看这几条除了 fenhong 之外协议还给了什么(cat != 1 的相邻事件)
            print()
        print()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
