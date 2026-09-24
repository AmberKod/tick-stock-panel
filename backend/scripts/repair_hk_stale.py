#!/usr/bin/env python
"""港股 stale 补拉入口 (方案 X)。

背景 (2026-09-22 架构师调研 + 实测): 港股热点覆盖率长期 42%, stale 1611 只;
抽样验证显示**不是结构性漏拉** —— 新浪源已自愈补齐到当日, 本地只是"管道没回头
重拉"。因此本脚本**不接新数据源**: 从 enriched 直算 stale 清单 → 逐只重跑
**现役** ``HKDailyProvider`` (新浪主 + 腾讯备 + 东财仲裁全在 provider 里) →
走现役 ``publish_hk_daily_snapshot`` 落盘 (raw + 复权因子 + enriched 原子发布)。

请求窗口是**维护窗口** (起点 = 现有历史最早日) 而非纯增量 ``latest+1``:
现役 publish 对"未核实口径的旧分区"要求 incoming 覆盖旧分区已确认交易日,
纯增量会让 ``missing`` 变成几千天而被 fail-closed 守卫拒 (2026-09-23 实跑
50/50 全失败)。放宽窗口**不增加网络请求** (新浪一次请求返回全历史再在内存
过滤; 腾讯兜底窗口恒为 end-120 天)。详见 ``_maintenance_start``。

第三层守卫: 旧仓 raw 分区两代口径并存 (104 只带 5 列身份声明 / 1425 只只有
老 8 列)。8 列标的首拉时, incoming 的 ``currency`` 全 null (新浪源不带币种,
唯一来源是腾讯 ``quote[75]``, 腾讯熔断即全空), 而 publish 的"从旧已核实分区
继承"旁路要求旧分区本身已核实 —— 8 列老分区没有 currency 可继承 → 守卫
"币种、量单位或价格口径未核实" 拒。修法见 ``_ensure_currency_identity``:
currency 从**证据链**取 (腾讯报价 → 旧分区 → HKEX 证券清单), 其余 4 列
声明 provider 恒产 (``_validated_rows``), 一列都不凭空造, 守卫一行不动。

第四层守卫 (归类, 不修): 口径未核实的旧分区在 enriched 算不出来时
(:1041 "维护窗口尚未具备完整原始价与复权因子"), publish 拒绝替换原文件。
触发链: 近 90 天真缺口 (长期停牌如 00167, 新浪对停牌段无行) →
coverage_ok=False → enriched 空 → repair → 拒。这是**正确的 fail-closed**
(坏了不如不动), 源侧缺口修管道修不出来 → 归入 ``repair_blocked`` 单独上报,
不混 failures。

第五层 (2026-09-24 判定: 批跑自己打死了腾讯兜底源, 非 A 假行非 B 源死):
腾讯 WAF 连续 5 次拦截开进程级熔断 (15→30→60min)。批跑每只 ≥2 个腾讯请求
(兜底日线 + 币种), 且日历缓存 key 按请求窗口起止 — 维护窗口起点逐标的各异
(1998/2015/…), 每个新 key ≈28 个 hkHSI 日历请求 ⇒ 1526 只数千次必然熔断
(实证第 80 只处开闸, 此后 1344 只新浪单源跑)。三改造:
1. ``_tencent_circuit_remaining`` 熔断感知: 每只前查冷却剩余, 暂停等待
   (上限 65 分钟, 超时跳过该只), 不在封禁期空烧 (熔断期请求会刷新封禁);
2. 日历校验窗口注入 ``_CALENDAR_WINDOW_DAYS``(120, 与腾讯兜底窗口同宽):
   只校验近段, 深历史由 merge 三重护栏兜底 → hkHSI 请求量砍一个数量级;
3. 默认限速 0.1→0.5s + ``--batch-size``(默认 100) 批间暂停打进度摘要。

stale 基准**不用全市场 as_of** (2026-09-23 实证暴露的移动靶): as_of 是所有
标的最新日期的最大值, 补拉把它推到今天后, 停在"服务停摆日"的标的
(1187 只停在 09-18) 会一夜之间全被算成 stale —— 补得越多 stale 越多
(1611 → 2796)。基准改用**市场当天 − 容差** (默认 7 个自然日, 覆盖周末 +
常规节假日), 或由 ``--stale-before`` 显式指定; 它不随本脚本的写盘移动。

为什么落库不用 ``recompute_market_enriched`` (规格原文建议):
它只"重算既有 bar" —— ``sync_hk_daily_to_enriched`` 不传 factors, HK 分支会走
``_hk_factors_for_window`` 读旧缓存, 而 ``coverage_end < 新 raw 的 max(date)``
直接 raise "覆盖不足或版本冲突"。补拉恰恰把 raw 推到更新的一天 ⇒ 用它必然
全批 fail-closed, 一只也补不进去。现役 ``publish_hk_daily_snapshot`` 会带上
本次拉取的新复权因子 (coverage_end = 本次 actual_end), 才是补拉的正确落库路径
(与 ``kline_sync.sync_and_persist_daily_batch`` 的港股分支同一条链)。

用法:
    python repair_hk_stale.py --data-dir <path> [--market HK] [--limit N]
                              [--stale-before YYYY-MM-DD] [--tolerance-days 7]
                              [--batch-size 100] [--sleep-seconds 0.5]
                              [--dry-run | --yes]

默认 dry-run (只出计划, 不写盘、不发请求); 只有 ``--yes`` 才真正补拉。
手动触发, **不进 APScheduler**。可重跑幂等: 补完的标的回到 as_of, 下次计划
自然不再包含它; publish 侧自带 unchanged 检测, 重复跑不会重复写盘。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as _time
from pathlib import Path
from typing import Any

import polars as pl

# `python scripts/repair_hk_stale.py` 从任意 cwd 都要能 import app.*
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

logger = logging.getLogger(__name__)

_MARKET_SUFFIX = {"HK": ".HK", "US": ".US"}
# 计划里展示的"峰值档"桶数上限
_BUCKET_TOP_N = 5
# 串行限速: 腾讯兜底源有 WAF, 批跑时每只要打它 ≥2 次 (兜底日线 + 币种报价),
# 2026-09-24 实证 0.1s 间隔下 80 只即熔断; 提到 0.5s 让腾讯侧 QPM 减 80%
_DEFAULT_SLEEP_SECONDS = 0.5
# 熔断等待上限 (秒): 现役熔断冷却 15→30→60 分钟封顶。等超过一档最大冷却
# (60min) 还没解封说明源长时间不可用, 跳过该只继续批 (别死等), 幂等可续跑。
_CIRCUIT_WAIT_MAX_SECONDS = 65 * 60
# 日历校验窗口 (自然日, 与腾讯兜底窗口同宽): 补拉场景只需校验近段覆盖,
# 深历史由 merge 护栏兜底 (详见 _resolve_fetch)
_CALENDAR_WINDOW_DAYS = 120
# 批间暂停 (秒): 每跑完一批 --batch-size 只, 暂停一下打进度摘要
_BATCH_PAUSE_SECONDS = 2.0
_DEFAULT_BATCH_SIZE = 100
# stale 基准容差 (自然日): 覆盖周末 + 常规节假日/长假, 与 _MARKET_DAILY_STALENESS_DAYS
# 同思路 —— "服务停了几天没同步"不该被判成源缺口 (2026-09-23: 1187 只停在 09-18)
_DEFAULT_TOLERANCE_DAYS = 7
# 单只耗时经验值 (秒): 2026-09-23 真实旧仓实测 2 只 16.391s (含全历史拉取 + 落盘)。
# 只在 dry-run (没有实测样本) 时用它做估算; 实跑后一律用本次实测值。
_MEASURED_SECONDS_PER_SYMBOL = 8.2


def _market_suffix(market: str) -> str:
    key = str(market or "").strip().upper()
    if key not in _MARKET_SUFFIX:
        raise ValueError(f"不支持的市场: {market} (可用: {sorted(_MARKET_SUFFIX)})")
    return _MARKET_SUFFIX[key]


def _plain_date(value: Any) -> date | None:
    """enriched 分区的 date 可能是 Date/Datetime/字符串, 统一成 date; 取不到返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def scan_latest_dates(data_dir: Path, market: str = "HK") -> pl.DataFrame:
    """扫 enriched 分区, 返回每只标的的最新交易日 (symbol, latest_date)。

    目录不存在 / 读失败 / 无该市场分区时返回空表 —— **不可用不计入**, 不拿
    空结果冒充"没有 stale"。legacy 分区的 date 是 Datetime('us'), 统一 cast
    成 Date 后再取 max, 与 overview 的 as_of 口径一致。
    """
    empty = pl.DataFrame(schema={"symbol": pl.String, "latest_date": pl.Date})
    root = Path(data_dir) / "kline_hk_us_enriched"
    if not root.exists():
        return empty
    suffix = _market_suffix(market)
    try:
        lazy = pl.scan_parquet(
            str(root / "symbol=*" / "part.parquet"),
            # 历史分区由不同源写入 (新浪 volume=Float64 / 兜底源 Int64), 跨分区
            # scan 需允许整型向浮点兼容提升, 否则 SchemaError (同 overview 侧)
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        )
        timeline = lazy.select("symbol", "date").collect()
    except Exception as exc:  # 目录损坏/混 schema 时不炸整个脚本
        logger.warning("enriched 扫描失败 (%s): %s", root, exc)
        return empty
    if timeline.is_empty():
        return empty
    dated = (
        timeline.filter(pl.col("symbol").cast(pl.String).str.ends_with(suffix))
        .with_columns(pl.col("date").cast(pl.Date, strict=False))
        .drop_nulls("date")
    )
    if dated.is_empty():
        return empty
    return (
        dated.group_by("symbol")
        .agg(pl.col("date").max().alias("latest_date"))
        .sort("symbol")
    )


