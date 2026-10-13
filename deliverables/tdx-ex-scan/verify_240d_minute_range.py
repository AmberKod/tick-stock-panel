"""0x240D 接入后的实拉验证：真 OHLC / 分片 / 跨零点保序 / 聚合对齐。"""
import json
import sys

sys.path.insert(0, r"E:\ai_codes\ai_personal_panel\tsp-fresh\backend")

from app.data_providers import tdx_ex_provider as tx  # noqa: E402


def head(tag, payload):
    print(f"\n=== {tag} ===")
    print(f"protocol={payload.get('protocol')} ohlc={payload.get('ohlc')} "
          f"has_avg={payload.get('has_avg')} bar_count={payload.get('bar_count')} "
          f"days={len(payload.get('days', []))} derived={payload.get('derived')}")
    for d in payload["days"]:
        bars = d["bars"]
        if not bars:
            continue
        f, l = bars[0], bars[-1]
        real = sum(1 for b in bars if not (b["open"] == b["high"] == b["low"] == b["close"]))
        print(f"  {d['date']}: n={len(bars):4d} first={f['datetime'][11:16]} "
              f"last={l['datetime'][11:16]} 真实OHLC={real}/{len(bars)}")
        print(f"      first bar={ {k: f[k] for k in ('open','high','low','close','volume','avg')} }")
        print(f"      last  bar={ {k: l[k] for k in ('open','high','low','close','volume','avg')} }")


for sym, days in [("00700.HK", 5), ("AAPL.US", 5)]:
    for p in ("m1", "m5"):
        tx.clear_cache()
        head(f"{sym} {p} days={days}", tx.minute_range(sym, days=days, period=p))

# 单日: 与 /minute(goods_kline MIN_1) 口径是否一致
print("\n" + "=" * 60)
print("口径交集比对: minute_range(m1, days=1) vs minute_bars(m1)")
print("=" * 60)
for sym in ("00700.HK", "AAPL.US"):
    tx.clear_cache()
    rng = tx.minute_range(sym, days=1, period="m1")["days"][-1]["bars"]
    tx.clear_cache()
    mb = tx.minute_bars(sym, period="m1", limit=700)["rows"]
    t_rng = [b["datetime"] for b in rng]
    t_mb = [b["datetime"] for b in mb]
    inter = set(t_rng) & set(t_mb)
    print(f"{sym}: 240D n={len(t_rng)} MIN_1 n={len(t_mb)} 交集={len(inter)}")
    if inter:
        mr = {b["datetime"]: b for b in rng}
        mm = {b["datetime"]: b for b in mb}
        dev = [abs(mr[k]["close"] - mm[k]["close"]) / mm[k]["close"] * 100
               for k in inter if mm[k]["close"]]
        print(f"    close 平均偏差={sum(dev)/len(dev):.6f}% 最大={max(dev):.6f}%")
        nfirst = next(iter(sorted(inter)))
        print(f"    交集首根={nfirst} 240D.close={mr[nfirst]['close']} "
              f"MIN_1.close={mm[nfirst]['close']}")
        vol_diff = [abs(mr[k]["volume"] - mm[k]["volume"]) for k in inter]
        print(f"    volume 差: 平均={sum(vol_diff)/len(vol_diff):.2f} "
              f"最大={max(vol_diff)}  (非零即两源量单位不同)")
        print(f"    样例 vol: 240D={ {k: mr[k]['volume'] for k in sorted(inter)[:3]} }")
        print(f"              MIN_1={ {k: mm[k]['volume'] for k in sorted(inter)[:3]} }")

print("\n=== 失败路径 ===")
try:
    tx.minute_range("00700.HK", days=99, period="m1")
except ValueError as e:
    print("days 越界 ->", e)
print("\nDONE " + json.dumps({"src": "verify_240d_minute_range.py"}, ensure_ascii=False))
