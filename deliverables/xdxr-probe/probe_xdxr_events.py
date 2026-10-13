"""探针: 通达信标准市(7709) GetXdxrInfoCmd —— 除权除息全事件能不能拿到真数据。

目标(只求事实, 不写代码):
  1. 容器/宿主能否直连 7709 标准节点拿到 xdxr;
  2. category 分布与各分类字段语义(哪些类参与复权、哪些只是股本变动);
  3. category==1 的日期到底是"股权登记日"还是"除权除息日" —— 这直接决定因子算在哪一天,
     后续用"复权后的日K是否还跳空"来判定;
  4. 每股口径是否真如 vendor 注释所说协议给的是"每10股"(vendor 已 /10)。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app._vendor.easy_tdx.commands.xdxr_info import GetXdxrInfoCmd  # noqa: E402
from app._vendor.easy_tdx.models.enums import Market  # noqa: E402
from app._vendor.easy_tdx.transport.sync import (  # noqa: E402
    KNOWN_HOSTS,
    TdxConnection,
    ping_all,
)

# market: SH=1 / SZ=0
SAMPLES = [
    (Market.SH, "600519", "贵州茅台"),   # 年年大额分红, 送转极少 -> 纯现金分红样本
    (Market.SZ, "000651", "格力电器"),
    (Market.SZ, "000001", "平安银行"),   # 历史上有送股+配股
    (Market.SH, "601398", "工商银行"),
    (Market.SZ, "300750", "宁德时代"),   # 有转增
]


def pick_host() -> str:
    print(f"已知标准节点: {len(KNOWN_HOSTS)} 个")
    ranked = ping_all(timeout=5.0)          # 返回按延迟升序的 (host, seconds)
    for host, sec in ranked[:5]:
        print(f"  可达 {host}  {sec * 1000:.1f} ms")
    if not ranked:
        raise SystemExit("没有任何标准节点可达 —— 先确认网络")
    return str(ranked[0][0])


def main() -> None:
    host = pick_host()
    print(f"\n=== 使用节点 {host} (7709) ===\n")
    with TdxConnection(host, timeout=8.0) as conn:
        for market, code, label in SAMPLES:
            print(f"--- {code} {label} (market={int(market)}) ---")
            try:
                recs = conn.execute(GetXdxrInfoCmd(market, code))
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR {type(exc).__name__}: {exc}")
                continue
            if not recs:
                print("  空")
                continue
            cats: dict[int, int] = {}
            for r in recs:
                cats[r.category] = cats.get(r.category, 0) + 1
            print(f"  共 {len(recs)} 条, category 分布: {dict(sorted(cats.items()))}")
            print(f"  日期区间: {recs[0].year:04d}-{recs[0].month:02d}-{recs[0].day:02d}"
                  f" .. {recs[-1].year:04d}-{recs[-1].month:02d}-{recs[-1].day:02d}")
            # 除权除息类明细
            ev = [r for r in recs if r.category == 1]
            print(f"  category==1 事件 {len(ev)} 条, 最近 8 条:")
            for r in ev[-8:]:
                print(f"    {r.year:04d}-{r.month:02d}-{r.day:02d}  {r.name}"
                      f"  fenhong={r.fenhong!r} songzhuangu={r.songzhuangu!r}"
                      f" peigu={r.peigu!r} peigujia={r.peigujia!r}")
            # 其它类别样例(看是否有可用信息)
            others = [r for r in recs if r.category != 1]
            if others:
                print("  非除权类样例(最后 6 条):")
                for r in others[-6:]:
                    print(f"    {r.year:04d}-{r.month:02d}-{r.day:02d} cat={r.category} {r.name}"
                          f"  前流通={r.panqian_liutong!r} 后流通={r.panhou_liutong!r}"
                          f" 前总={r.qian_zongguben!r} 后总={r.hou_zongguben!r}"
                          f" suogu={r.suogu!r}")
            print()


if __name__ == "__main__":
    main()