def _resolve_stale_before(
    market: str, *, stale_before: date | None = None,
    tolerance_days: int = _DEFAULT_TOLERANCE_DAYS, today: date | None = None,
) -> tuple[date, str, date]:
    """stale 判定基准: 返回 ``(阈值, 基准说明, 市场当天)``; 严格**小于**阈值算 stale。

    基准不能用"全市场 ``as_of`` = max(所有标的最新日)" —— 那是**移动靶**:
    补拉把 as_of 推到今天之后, 停在"服务停摆日"的那批标的会被一夜之间全部算成
    stale。2026-09-23 实证: 补拉前 as_of=09-18 / stale=1611; 只补了 2 只把 as_of
    推到 09-23 后 stale 变 2796 —— 多出来的 1187 只全是"停在 09-18"的标的,
    它们只是服务没启动 (每日同步 cron 没跑), 不是源缺口。**补得越多 stale 越多**。

    稳定基准 = 市场当天 − 容差 (默认 7 个自然日, 覆盖周末 + 常规节假日), 它只随
    日历移动、不随本脚本的写盘移动; ``stale_before`` 给定时它就是阈值本身
    (不再叠加容差), 便于用户精确圈定"只补 09-03 那一批"。
    """
    market_today = today or _market_today(market)
    if stale_before is not None:
        return stale_before, "stale-before", market_today
    days = max(0, int(tolerance_days))
    return market_today - timedelta(days=days), f"market-today-{days}d", market_today


