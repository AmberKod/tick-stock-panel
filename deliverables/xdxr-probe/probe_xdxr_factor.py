"""探针: 自建复权因子 vs 腾讯前/后复权 —— 逐根比对, 验证算法与日期口径。

要证的四件事(任一件不成立, 这个数据源就不能拿去给 /adjratio 当真源):
  1. fenhong/songzhuangu 是不是真"每股"口径(vendor 注释说协议给每10股并已 /10);
  2. category==1 的日期是不是**除权除息日**(不是股权登记日) —— 错一天, 因子整条链错位;
  3. 公式 r = (C - fenhong + peigujia*peigu) / ((1 + song + peigu) * C) 是否被腾讯认同;
  4. 后复权方向是否与腾讯 hfqday 一致。

判据: 对同一批交易日, 自建 qfq/hfq 收盘价与腾讯 qfqday/hfqday 的偏差。
逐根 0.000% 才算自证通过; 有系统性偏差说明口径不对, 不做任何"差不多"的推断。
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
    (Market.SH, "600519", "sh600519", "贵州茅台"),    # 纯现金分红, 金额大
    (Market.SZ, "300750", "sz300750", "宁德时代"),    # 有转增(songzhuangu)
    (Market.SZ, "000651", "sz000651", "格力电器"),
    (Market.SZ, "000001", "sz000001", "平安银行"),
    (Market.SH, "600036", "sh600036", "招商银行"),    # 有配股历史
]
START = "2016-01-01"
END = datetime.now().strftime("%Y-%m-%d")
URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"


def tx_kline(tcode: str, adjust: str) -> list[tuple[str, float]]:
    """腾讯日K: adjust='' 不复权 / 'qfqa' 前复权 / 'hfqa' 后复权 → [(date, close)]。

    ⚠️ 别带 ``_var`` 参数: 带了它会返回 ``kline_dayqfq={...}`` 这种 JSONP,
    json.loads 直接炸。另外老文档说 qfq 落在 ``qfqday`` 键下, 实测 response 里
    复权与否都叫 ``day``, 所以下面按 adjust 优先挑键、退回到任意 list 值。
    """
    params = {"param": f"{tcode},day,{START},{END},2000,{adjust}"}
    resp = httpx.get(URL, params=params, timeout=20.0)
    resp.raise_for_status()
    body = json.loads(resp.text)
    node = body["data"][tcode]
    want = {"": "day", "qfqa": "qfqday", "hfqa": "hfqday"}[adjust]
    key = want if node.get(want) else next((k for k in ("day", "qfqday", "hfqday") if node.get(k)), None)
    if key is None:
        return []
    return [(row[0][:10], float(row[2])) for row in node[key]]


def xdxr_events(market: Market, code: str, host: str) -> list[dict]:
    with TdxConnection(host, timeout=8.0) as conn:
        recs = conn.execute(GetXdxrInfoCmd(market, code))
    return [
        {
            "date": f"{r.year:04d}-{r.month:02d}-{r.day:02d}",
            "fenhong": r.fenhong or 0.0,
            "songzhuangu": r.songzhuangu or 0.0,
            "peigu": r.peigu or 0.0,
            "peigujia": r.peigujia or 0.0,
        }
        for r in recs
        if r.category == 1
    ]


def build_factors(dates: list[str], raw: dict[str, float], events: list[dict]) -> dict[str, float]:
    """返回 qfq_factor[date]: 历史价格 × 该因子 = 前复权价(最新日因子为 1)。"""
    factor = {d: 1.0 for d in dates}
    for ev in events:
        t = ev["date"]
        if t not in factor:
            continue                       # 事件日不在K线区间内(停牌/太老)
        idx = dates.index(t)
        if idx == 0:
            continue                       # 没有前收盘可用, 无法定位
        prev_close = raw.get(dates[idx - 1])
        if not prev_close:
            continue
        num = prev_close - ev["fenhong"] + ev["peigujia"] * ev["peigu"]
        den = (1.0 + ev["songzhuangu"] + ev["peigu"]) * prev_close
        if den <= 0:
            continue
        r = num / den
        # 除权日之前(含前一日)的价格缩到当前可比口径
        for d in dates[:idx]:
            factor[d] *= r
    return factor


def stats(label: str, mine: dict[str, float], base: dict[str, float]) -> None:
    diffs = []
    worst = None
    for d, v in mine.items():
        b = base.get(d)
        if not b or b == 0:
            continue
        pct = abs(v - b) / b * 100.0
        diffs.append(pct)
        if worst is None or pct > worst[1]:
            worst = (d, pct, v, b)
    if not diffs:
        print(f"    {label}: 无可比对样本")
        return
    n = len(diffs)
    mean = sum(diffs) / n
    print(f"    {label}: n={n}  mean={mean:.6f}%  max={max(diffs):.6f}%"
          f"  (>0.01% 的有 {sum(1 for x in diffs if x > 0.01)} 根)")
    if worst and worst[1] > 0.01:
        print(f"      最差 {worst[0]}: 自建={worst[2]:.4f} 基准={worst[3]:.4f} → {worst[1]:.4f}%")


def main() -> None:
    ranked = ping_all(timeout=5.0)
    host = ranked[0][0]
    print(f"TDX 节点 {host}, 腾讯基准区间 {START}..{END}\n")

    for market, code, tcode, label in SAMPLES:
        print(f"=== {code} {label} ===")
        try:
            raw_rows = tx_kline(tcode, "")
            qfq_rows = tx_kline(tcode, "qfqa")
            hfq_rows = tx_kline(tcode, "hfqa")
            events = xdxr_events(market, code, host)
        except Exception as exc:  # noqa: BLE001
            print(f"    ERR {type(exc).__name__}: {exc}")
            continue

        raw = dict(raw_rows)
        dates = [d for d, _ in raw_rows]
        qfq = dict(qfq_rows)
        hfq = dict(hfq_rows)
        in_range = [e for e in events if START <= e["date"] <= END and e["date"] in raw]
        print(f"    不复权 {len(raw)} 根 / 腾讯qfq {len(qfq)} 根 / 腾讯hfq {len(hfq)} 根"
              f" / 区间内 category==1 事件 {len(in_range)} 条")

        factor = build_factors(dates, raw, events)
        # 前复权: 历史 × factor(相对最新=1)
        last_date = dates[-1]
        anchor_raw = raw[last_date]
        anchor_qfq = qfq.get(last_date, anchor_raw)
        scale = anchor_qfq / anchor_raw if anchor_raw else 1.0
        mine_qfq = {d: raw[d] * factor[d] * scale for d in dates}
        # 后复权: 以最早日为基准 1, 之后按 1/r 放大 → 等价于 raw / factor 后统一缩到首日
        first_raw = raw[dates[0]]
        mine_hfq = {d: raw[d] / factor[d] / (first_raw / first_raw) for d in dates}
        hfq_scale = (hfq.get(dates[0], first_raw) / first_raw) if first_raw else 1.0
        mine_hfq = {d: mine_hfq[d] * hfq_scale for d in dates}

        stats("前复权 qfq vs 腾讯", mine_qfq, qfq)
        stats("后复权 hfq vs 腾讯", mine_hfq, hfq)

        # 除权日附近抽样: 看事实ables
        for ev in in_range[-3:]:
            i = dates.index(ev["date"])
            for j in (i - 1, i):
                if j < 0 or j >= len(dates):
                    continue
                d = dates[j]
                tag = "除权前一日" if j == i - 1 else "除权当日  "
                print(f"      {tag} {d}  raw={raw[d]:.3f}"
                      f"  factor={factor[d]:.6f}"
                      f"  自建qfq={mine_qfq[d]:.3f}  腾讯qfq={qfq.get(d, float('nan')):.3f}")
        print()


if __name__ == "__main__":
    main()
