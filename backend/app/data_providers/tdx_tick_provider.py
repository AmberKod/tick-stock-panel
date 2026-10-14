"""通达信标准市(TCP 7709) ``GetHistoryTransactionDataCmd`` —— A股**真逐笔成交**。

为什么要有它(2026-10-14 实测):
    在此之前个股页「成交明细」走的是腾讯 3 秒成交快照(stock.gtimg.cn 那一路),
    面板上明写"每 3 秒一行、不是单笔成交、真正的逐笔需要 Level-2 付费"。
    通达信这条命令给的就是**逐笔**, 且不需要 Level-2 授权。

协议行为(**全部实测, 别照网上博客猜**):
- 当日命令 ``GetTransactionDataCmd``(0x0f50) 在这批节点上恒回 0 条 → **不用**;
  历史命令 ``GetHistoryTransactionDataCmd``(0x0fb5) 对**当日同样有效**
  (收盘后当天就能拉到, 实测 2026-10-14 20:30 已可拉当日 600519)。
- 🔴 **``start=0`` 返回的是"最后 count 条"**, 且**页内已是时间正序**;
  ``start`` 递增是往**更早**翻(start=800 = 倒数第 800 条起)。
  所以"最近 N 笔"= 单次 ``start=0, count=N``, 只要 N ≤ 800 就不必翻全量。
- 🔴 **拼整天必须重排**: 页序是 新→旧, 但**页内是正序**, 按原生顺序拼会乱。
  正确 key 是 ``(-page, 页内 idx)`` —— 实测重排后时间**零逆序对**。
- 🔴 ``buyorsell`` 取值域是 ``{0,1,2,5,8}``, 比 vendor 模型注释(0/1/2/8)多一个 5:
      ``0``=买盘成交  ``1``=卖盘成交  ``2``=集合竞价撮合(09:25 开盘 / 15:00 收盘)
      ``5``=盘后(15:05 之后, 价格恒等于收盘价)
      ``8``=**无成交的行情标记行, vol 恒为 0** → 必须剔除, 否则会出现假成交。
- vol 单位是**手**; 时间精度只到分钟(协议限制)。

交叉校验(**与 0x240D / 0x240C 同策略, 每次都给出结论**):
    与腾讯日K咬合 —— 两支样本 **0.0000%**:
      600519 2026-10-13: 盘中 vol 合计 26573 == 日K 26573; 末笔 15:00 p=1274.89 == 收盘
      300750 2026-10-12: 盘中 vol 合计 417064 == 日K 417064; 末笔 15:00 p=298.38 == 收盘
    盘后(bs=5)那几笔**不在日K成交量里**, 是独立一段, 单列、不混进盘中合计。

法务边界: 与 tdx_ex_provider 一致, 只用上游的**在线协议**部分; 离线模块
(``offline/gbbq.py``, XOR 密钥移植自无 license 的 pytdx)已在 vendor 时整目录删除。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import TypeVar

import httpx

from app._vendor.easy_tdx.commands.transaction import GetHistoryTransactionDataCmd
from app._vendor.easy_tdx.models.enums import Market
from app._vendor.easy_tdx.transport.sync import TdxConnection

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 登记在 app/services/source_gate.py 白名单里的源名。
SOURCE = "tdx-tick"

#: 协议单次上限 —— count > 800 服务端照样只回 800。
MAX_TAIL = 800

#: 全量模式翻页安全阀: 40 页 = 32000 笔, 已超过绝大多数 A股一天的笔数。
MAX_PAGES = 40

#: 会话切分(分钟数)。A股: 09:25 开盘竞价撮合, 15:00 收盘。
_OPEN_AUCTION_MAX_MIN = 9 * 60 + 25
_CLOSE_MIN = 15 * 60

_TENCENT_DAILY_URL = "https://ifzq.gtimg.cn/appstock/app/kline/kline"
_TENCENT_TIMEOUT_S = 12.0

#: 单位与口径说明 —— 前端必须摆出来。
_NOTE_UNITS = (
    "成交量单位是**手**(1 手 = 100 股), 与日K口径一致; "
    "时间精度只到分钟, 同一分钟内的先后沿用上游顺序, 不是秒级 tick。"
)
_NOTE_DIRECTION = (
    "买卖方向是行情商按成交价与买卖盘对比判定的, **不是交易所披露字段**; "
    "标 '竞价' 的是 09:25 开盘集合竞价与 15:00 收盘集合竞价的撮合成交。"
)
_NOTE_AFTER_HOURS = (
    "15:00 之后的成交标 '盘后': 恒等于当日收盘价, 通常是盘后固定价格交易/大宗交易, "
    "**不计入当日成交量**, 因此这部分量不入下面的盘中合计。"
)


class TdxTickUnavailableError(Exception):
    """逐笔数据不可用 —— 端点转成 ``available:false``, 不向上抛 5xx。"""


_cache: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()


def clear_cache() -> None:
    _cache.clear()


def _settings():
    from app.config import settings

    return settings


def _ttl() -> float:
    try:
        return float(_settings().tdx_tick_cache_ttl_s)
    except Exception:
        return 60.0


def _hosts() -> list[str]:
    try:
        raw = _settings().tdx_std_hosts
    except Exception:
        return []
    return [h.strip() for h in (raw or "").split(",") if h.strip()]


def _port() -> int:
    try:
        return int(_settings().tdx_std_port)
    except Exception:
        return 7709


def _timeout() -> float:
    try:
        return float(_settings().tdx_std_timeout_s)
    except Exception:
        return 8.0


def _cached(key: str) -> dict | None:
    with _LOCK:
        hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _put(key: str, value: dict) -> None:
    with _LOCK:
        _cache[key] = (time.time() + _ttl(), value)


_SH_PREFIXES = ("60", "68", "90")
_SZ_PREFIXES = ("00", "30", "20")
_BJ_PREFIXES = ("43", "83", "87", "88", "92")


def _market_for(code: str) -> Market:
    """6 位代码 → 通达信 market; 北交所逐笔未实测 → fail-closed。

    与 ``tdx_xdxr_provider._market_for`` 同口径(判两位前缀), 因为 "9" 既是沪 B
    又是北交所, 只判首位会把北交所当成沪市去问。
    """
    if len(code) == 6 and code.isdigit():
        prefix = code[:2]
        if prefix in _SH_PREFIXES:
            return Market.SH
        if prefix in _SZ_PREFIXES:
            return Market.SZ
    raise TdxTickUnavailableError(
        f"bad_code: {code!r} 不是沪深 A股代码"
        + ("(北交所逐笔暂不支持: 未实测)" if code[:2] in _BJ_PREFIXES else "")
    )


def _exchange(code: str) -> str:
    return "sh" if code[:2] in _SH_PREFIXES else "sz"


def _exec(cmd_factory) -> T:
    """连一个节点执行一次命令; 失败逐个降级。

    标准市不需要登录, 但对不支持/超限命令同样会直接断连接 —— 所以与 xdxr 一致,
    **每条命令一次连接**, 不在同一连接里复用第二个命令。
    """
    hosts = _hosts()
    if not hosts:
        raise TdxTickUnavailableError("no_host: 未配置通达信标准市节点")
    errors: list[str] = []
    for host in hosts:
        try:
            with TdxConnection(host, port=_port(), timeout=_timeout()) as conn:
                return conn.execute(cmd_factory())
        except Exception as exc:
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
    raise TdxTickUnavailableError("connect_failed: " + " | ".join(errors))


# ---------------------------------------------------------------------------
# 腾讯日K —— 交叉校验 + 交易日历
# ---------------------------------------------------------------------------


def _tencent_daily(code: str, count: int = 10) -> list[dict]:
    """腾讯不复权日K, 升序 ``[{date, open, close, high, low, volume}]``。

    为什么走腾讯而不是 ``GetSecurityBarsCmd``: 能回逐笔/xdxr 的那批 7709 属于
    **信息服务器**, 对日K一律回 2 字节空包(实测, vendor 解析会直接报错)。
    """
    try:
        resp = httpx.get(
            _TENCENT_DAILY_URL,
            params={"param": f"{_exchange(code)}{code},day,,,{count},"},
            timeout=_TENCENT_TIMEOUT_S,
            follow_redirects=True,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # 交叉源取不到 ≠ 逐笔是假的, 不该 5xx
        logger.info("tencent daily unavailable for %s: %s", code, exc)
        return []
    data = payload.get("data") or {}
    node = next((v for v in data.values() if isinstance(v, dict)), None)
    if not node:
        return []
    rows = node.get("day") or node.get("qfqday") or []
    out: list[dict] = []
    for row in rows:
        try:
            out.append({
                "date": str(row[0]),
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            })
        except (TypeError, ValueError, IndexError):
            continue
    return out


def trading_dates(code: str, limit: int = 10) -> list[str]:
    """最近若干个交易日 ``["YYYY-MM-DD", ...]`` 升序 —— 前端日期下拉用。"""
    return [d["date"] for d in _tencent_daily(code, limit)]


def _norm_date(raw: str | None) -> str | None:
    """``20261013`` / ``2026-10-13`` → ``"2026-10-13"``; 空值原样返回。"""
    if not raw:
        return None
    raw = raw.strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


def _resolve_date(code: str, date: str | None) -> str:
    date = _norm_date(date)
    if date:
        return date
    today = datetime.now().strftime("%Y-%m-%d")
    dates = trading_dates(code, 10)
    if not dates:
        raise TdxTickUnavailableError("no_date: 拿不到交易日历, 且未指定 date")
    # 交易日历的最后一根就是最近交易日 —— 比"今天减一天再回退"可靠:
    # 腾讯日K本身就只给真实交易日, 不留非交易日给用户去踩空。
    if today in dates:
        return today
    return dates[-1]


# ---------------------------------------------------------------------------
# 字段映射
# ---------------------------------------------------------------------------


def _session_of(bs: int, minutes: int) -> str:
    """判断这一笔属于哪个交易时段。"""
    if bs == 5 or minutes > _CLOSE_MIN:
        return "after_hours"
    if bs == 2:
        return "open_auction" if minutes <= _OPEN_AUCTION_MAX_MIN else "close_auction"
    return "continuous"


def _side_of(bs: int) -> str:
    if bs == 0:
        return "buy"
    if bs == 1:
        return "sell"
    if bs == 2:
        return "auction"
    return "unknown"


def _payload(rec, index: int) -> dict:
    minutes = rec.hour * 60 + rec.minute
    return {
        "seq": index,
        "time": f"{rec.hour:02d}:{rec.minute:02d}",
        "minutes": minutes,
        "price": round(rec.price, 4),
        "vol": int(rec.vol),
        "side": _side_of(rec.buyorsell),
        "session": _session_of(rec.buyorsell, minutes),
    }


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def _fetch_page(code: str, market: Market, date_int: int, start: int, count: int):
    return _exec(lambda: GetHistoryTransactionDataCmd(market, code, date_int, start, count))


def _fetch_rows(code: str, market: Market, date_int: int, want: int):
    """从末尾取行, 直到**过滤标记行之后**仍有 ``want`` 笔真成交。

    ``start=0`` 的语义已经是"最后 count 条", 所以只需要把 count 调大就行 ——
    但标记行密度不均匀(15:00 之后几乎全是标记行), 得循环补取, 不能一次定死。
    """
    count = min(want * 2 + 80, MAX_TAIL)
    while True:
        try:
            rows = _fetch_page(code, market, date_int, 0, count)
        except TdxTickUnavailableError:
            raise
        except Exception as exc:
            raise TdxTickUnavailableError(f"upstream_error: {type(exc).__name__}: {exc}") from exc
        got = sum(1 for r in rows if r.buyorsell != 8)
        if got >= want or count >= MAX_TAIL:
            return rows
        count = min(count * 2, MAX_TAIL)


def _cross_check_last_price(code: str, date: str, trades: list[dict]) -> dict:
    """末笔盘中成交价 vs 腾讯日K收盘价。

    注意: **只有全量模式**才校验成交量(最近 N 笔本来就不是一天的量),
    量校验放在 ``day_summary`` 里做。
    """
    daily = next((d for d in _tencent_daily(code, 10) if d["date"] == date), None)
    if not daily:
        return {
            "available": False,
            "reason": f"cross_source_unavailable: 腾讯日K没有 {date} 这根(节假日/停牌/上游不通)",
        }
    regular = [t for t in trades if t["session"] != "after_hours"]
    if not regular:
        return {"available": False, "reason": "no_regular_trade: 当天没有盘中成交"}
    last = regular[-1]
    diff = (last["price"] - daily["close"]) / daily["close"] * 100 if daily["close"] else None
    return {
        "available": True,
        "daily_date": daily["date"],
        "daily_close": daily["close"],
        "last_trade_price": last["price"],
        "last_trade_time": last["time"],
        "close_diff_pct": None if diff is None else round(diff, 6),
        "verified": diff is not None and abs(diff) < 0.01,
        "note": (
            "校验方式: 最后一笔盘中成交(15:00 收盘竞价撮合)必须与腾讯日K收盘价一致, "
            "容差 0.01%。超阈值会置 verified=false —— 不静默。"
        ),
    }


def recent_trades(code: str, date: str | None = None, tail: int = 200) -> dict:
    """最近 ``tail`` 笔逐笔成交(利用 start=0 = 最后 N 条且页内已正序)。

    fail-closed: 代码非法 / 上游无数据 / 全是被剔除的标记行 → 抛
    ``TdxTickUnavailableError``, 由端点转成 ``available:false``。
    """
    code = code.strip()
    market = _market_for(code)
    date = _resolve_date(code, date)
    tail = max(1, min(int(tail), MAX_TAIL))

    key = f"recent|{market}|{code}|{date}|{tail}"
    hit = _cached(key)
    if hit:
        return hit

    date_int = int(date.replace("-", ""))
    rows = _fetch_rows(code, market, date_int, want=tail)
    if rows is None:
        raise TdxTickUnavailableError(f"empty_ticks: {code} {date} 上游无逐笔")

    # bs=8 是"没成交"的行情标记行(vol 恒 0) —— 不剔除会显示成"有价格、量为 0"的假成交。
    # 🔴 tail 必须按**过滤后**的真成交计数: 标记行密度很不均匀(15:00 之后几乎没有
    # 连续竞价, 上游夹了大量标记行), 实测 tail=20 时上游 20 行里只有 7 笔真实成交 ——
    # 直接把上游行数当用户要的行数, 用户会以为"今天只成交了 7 笔"。
    markers = sum(1 for r in rows if r.buyorsell == 8)
    all_trades = [_payload(r, i) for i, r in enumerate(rows) if r.buyorsell != 8]
    trades = all_trades[-tail:]

    if not trades:
        raise TdxTickUnavailableError(
            f"empty_ticks: {code} {date} 上游返回 {len(rows)} 行但全是标记行"
            if markers else f"empty_ticks: {code} {date} 上游无逐笔(非交易日?)"
        )

    regular = [t for t in trades if t["session"] != "after_hours"]
    body = {
        "code": code,
        "date": date,
        "protocol": "GetHistoryTransactionDataCmd(0x0fb5)",
        "trades": trades,
        "count": len(trades),
        "tail": tail,
        # 只看 "上游还有没有更早的成交" —— 不是看上游行数
        "truncated": len(all_trades) > tail,
        "summary": {
            "upstream_rows": len(rows),
            "marker_rows_dropped": markers,
            "regular_count": len(regular),
            "after_hours_count": len(trades) - len(regular),
            "volume": sum(t["vol"] for t in trades),
            "volume_regular": sum(t["vol"] for t in regular),
        },
        "cross_check": _cross_check_last_price(code, date, trades),
        "unit_note": _NOTE_UNITS,
        "direction_note": _NOTE_DIRECTION,
        "after_hours_note": _NOTE_AFTER_HOURS,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result = {"source": SOURCE, "data": body}
    _put(key, result)  # 存包装后的结构: 命中时直接返回, 形状必须一致
    return result


def day_summary(code: str, date: str | None = None) -> dict:
    """整天逐笔 + **成交量**交叉校验(要翻多页, 比 recent_trades 慢)。"""
    code = code.strip()
    market = _market_for(code)
    date = _resolve_date(code, date)

    key = f"day|{market}|{code}|{date}"
    hit = _cached(key)
    if hit:
        return hit

    date_int = int(date.replace("-", ""))
    pages: list[tuple[int, list]] = []
    for page in range(MAX_PAGES):
        try:
            chunk = _fetch_page(code, market, date_int, page * MAX_TAIL, MAX_TAIL)
        except TdxTickUnavailableError as exc:
            if page == 0:
                raise
            logger.info("day_summary %s %s 翻页中断(page=%d): %s", code, date, page, exc)
            break
        if not chunk:
            break
        pages.append((page, chunk))
        # 🔴 不用 "len(chunk) < MAX_TAIL" 判最后一页: 万一某页正好满 800 会提前收工。
        # 让服务端用空包告诉我们到头了(实测越界/非交易日都回空包, 不回垃圾数据)。

    if not pages:
        raise TdxTickUnavailableError(f"empty_ticks: {code} {date} 上游无逐笔")

    # 🔴 页序是 新→旧 而页内是正序 —— 必须按 (-page, 页内 idx) 重排才是时间正序
    keyed = sorted(
        ((p, i, r) for p, chunk in pages for i, r in enumerate(chunk)),
        key=lambda t: (-t[0], t[1]),
    )
    rows = [t[2] for t in keyed]

    markers = sum(1 for r in rows if r.buyorsell == 8)
    trades = [_payload(r, i) for i, r in enumerate(rows) if r.buyorsell != 8]
    if not trades:
        raise TdxTickUnavailableError(f"empty_ticks: {code} {date} 全是标记行({markers})")

    regular = [t for t in trades if t["session"] != "after_hours"]
    after = [t for t in trades if t["session"] == "after_hours"]
    vol_regular = sum(t["vol"] for t in regular)
    high = max(t["price"] for t in trades)
    low = min(t["price"] for t in trades)

    daily = next((d for d in _tencent_daily(code, 10) if d["date"] == date), None)
    if daily:
        vol_diff = ((vol_regular - daily["volume"]) / daily["volume"] * 100
                    if daily["volume"] else None)
        last_price = (regular or trades)[-1]["price"]
        close_diff = ((last_price - daily["close"]) / daily["close"] * 100
                      if daily["close"] else None)
        check = {
            "available": True,
            "daily_close": daily["close"],
            "last_trade_price": last_price,
            "close_diff_pct": None if close_diff is None else round(close_diff, 6),
            "volume_regular": vol_regular,
            "daily_volume": daily["volume"],
            "volume_diff_pct": None if vol_diff is None else round(vol_diff, 6),
            "high_in_ticks": high,
            "low_in_ticks": low,
            "daily_high": daily["high"],
            "daily_low": daily["low"],
            "verified": (
                vol_diff is not None and abs(vol_diff) < 0.01
                and close_diff is not None and abs(close_diff) < 0.01
            ),
            "note": (
                "盘中(≤15:00)逐笔成交量合计必须与腾讯日K成交量相等, 末笔价必须等于收盘价 "
                "—— 600519 / 300750 实测 0.000000%; 盘后那几笔不在日K里, 不入此合计。"
            ),
        }
    else:
        check = {"available": False, "reason": f"cross_source_unavailable: 腾讯日K无 {date}"}

    body = {
        "code": code,
        "date": date,
        "protocol": "GetHistoryTransactionDataCmd(0x0fb5)",
        "trades": trades,
        "count": len(trades),
        "pages": len(pages),
        "summary": {
            "upstream_rows": len(rows),
            "marker_rows_dropped": markers,
            "regular_count": len(regular),
            "after_hours_count": len(after),
            "volume": sum(t["vol"] for t in trades),
            "volume_regular": vol_regular,
            "volume_after_hours": sum(t["vol"] for t in after),
        },
        "cross_check": check,
        "unit_note": _NOTE_UNITS,
        "direction_note": _NOTE_DIRECTION,
        "after_hours_note": _NOTE_AFTER_HOURS,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result = {"source": SOURCE, "data": body}
    _put(key, result)
    return result