def compute_stale_plan(
    data_dir: Path, market: str = "HK", limit: int | None = None, *,
    stale_before: date | None = None,
    tolerance_days: int = _DEFAULT_TOLERANCE_DAYS,
    today: date | None = None,
) -> dict[str, Any]:
    """从 enriched 直算 stale 清单与补拉计划 (纯读, 不发请求)。

    stale 判定: 该标的最新交易日 **< 基准阈值** (见 ``_resolve_stale_before``;
    默认 = 市场当天 − 容差, 可由 ``stale_before`` 显式指定)。**刻意不用全市场
    as_of** —— 否则补拉本身推高基准、stale 数反弹。返回的 ``as_of`` 只是给
    人看的现状指标, 不参与判定。

    优先级: 按"停在哪个日期"分桶, 桶大的先补 (973 只停在 09-03 的峰值档最优先),
    桶内按 symbol 排序保证可重放。
    """
    latest = scan_latest_dates(data_dir, market)
    threshold, basis, market_today = _resolve_stale_before(
        market, stale_before=stale_before, tolerance_days=tolerance_days, today=today)
    plan: dict[str, Any] = {
        "market": market, "scanned": 0, "as_of": None, "stale": 0,
        "buckets": [], "targets": [],
        "stale_before": threshold.isoformat(), "basis": basis,
        "tolerance_days": max(0, int(tolerance_days)),
        "market_today": market_today.isoformat(),
    }
    if latest.is_empty():
        return plan
    days = [_plain_date(v) for v in latest["latest_date"].to_list()]
    symbols = latest["symbol"].to_list()
    observed = [day for day in days if day is not None]
    as_of = max(observed) if observed else None
    stale_rows = [(s, d) for s, d in zip(symbols, days, strict=True)
                  if d is not None and d < threshold]
    counts = Counter(day for _, day in stale_rows)
    # 桶降序 → 桶内 symbol 升序, 保证同输入同输出 (可重放)
    order = {day: index for index, (day, _) in enumerate(
        sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))}
    stale_rows.sort(key=lambda row: (order[row[1]], row[0]))
    buckets = [
        {"latest_date": day.isoformat(), "symbols": count}
        for day, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_BUCKET_TOP_N]
    ]
    targets = stale_rows[:limit] if limit and limit > 0 else stale_rows
    plan.update({
        "scanned": latest.height,
        "as_of": as_of.isoformat() if as_of is not None else None,
        "stale": len(stale_rows),
        "buckets": buckets,
        "targets": [{"symbol": symbol, "latest_date": day.isoformat()} for symbol, day in targets],
    })
    return plan


def _resolve_fetch(market: str, *, calendar_window_days: int | None = None) -> tuple[Any, Callable[..., Any]]:
    """取现役 provider 的逐标的报告接口, 缺接口显式报错 (不降级成静默空跑)。

    必须走 ``get_daily_with_report``: 补拉要拿**本次的复权因子** (coverage_end
    = 本次 actual_end) 才能落 enriched, ``get_daily`` 只回 frame、丢因子。

    ``calendar_window_days`` 给定时注入到现役 provider 实例 (日历校验只拉近段):
    补拉的维护窗口起点逐标的各异 (1998/2015/2020…), 每个起点都是一组新的
    hkHSI 日历请求 (365 天一段, 28 年历史 ≈28 个请求), 1526 只批跑必然把腾讯
    打出 WAF 熔断 (2026-09-24 实证: 第 80 只处熔断, 此后 1344 只新浪单源)。
    日历窗口与腾讯兜底窗口同宽 (120 天) —— 深历史覆盖缺口由 merge 侧
    ``_legacy_gap_tolerable`` 三重护栏兜底, 不依赖日历。**注入现役实例而非
    新建**: 注册表是单例语义, 新建会丢共享熔断状态。
    """
    from app.data_providers.registry import get_default_provider

    provider = get_default_provider(market, dataset="daily")
    fetch = getattr(provider, "get_daily_with_report", None)
    if not callable(fetch):
        raise RuntimeError(
            f"{market} 默认日线源 {type(provider).__name__} 未提供逐标的报告接口 "
            f"get_daily_with_report; 本脚本当前只服务港股补拉"
        )
    if calendar_window_days is not None and hasattr(provider, "calendar_window_days"):
        try:
            provider.calendar_window_days = max(1, int(calendar_window_days))
        except Exception as exc:  # 属性只读等异常: 不阻断补拉, 只是回到全窗口校验
            logger.warning("日历窗口注入失败 (%s), 保持默认全窗口校验", exc)
    return provider, fetch


