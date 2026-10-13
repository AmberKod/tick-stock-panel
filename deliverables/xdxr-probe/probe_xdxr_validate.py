"""探针(终版): 用新浪 hfq 因子做基准, 硬校验 xdxr 自建因子。

为什么要新浪: 腾讯 fqkline 的 qfqa/hfqa 参数实测被忽略 —— 三种 adjust 返回同一份
不复权数据(见 probe_xdxr_factor.py 的输出: 除权当日"腾讯qfq"与 raw 一字不差),
所以它当不了基准。新浪 hfq.js 给的是真正的累乘因子。

新浪语义(按 akshare 用法确认): data 按日期**倒序**, 每条 {d, f}; 升序排后按日期
向前填充(ffill), 即 **从除权除息日 d 起、到下一个除权日之前, 价格乘 f**。

于是不存在任何"按比例配 Georges" 的自由度: 对相邻两条事件,
    新浪 f_i / f_{i-1}  必须等于   自建 1/r_i
其中 r_i = (C - fenhong + peigujia*peigu) / ((1 + song + peigu) * C), C = 除权日前一
交易日的不复权收盘价。逐条对不上就是口径错了, 不做"差不多"判定。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app._vendor.easy_tdx.commands.xdxr_info import GetXdxrInfoCmd  # noqa: E402
from app._vendor.easy_tdx.models.enums import Market  # noqa: E402
from app._vendor.easy_tdx.transport.sync import TdxConnection, ping_all  # noqa: E402

SAMPLES = [
    (Market.SH, "600519", "sh600519", "贵州茅台"),
    (Market.SZ, "300750", "sz300750", "宁德时代"),      # 有转增 0.8
    (Market.SZ, "000651", "sz000651", "格力电器"),
    (Market.SZ, "000001", "sz000001", "平安银行"),
    (Market.SH, "600036", "sh600036", "招商银行"),
    (Market.SH, "600887", "sh600887", "伊利股份"),      # 多次送转
    (Market.SZ, "002594", "sz002594", "比亚迪"),
]
START = "2015-01-01"
END = datetime.now().strftime("%Y-%m-%d")
TX_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_URL = "https://finance.sina.com.cn/realstock/company/{code}/hfq.js"


def tx_raw(tcode: str) -> list[tuple[str, float]]:
    """腾讯不复权日K。"""
    params = {"param": f"{tcode},day,{START},{END},2000,"}
    resp = httpx.get(TX_URL, params=params, timeout=20.0)
    resp.raise_for_status()
    node = json.loads(resp.text)["data"][tcode]
    rows = node.get("day") or []
    return [(r[0][:10], float(r[2])) for r in rows]


def sina_hfq(tcode: str) -> list[tuple[str, float]]:
    """新浪后复权因子 → [(date, factor)] 按日期**升序**。"""
    resp = httpx.get(SINA_URL.format(code=tcode), timeout=20.0)
    resp.raise_for_status()
    text = resp.text
    payload = text[text.find("={") + 1:text.rfind("}") + 1]
    data = json.loads(payload)["data"]
    rows = [(d["d"], float(d["f"])) for d in data]
    return sorted(rows, key=lambda t: t[0])       # 新浪给倒序 -> 转升序


def xdxr_events(market: Market, code: str, host: str) -> dict[str, dict]:
    with TdxConnection(host, timeout=8.0) as conn:
        recs = conn.execute(GetXdxrInfoCmd(market, code))
    return {
        f"{r.year:04d}-{r.month:02d}-{r.day:02d}": {
            "fenhong": r.fenhong or 0.0,
            "songzhuangu": r.songzhuangu or 0.0,
            "peigu": r.peigu or 0.0,
            "peigujia": r.peigujia or 0.0,
        }
        for r in recs
        if r.category == 1
    }


def main() -> None:
    host = ping_all(timeout=5.0)[0][0]
    print(f"TDX 节点 {host}   基准: 新浪 hfq.js    区间 {START}..{END}\n")

    for market, code, tcode, label in SAMPLES:
        print(f"=== {code} {label} ===")
        raw = dict(tx_raw(tcode))
        dates = sorted(raw)
        sina = sina_hfq(tcode)
        events = xdxr_events(market, code, host)

        sina_dates = {d for d, _ in sina}
        xdxr_dates = set(events)
        both = sorted(d for d in sina_dates & xdxr_dates if START <= d <= END)
        print(f"    不复权 {len(raw)} 根 | 新浪因子 {len(sina)} 条 | xdxr除权 {len(xdxr_dates)} 条"
              f" | 两者日期**完全一致**的日子 {len(both)} 条")
        only_sina = sorted(d for d in (sina_dates - xdxr_dates) if START <= d <= END)
        only_xdxr = sorted(d for d in (xdxr_dates - sina_dates) if START <= d <= END)
        if only_sina:
            print(f"      ⚠ 只在新浪有的日期: {only_sina[-5:]}")
        if only_xdxr:
            print(f"      ⚠ 只在通达信有的日期: {only_xdxr[-5:]}")

        # 按日期升序, 逐条比较 f_i/f_{i-1}  与  1/r_i
        rows = [(d, f) for d, f in sina if START <= d <= END]
        diffs: list[float] = []
        worst = None
        base_prev = None
        for i, (d, f) in enumerate(rows):
            if d not in events:
                continue
            if base_prev is None:
                base_prev = f          # 段首条只能用来对齐, 不与自身比值
                continue
            ratio_sina = f / base_prev
            base_prev = f
            ev = events[d]
            # 找除权日前一交易日
            j = None
            for k, dd in enumerate(dates):
                if dd >= d:
                    j = k
                    break
            if j is None or j == 0:
                continue
            prev_close = raw[dates[j - 1]]
            num = prev_close - ev["fenhong"] + ev["peigujia"] * ev["peigu"]
            den = (1.0 + ev["songzhuangu"] + ev["peigu"]) * prev_close
            if den <= 0 or num <= 0:
                continue
            r = num / den
            ratio_mine = 1.0 / r
            pct = abs(ratio_mine - ratio_sina) / ratio_sina * 100.0
            diffs.append(pct)
            if worst is None or pct > worst[1]:
                worst = (d, pct, ratio_mine, ratio_sina)

        if not diffs:
            print("    无可比对事件")
            print()
            continue
        n = len(diffs)
        print(f"    逐条比率比对 n={n}  mean={sum(diffs) / n:.6f}%  max={max(diffs):.6f}%"
              f"  (>0.01% 的有 {sum(1 for x in diffs if x > 0.01)} 条)")
        if worst:
            print(f"      最差 {worst[0]}: 自建={worst[2]:.8f} 新浪={worst[3]:.8f} → {worst[1]:.6f}%")
        if max(diffs) <= 0.01:
            print("      ✅ 每一条都对上了 —— 日期口径/每股口径/公式三件事同时自证")
        print()


if __name__ == "__main__":
    main()
