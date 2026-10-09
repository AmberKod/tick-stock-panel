"""数据真实性闸门 (source gate) — 项目铁律的代码化。

背景: 九章量化终端有 15 条路由在数据源不可达时用 random() 伪造数据,
且前端 "demo 灯不熄灭" —— UI 亮着实时绿灯显示假 PE/PB。九章模块摘入
TSP 之前, 所有外来数据必须先过本闸门:

- 只有白名单里的 source 才算真实数据;
- demo / fail / timeout / 未知 source / **缺失 source 字段** → 一律剔除,
  不计入分母, 由调用方声明 unavailable;
- **绝不留占位值** —— 不允许用 0/None/{} 伪装成功。

本模块全部纯函数、零 IO, 可直接单测。

白名单收录依据 (2026-10-09 grep 实证):
- 九章实测真数据标记 6 个 (见下方第一组);
- TSP provider 真实标记 (backend/app/data_providers/ 及其数据链路
  services/hk_data_adapter.py 中 source 字段的实际取值, file:line 见注释)。
- 明确**不收**: us_demo / hk_demo (yfinance_provider.py:196、
  hk_data_adapter.py:102 —— 正是闸门要拦的静态演示池)。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

#: 真实数据 source 白名单 (frozenset, 顺序无关)。
REAL_SOURCES: frozenset[str] = frozenset({
    # ── 九章实测真数据 source 标记 ──
    "live-tencent",
    "live-akshare",
    "live-eastmoney",
    "live-hexin",
    "live-tencent-proxy",
    "sec-edgar",
    # ── TSP provider 真实标记 (data_providers/ grep 实证) ──
    # hk_quickquote_provider.py:140,211
    "tencent",
    # hk_quickquote_provider.py:173
    "sina",
    # tencent_market_provider.py:53,105
    "tencent_allstock_compat",
    # tickflow_provider.py:24 (name) → normalizer.py:88,116,139 默认 source
    "tickflow",
    # yfinance_provider.py:237,300
    "yfinance",
    # hk_daily_provider.py:208
    "sina_hk_qfq",
    # hk_daily_provider.py:358,387
    "eastmoney_hk_daily_check",
    # hk_daily_provider.py:441
    "sina_hk_daily",
    # hk_daily_provider.py:469
    "tencent_hk_daily",
    # hk_financial_provider.py:25 (HK_FINANCIAL_SOURCE)
    "eastmoney_hk_announcement",
    # ── provider 数据链路真实标记 (services/hk_data_adapter.py) ──
    # hk_data_adapter.py:340
    "akshare",
    # hk_data_adapter.py:141,148
    "akshare_em",
    "akshare_sina",
    # hk_data_adapter.py:199,305 (真实同步落盘的证券池文件)
    "hk_universe_file",
    "us_universe_file",
})

#: 组合 source 的分隔符 (hk_daily_provider.py:684 用 "+" 连接主备源,
#: 如 "sina_hk_daily+tencent_hk_daily" —— 各部分都在白名单才算真)。
_COMBO_SEP = "+"


@dataclass(frozen=True)
class FilterResult:
    """filter_records 的返回: 通过明细 + 剔除计数 + 拒因明细。

    accepted 里的元素是**原样透传**的入参记录 (绝不做占位填充);
    reject_reasons 每条对应一条被剔除记录, 格式
    ``records[{i}]: {reason_code}:{value!r}``。
    """

    accepted: list = field(default_factory=list)
    rejected_count: int = 0
    reject_reasons: list[str] = field(default_factory=list)


def _extract_source(record_or_source: object) -> str | None:
    """从 str / Mapping / 带 source 属性的对象里取 source 字符串。

    取不到或类型不对 → None (缺失即剔除, 不猜测)。
    """
    if isinstance(record_or_source, str):
        source: object = record_or_source
    elif isinstance(record_or_source, Mapping):
        source = record_or_source.get("source")
    else:
        source = getattr(record_or_source, "source", None)
    return source if isinstance(source, str) else None


def _classify_reject(source: str) -> str:
    """对未通过白名单的 source 给出拒因分类码。"""
    low = source.lower()
    if "demo" in low:
        return "demo_source"
    if "fail" in low or "timeout" in low:
        return "unavailable_source"
    return "unknown_source"


def is_real(record_or_source: object) -> bool:
    """判断一条记录 (或裸 source 字符串) 是否来自真实数据源。

    - str: 视为 source 本身;
    - Mapping: 取 ``record["source"]``;
    - 其它对象: 取 ``record.source`` 属性 (取不到视为缺失)。

    缺失 / 空 / 含空白段 / 任一部分不在白名单 → False。
    组合 source ("a+b") 的每一段都必须在白名单内。
    """
    source = _extract_source(record_or_source)
    if source is None:
        return False
    parts = [p.strip() for p in source.split(_COMBO_SEP)]
    if not parts or any(not p for p in parts):
        return False
    return all(p in REAL_SOURCES for p in parts)


def filter_records(records: object) -> FilterResult:
    """批量过滤记录: 真数据透传, 假数据/缺失数据剔除并给出拒因明细。

    纯函数: 不修改入参, accepted 原样引用通过者; 拒绝的记录**直接消失**,
    绝不生成占位记录 —— 分母语义由调用方按 ``len(result.accepted)`` 计算。
    """
    if not records:
        return FilterResult(accepted=[], rejected_count=0, reject_reasons=[])
    accepted: list = []
    reject_reasons: list[str] = []
    for i, rec in enumerate(records):
        source = _extract_source(rec)
        if source is None:
            reject_reasons.append(f"records[{i}]: missing_source")
            continue
        parts = [p.strip() for p in source.split(_COMBO_SEP)]
        if any(not p for p in parts):
            reject_reasons.append(f"records[{i}]: empty_source_part:{source!r}")
            continue
        if any(p not in REAL_SOURCES for p in parts):
            reject_reasons.append(f"records[{i}]: {_classify_reject(source)}:{source!r}")
            continue
        accepted.append(rec)
    return FilterResult(accepted=accepted, rejected_count=len(reject_reasons), reject_reasons=reject_reasons)