def _tencent_circuit_remaining(now: float | None = None) -> float:
    """腾讯 WAF 熔断剩余秒数 (0 = 未熔断)。

    直接读现役 provider 的模块级 ``_TENCENT_CIRCUIT`` dict —— 锁外读是安全的
    (只读不写; dict 单键读取在 CPython 下原子)。批循环用它感知熔断, 避免在
    冷却期内空烧: 现役熔断器的语义是"被拦期间继续请求既浪费配额又延长封禁"
    (hk_daily_provider.py:107-110), 批跑必须在冷却期主动暂停。

    ``now`` 参数化是为了让循环的时钟与判定同一域 (熔断的 blocked_until 本身
    就是 ``time.monotonic()`` 域); 生产默认即 ``time.monotonic``。
    """
    try:
        from app.data_providers.hk_daily_provider import _TENCENT_CIRCUIT
    except Exception:  # provider 不在 (非港股市场): 无熔断可言
        return 0.0
    try:
        blocked_until = float(_TENCENT_CIRCUIT.get("blocked_until") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, blocked_until - (time.monotonic() if now is None else now))


def publish_hk_snapshot(
    root: Path, symbol: str, frame: pl.DataFrame, *, factors: pl.DataFrame,
    item: dict, legacy: pl.DataFrame, verification_archives: list[dict],
) -> dict:
    """现役落库路径: raw + 复权因子 + enriched 一次性原子发布 (港股分支同款)。"""
    from app.services.hk_data_adapter import publish_hk_daily_snapshot

    return publish_hk_daily_snapshot(
        root, symbol, frame, factors=factors, item=item, legacy=legacy,
        verification_archives=verification_archives,
    )


def _maintenance_start(
    root: Path, symbol: str, legacy: pl.DataFrame, fallback: date,
) -> date:
    """该标的的**维护窗口**起点 = 现有历史最早日 (无历史则退回 fallback)。

    对齐现役 ``kline_sync`` 港股分支的 ``maintained_start`` (它同样把 start 前
    移到 ``previous["date"].min()``)。

    为什么不能只拉 ``latest+1`` 增量 (2026-09-23 实跑 50/50 全失败的根因):
    现役 publish 走 ``merge_market_daily_frames(old, incoming, replace_legacy=True)``,
    当旧分区**未核实口径** (legacy / 早期分区) 时要求 incoming 覆盖旧分区已确认
    交易日 (``_legacy_gap_tolerable`` 三道护栏)。纯增量只有十几天, ``missing``
    是几千天 ⇒ 被 fail-closed 守卫拒。**守卫本身是对的** (防窄窗口数据把历史洗
    掉), 正确做法是放宽请求窗口, 让 incoming 自带完整历史 —— 不碰守卫。

    代价: 新浪一次请求返回全历史再在内存按 [start,end] 过滤 ⇒ 放宽窗口**不增加
    任何网络请求** (同一 URL); 腾讯兜底窗口恒为 ``max(start, end-120天)``, 同样
    不受 start 前移影响。

    为什么不改成"旧历史行 ∪ 增量"的并集: publish 的 ``is_verified_hk_raw(raw)``
    是**整帧逐行 AND** 判定 (price_schema_version/raw_price_verified/
    price_adjustment/volume_unit/currency 五行全过), 掺入未核实的旧行会让整帧
    判为未核实 → raise "币种、量单位或价格口径未核实"; 要给旧行补这些标记等于
    凭空声明它的来源口径 —— 而旧分区被拦正是因为口径未知 ⇒ 属伪造, 违反不伪造
    铁律。故宁可多拉一次 (网络成本为零), 也不给旧行贴标签。
    """
    from app.tickflow.market_daily import read_market_daily_symbol

    try:
        previous = read_market_daily_symbol(root, symbol, legacy=legacy)
    except Exception as exc:
        logger.warning("维护窗口起点读取失败 %s (退回 latest+1): %s", symbol, exc)
        return fallback
    if previous.is_empty():
        return fallback
    earliest = _plain_date(previous["date"].min())
    return fallback if earliest is None else min(earliest, fallback)


def _market_today(market: str) -> date:
    """市场当天 (非宿主机当天): 与全站市场时钟口径同源, 模块属性调用便于测试钉值。"""
    from app.markets import registry as market_registry

    return market_registry.get_profile(market).today()


_CURRENCY_EVIDENCE_ORDER = ("quote", "partition", "hkex")
_HK_CURRENCIES = frozenset({"HKD", "CNY", "USD"})


def _hkex_currency(root: Path, symbol: str) -> str | None:
    """HKEX 证券清单的 Trading Currency (权威第三方, 只读)。

    ``instruments/hk_instruments.parquet`` 由现役 ``sync_hk_lot_sizes`` 维护
    (``hkex_instruments.py:51-53``: RMB→CNY 归一, 白名单 {HKD,CNY,USD})。
    读取失败/清单缺失返回 None —— 币种查不到走"跳过并报因", 绝不默认 HKD。
    """
    path = root / "instruments" / "hk_instruments.parquet"
    if not path.exists():
        return None
    try:
        frame = pl.read_parquet(path, columns=["symbol", "currency"])
    except Exception as exc:
        logger.warning("HKEX 证券清单读取失败 %s: %s", symbol, exc)
        return None
    if frame.is_empty():
        return None
    hit = frame.filter(pl.col("symbol") == symbol)
    if hit.is_empty():
        return None
    values = [str(v).strip().upper() for v in hit["currency"].drop_nulls().to_list()
              if v is not None]
    unique = sorted(set(values))
    if len(unique) != 1 or unique[0] not in _HK_CURRENCIES:
        return None
    return unique[0]


def _partition_currency(old: pl.DataFrame) -> str | None:
    """旧分区已核实的 currency (唯一值才可信, 冲突/缺失返回 None)。"""
    if old.is_empty() or "currency" not in old.columns:
        return None
    unique = sorted({str(v).strip().upper() for v in old["currency"].drop_nulls().to_list()
                     if v is not None})
    if len(unique) != 1 or unique[0] not in _HK_CURRENCIES:
        return None
    return unique[0]


def _ensure_currency_identity(
    frame: pl.DataFrame, root: Path, symbol: str, old: pl.DataFrame, item: dict,
) -> tuple[pl.DataFrame, str | None]:
    """给本次 publish 的 incoming frame 补 currency 身份列 (只补 currency!)。

    为什么只有 currency 需要补: ``_validated_rows`` (hk_daily_provider.py:258-261)
    给新浪/腾讯/东财三条链路的**每一行**都恒产 ``price_adjustment="unadjusted"`` /
    ``volume_unit="share"`` / ``price_schema_version=1`` / ``raw_price_verified=True``;
    唯独 ``currency`` 初始为 None (:260), 全链路只有腾讯报价 ``quote[75]``
    (:466-475) 能填 —— 腾讯 WAF 熔断时 currency 整列 null, publish 的
    "从旧已核实分区继承"旁路 (:991-1000) 又只救"旧分区本身已核实"的标的。
    8 列老分区 (1425/1529 只) 两头都空 → 守卫 (:1001) 拒。

    currency 取值**证据链** (先强后弱, 任何一步拿到即止, 拿不到返回原 frame):
      1. 本次腾讯报价 (``item["currency"]``) —— 与 OHLCV 同次请求、最强;
      2. 旧分区已核实的 currency —— 上市主体币种不变 (与 publish 旁路同理);
      3. HKEX 证券清单 Trading Currency —— 官方权威, 只读不写。
    三层全空 → 返回 None, 由调用方计入 no_identity 单独归类, **绝不默认 HKD**
    (24 只 CNY / 1 只 USD, 默认 HKD 会写错 25 只)。

    只影响本脚本的补拉路径: publish_hk_daily_snapshot 的公共语义不变
    (它自己那份 currency 全 null 旁路原样保留), 其他调用方零感知。
    """
    if frame.is_empty() or "currency" not in frame.columns:
        return frame, None
    if frame.get_column("currency").null_count() == 0:
        return frame, frame["currency"][0]
    source = "none"
    currency: str | None = None
    quote = str(item.get("currency") or "").strip().upper()
    if quote in _HK_CURRENCIES:
        currency, source = quote, _CURRENCY_EVIDENCE_ORDER[0]
    if currency is None:
        inherited = _partition_currency(old)
        if inherited is not None:
            currency, source = inherited, _CURRENCY_EVIDENCE_ORDER[1]
    if currency is None:
        official = _hkex_currency(root, symbol)
        if official is not None:
            currency, source = official, _CURRENCY_EVIDENCE_ORDER[2]
    if currency is None:
        return frame, None
    logger.info("hk %s: currency=%s (来源: %s)", symbol, currency, source)
    return frame.with_columns(pl.lit(currency, dtype=pl.String).alias("currency")), currency


def repair_stale(
    data_dir: Path,
    market: str = "HK",
    limit: int | None = None,
    *,
    dry_run: bool = True,
    sleep_seconds: float = _DEFAULT_SLEEP_SECONDS,
    today: date | None = None,
    stale_before: date | None = None,
    tolerance_days: int = _DEFAULT_TOLERANCE_DAYS,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    publisher: Callable[..., dict] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    batch_pause: Callable[[int, int, dict[str, Any]], None] | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """补拉 stale 标的。dry_run=True 时只算计划, 不发请求、不写盘。

    熔断感知: 每只开始前查腾讯 WAF 熔断状态, 冷却期内暂停等待 (上限
    ``_CIRCUIT_WAIT_MAX_SECONDS``, 超时跳过该只), 不在封禁期空烧请求。

    Returns:
        报告 dict: scanned / as_of / stale / stale_before / basis / buckets /
        attempted / succeeded / failed / failures / no_data / circuit_pauses /
        elapsed_seconds / seconds_per_symbol / estimate_*。
    """
    started = time.monotonic()
    now = clock or time.monotonic
    do_sleep = sleeper or time.sleep
    plan = compute_stale_plan(
        data_dir, market, limit=limit, stale_before=stale_before,
        tolerance_days=tolerance_days, today=today,
    )
    report: dict[str, Any] = {
        **plan, "dry_run": dry_run, "limit": limit or 0,
        "attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0,
        "no_data": 0, "no_data_symbols": [], "no_identity": 0, "no_identity_symbols": [],
        "repair_blocked": 0, "repair_blocked_symbols": [],
        "circuit_pauses": 0, "circuit_wait_seconds": 0.0,
        "failures": [], "elapsed_seconds": 0.0,
    }
    targets = plan["targets"]
    if dry_run or not targets:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _attach_estimate(report, measured=False)
        return report

    root = Path(data_dir)
    _, fetch = _resolve_fetch(market, calendar_window_days=_CALENDAR_WINDOW_DAYS)
    from app.services.hk_data_adapter import load_hk_raw_verification_archives
    from app.tickflow.market_daily import read_legacy_market_daily

    symbols = [entry["symbol"] for entry in targets]
    legacy = read_legacy_market_daily(root, market, symbols)
    archives = load_hk_raw_verification_archives(root, symbols)
    end_day = today or _market_today(market)
    publish = publisher or publish_hk_snapshot
    batch = max(1, int(batch_size))

    for index, entry in enumerate(targets, start=1):
        symbol = entry["symbol"]
        latest = date.fromisoformat(entry["latest_date"])
        # 补拉区间 = 维护窗口起点 → 市场当天。窗口必须覆盖 [latest+1, today]
        # 这段缺口; 起点前移到"现有历史最早日"是 publish 的 legacy 合并守卫
        # 要求 (详见 _maintenance_start: 纯增量会被判 missing 几千天而拒)。
        start_day = _maintenance_start(root, symbol, legacy, latest + timedelta(days=1))
        if progress is not None:
            progress(index, len(targets), symbol)
        if start_day > end_day:
            report["skipped"] += 1
            continue
        # 熔断感知: 冷却期内不发请求 (发了也会被本地快速失败, 还会刷新封禁)。
        # 等待有上限 —— 超过一档最大冷却还没解封 = 源长时间不可用, 跳过该只
        # 继续批 (幂等可续跑, 源恢复后重跑同命令即补上)。
        remaining = _tencent_circuit_remaining(now())
        if remaining > 0:
            wait = min(remaining, _CIRCUIT_WAIT_MAX_SECONDS)
            report["circuit_pauses"] += 1
            report["circuit_wait_seconds"] = round(
                float(report["circuit_wait_seconds"]) + wait, 1)
            logger.warning(
                "腾讯 WAF 熔断冷却中 (剩余 %.0f 分钟), 暂停 %.0f 分钟后继续 (上限 %.0f 分钟)",
                remaining / 60, wait / 60, _CIRCUIT_WAIT_MAX_SECONDS / 60)
            do_sleep(wait)
            if _tencent_circuit_remaining(now()) > 0:
                logger.warning(
                    "熔断等待超上限仍未解封, 跳过 %s (重跑可续补)", symbol)
                report["skipped"] += 1
                continue
        report["attempted"] += 1
        try:
            fetched = fetch(
                [symbol],
                start_time=datetime.combine(start_day, _time.min),
                end_time=datetime.combine(end_day, _time.min),
                asset_type="stock",
                verification_archives=[a for a in archives if a.get("symbol") == symbol],
            )
            frame = (fetched.frame.filter(pl.col("symbol") == symbol)
                     if not fetched.frame.is_empty() else pl.DataFrame())
            if frame.is_empty():
                # 源侧没有该标的这段窗口的数据 (停牌/退市/源缺) —— 与"发布失败"
                # 是两类问题, 单独归类, 不计入 failed (用户需要区分对待)。
                report["no_data"] += 1
                report["no_data_symbols"].append(symbol)
                logger.info("源未返回 %s 的日线 (停牌/退市/源缺), 跳过", symbol)
                continue
            factors = (fetched.adjustments.filter(pl.col("symbol") == symbol)
                       if not fetched.adjustments.is_empty() else pl.DataFrame())
            items = {row["symbol"]: dict(row) for row in fetched.items}
            item = items.get(symbol, {"symbol": symbol})
            # 第三层守卫的前置修复: 8 列老分区 + 腾讯熔断时 currency 全 null,
            # publish 会以"口径未核实"拒。这里从证据链补 currency (其余 4 列
            # provider 恒产), 三层证据全空则单独归类跳过, 绝不默认 HKD。
            old_identity = None
            if market.upper() == "HK":
                try:
                    from app.tickflow.market_daily import read_market_daily_symbol
                    old_identity = read_market_daily_symbol(root, symbol, legacy=legacy)
                except Exception as exc:
                    logger.warning("旧分区读取失败 %s (币种继承跳过): %s", symbol, exc)
                frame, currency = _ensure_currency_identity(
                    frame, root, symbol, old_identity, item)
                if currency is None:
                    report["no_identity"] += 1
                    report["no_identity_symbols"].append(symbol)
                    logger.warning(
                        "跳过 %s: currency 三层证据 (腾讯报价/旧分区/HKEX 清单) 全空, "
                        "不肯默认 HKD 落盘; 请先同步标的池 (sync_hk_lot_sizes)", symbol)
                    continue
                item = {**item, "currency": currency}
            published = publish(
                root, symbol, frame, factors=factors, item=item, legacy=legacy,
                verification_archives=[a for a in getattr(fetched, "verification_archives", ())
                                       if a.get("symbol") == symbol],
            )
            if published.get("status") in {"ok", "unchanged"}:
                report["succeeded"] += 1
            else:
                report["failed"] += 1
                report["failures"].append({
                    "symbol": symbol,
                    "reason": published.get("reason") or f"发布状态 {published.get('status')}",
                })
        except Exception as exc:  # 单只失败记录后继续, 不中断整批
            if "旧价格口径维护窗口尚未具备完整原始价与复权因子" in str(exc):
                # 第四层守卫 (:1041): repair 分区 (口径未核实的旧分区) 在 enriched
                # 算不出来时 raise 拒绝替换原文件 —— 这是**对的** fail-closed
                # (坏了不如不动)。触发链: 近 90 天真缺口 (长期停牌如 00167,
                # 新浪对停牌段无行) → coverage_ok=False → enriched 空 → repair → 拒。
                # 源侧缺口修管道修不出来 → 单独归类, 不混 failures。
                report["repair_blocked"] += 1
                report["repair_blocked_symbols"].append(symbol)
                logger.info(
                    "%s: 近期覆盖不全, 维护窗口不具备完整原始价+复权因子, "
                    "publish 保留原文件 (源侧缺口, 非管道故障)", symbol)
            else:
                report["failed"] += 1
                report["failures"].append({"symbol": symbol, "reason": str(exc)})
                logger.warning("补拉失败 %s: %s", symbol, exc)
        if sleep_seconds > 0:
            do_sleep(sleep_seconds)
        # 批间暂停 + 进度摘要: 用户观察/中断点 (幂等可续跑, Ctrl-C 后重跑同命令,
        # 已成功标的自动跳过)。最后一批结束不打 (print_report 已有完整摘要)。
        if index % batch == 0 and index < len(targets):
            logger.info(
                "进度 %d/%d | 成功 %d | 失败 %d | 源无数据 %d | 币种缺证据 %d | "
                "维护窗口不全 %d | 熔断暂停 %d 次",
                index, len(targets), report["succeeded"], report["failed"],
                report["no_data"], report["no_identity"], report["repair_blocked"],
                report["circuit_pauses"])
            if batch_pause is not None:
                batch_pause(index, len(targets), report)
            else:
                do_sleep(_BATCH_PAUSE_SECONDS)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    _attach_estimate(report, measured=report["attempted"] > 0)
    return report


def _attach_estimate(report: dict[str, Any], *, measured: bool) -> None:
    """给报告挂上"单只耗时 + 剩余/全量预计" (用户要拿它决定跑多大批量)。

    ``measured=True`` 时用本次实测均值 (有真实样本就不用经验值);
    dry-run 没有样本, 退回 ``_MEASURED_SECONDS_PER_SYMBOL`` 并在报告里标明
    是经验值, 免得用户拿它当 SLA。
    """
    attempted = int(report.get("attempted") or 0)
    if measured and attempted > 0:
        per = round(float(report["elapsed_seconds"]) / attempted, 3)
    else:
        per = float(_MEASURED_SECONDS_PER_SYMBOL)
    report["seconds_per_symbol"] = per
    report["seconds_per_symbol_measured"] = bool(measured and attempted > 0)
    stale = int(report.get("stale") or 0)
    report["estimate_remaining_seconds"] = round(max(0, stale - attempted) * per, 1)
    report["estimate_all_seconds"] = round(stale * per, 1)


def _format_duration(seconds: float) -> str:
    """秒数说人话 (用户要决定批量大小, 别让他自己按计算器)。"""
    value = float(seconds)
    if value < 60:
        return f"约 {value:.0f} 秒"
    if value < 3600:
        return f"约 {value / 60:.1f} 分钟"
    if value < 86400:
        return f"约 {value / 3600:.1f} 小时"
    return f"约 {value / 86400:.1f} 天"


def print_report(report: dict[str, Any]) -> None:
    """打印 before/after 报告 (人读; --json 时给机器读)。"""
    print(f"市场            : {report['market']}")
    print(f"扫描标的数      : {report['scanned']}")
    print(f"全市场 as_of    : {report['as_of']} (现状指标, 不作判定基准)")
    basis = report.get("basis")
    basis_text = {"stale-before": "--stale-before 显式指定"}.get(
        basis, f"市场当天 {report.get('market_today')} - {report.get('tolerance_days')} 天容差")
    print(f"stale 基准      : 最新交易日 < {report.get('stale_before')} ({basis_text})")
    print(f"stale 标的数    : {report['stale']}")
    buckets = report.get("buckets") or []
    if buckets:
        detail = ", ".join(f"{b['symbols']}@{b['latest_date']}" for b in buckets)
        print(f"峰值档 (前 {len(buckets)})   : {detail}")
    print(f"本次拟补拉      : {len(report['targets'])}" + (f" (limit={report['limit']})" if report.get("limit") else ""))
    per = report.get("seconds_per_symbol") or 0.0
    kind = "本次实测" if report.get("seconds_per_symbol_measured") else "经验值"
    print(f"单只耗时        : {per} 秒/只 ({kind})")
    print(f"剩余预计        : {_format_duration(report.get('estimate_remaining_seconds') or 0.0)}"
          f" (还剩 {max(0, int(report.get('stale') or 0) - int(report.get('attempted') or 0))} 只)")
    print(f"全量预计        : {_format_duration(report.get('estimate_all_seconds') or 0.0)}"
          f" (全部 {report.get('stale')} 只)")
    if report.get("dry_run"):
        print("模式            : dry-run (未发请求、未写盘; 加 --yes 才真正补拉)")
        return
    print(f"实际请求        : {report['attempted']} (跳过 {report['skipped']})")
    print(f"成功            : {report['succeeded']}")
    print(f"失败            : {report['failed']}")
    for failure in report["failures"][:20]:
        print(f"  - {failure['symbol']}: {failure['reason']}")
    if len(report["failures"]) > 20:
        print(f"  ... 另有 {len(report['failures']) - 20} 条失败")
    if report.get("no_data"):
        # 与发布失败是两类问题: 源侧根本没有这段窗口的数据 (停牌/退市/源缺),
        # 不是我们的管道坏了 —— 单独列出, 别混进 failures 让用户误判。
        print(f"源无数据        : {report['no_data']} (停牌/退市/源缺, 非管道故障)")
        print(f"  {', '.join(report['no_data_symbols'][:20])}"
              + (f" ... 另有 {report['no_data'] - 20} 只" if report['no_data'] > 20 else ""))
    if report.get("no_identity"):
        # currency 三层证据全空: 落盘会触发 publish 的口径守卫 (或不肯默认 HKD)。
        # 不是失败, 是"身份证据不足" —— 同步标的池后重跑即可补上。
        print(f"币种证据不足    : {report['no_identity']} (跳过不落盘; 同步标的池后重跑)")
        print(f"  {', '.join(report['no_identity_symbols'][:20])}"
              + (f" ... 另有 {report['no_identity'] - 20} 只" if report['no_identity'] > 20 else ""))
    if report.get("repair_blocked"):
        # 第四层守卫: 近期覆盖不全, publish 拒绝替换口径未核实的旧分区 (fail-closed
        # 保住了原文件)。这批标的源侧真没有完整的近期数据 (多为长期停牌), 等
        # 源补齐后重跑即可, 修管道修不出来。
        print(f"维护窗口不全    : {report['repair_blocked']} (近期覆盖缺口, publish 保留原文件)")
        print(f"  {', '.join(report['repair_blocked_symbols'][:20])}"
              + (f" ... 另有 {report['repair_blocked'] - 20} 只" if report['repair_blocked'] > 20 else ""))
    if report.get("circuit_pauses"):
        print(f"熔断暂停        : {report['circuit_pauses']} 次 (累计等待 {_format_duration(report.get('circuit_wait_seconds') or 0.0)})")
    print(f"耗时(秒)        : {report['elapsed_seconds']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="港股 stale 日K 补拉 (方案 X, 手动触发)")
    parser.add_argument("--data-dir", required=True, help="数据根目录 (含 kline_hk_us_enriched/)")
    parser.add_argument("--market", default="HK", help="市场代码 (默认 HK)")
    parser.add_argument("--limit", type=int, default=0, help="本次最多补拉只数 (0=全部)")
    parser.add_argument("--stale-before", default=None, metavar="YYYY-MM-DD",
                        help="只补最新交易日早于该日期的标的 (给定时它就是判定基准, 不再叠加容差)")
    parser.add_argument("--tolerance-days", type=int, default=_DEFAULT_TOLERANCE_DAYS,
                        help=f"未给 --stale-before 时的容差自然日: 基准 = 市场当天 - N "
                             f"(默认 {_DEFAULT_TOLERANCE_DAYS}, 覆盖周末与常规节假日)")
    parser.add_argument("--dry-run", action="store_true", help="只出计划, 不写盘 (默认)")
    parser.add_argument("--yes", action="store_true", help="真正执行补拉")
    parser.add_argument("--sleep-seconds", type=float, default=_DEFAULT_SLEEP_SECONDS,
                        help=f"每只之间的间隔秒数 (默认 {_DEFAULT_SLEEP_SECONDS}, 腾讯 WAF 熔断防护)")
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE,
                        help=f"每批只数: 每批结束打进度摘要并暂停观察 (默认 {_DEFAULT_BATCH_SIZE})")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"数据目录不存在: {data_dir}", file=sys.stderr)
        return 2
    stale_before: date | None = None
    if args.stale_before:
        try:
            stale_before = date.fromisoformat(args.stale_before.strip())
        except ValueError:
            print(f"--stale-before 不是合法日期 (YYYY-MM-DD): {args.stale_before}", file=sys.stderr)
            return 2
    dry_run = not args.yes or args.dry_run
    try:
        report = repair_stale(
            data_dir, args.market, args.limit or None,
            dry_run=dry_run, sleep_seconds=max(0.0, args.sleep_seconds),
            stale_before=stale_before, tolerance_days=max(0, args.tolerance_days),
            batch_size=max(1, args.batch_size),
        )
    except Exception as exc:
        print(f"补拉失败: {exc}", file=sys.stderr)
        return 1
    if args.json:
        import json

        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
