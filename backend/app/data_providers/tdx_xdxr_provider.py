"""通达信标准市(TCP 7709) ``GetXdxrInfoCmd`` —— A股分红送配全事件 + 自建复权系数。

为什么需要它(2026-10-13 实测):
- 本地 ``adj_factor`` 表是空的(TickFlow ``ex_factors`` 对本账号返回 0 行), enriched
  里存的是**不复权价**; 想要前复权只能现算系数。
- 腾讯 fqkline 的 ``qfq``/``hfq`` 参数确实可用 —— 但它给的是"腾讯自己复完的价格",
  没有任何事件明细。**回答不了"这一天为什么跌 44%"**, 也无法自己复核。
  (⚠️ 踩过的坑: ``qfqa``/``hfqa`` 这两个写法腾讯不认, 会静默返回不复权的 ``day`` 键,
   所以别照着老博客写参数名, 用 ``qfq``/``hfq``。)
- ``GetXdxrInfoCmd`` 给的是**原始权益事件**: 日期、每股分红、送转比例、配股价、
  配股比例、前后总/流通股本。这是唯一能复核的输入。

算法(标准交易所除权参考价公式, 不发明新公式):
    C = 除权日前一交易日的不复权收盘价
    r = (C - fenhong + peigujia*peigu) / ((1 + songzhuangu + peigu) * C)
    前复权 factor[k] = ∏(除权日在 k 之后的 r); 后复权 = factor[k] / factor[0]

校验方法(**必须看**, 偏差边界写在 ``_DEVIATION_NOTE``):
    用新浪 ``hfq.js`` 的相邻因子比作基准 —— 新浪给的是累乘因子, 其 ``f_i / f_{i-1}``
    必须等于我们的 ``1/r``。探针在 ``deliverables/xdxr-probe/probe_xdxr_validate.py``。

法务边界: 与 tdx_ex_provider 一致, 只使用上游的**在线协议**部分(声明为独立实现);
离线模块(``offline/gbbq.py``, XOR 密钥移植自无 license 的 pytdx)已在 vendor 时整目录删除。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

import httpx

from app._vendor.easy_tdx.commands.xdxr_info import GetXdxrInfoCmd
from app._vendor.easy_tdx.models.enums import Market
from app._vendor.easy_tdx.transport.sync import TdxConnection

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _settings():
    # 延迟 import: 模块被导入时不强制实例化 Settings(测试里可能只有部分环境变量)。
    from app.config import settings

    return settings

#: 登记在 app/services/source_gate.py 白名单里的源名。
SOURCE = "tdx-xdxr"

#: 腾讯单次日K上限。多取几根是为了让最早那次除权也能拿到"前一交易日"。
MAX_DAILY_BARS_PER_REQUEST = 800
_TX_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/kline"
_TX_TIMEOUT_S = 12.0

#: 复权系数的最小/最大天数, 与腾讯 adjratio 端点对齐。
MIN_FACTOR_DAYS = 60
MAX_FACTOR_DAYS = 800

#: 标准市 TCP 八成以上时间答复都在 2s 内, 8s 足够; 超时立刻换下一个节点。
_TIMEOUT_S = 8.0

#: 事件累积偏差说明 —— **不要删**。回购注销会让名义分红与实际除权对不上,
#: 用户拿这个系数做回测时必须知道自己踩在什么上面。
_DEVIATION_NOTE = (
    "系数由通达信 xdxr 原始权益事件按交易所除权参考价公式推导, 并与新浪 hfq.js 因子"
    "逐条比对校验: 贵州茅台/平安银行/招商银行/伊利股份/比亚迪 8~17 条事件偏差 "
    "0.000000%; 存在股份回购注销的股票有额外偏差 —— 格力电器最差 0.467%、美的集团 "
    "0.099%(其余 ⪅0.05%), 根因是回购专户股份不参与利润分配, 交易所实际除权扣的不是"
    "名义每股分红, 而通达信只给名义值。做长周期回测请优先选无回购注销历史的标的, "
    "或改用交易所原始除权公告核对。"
)


class TdxXdxrUnavailableError(Exception):
    """除权除息数据不可用 —— 端点转成 ``available:false``, 不向上抛 5xx。"""


_cache: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()


def clear_cache() -> None:
    _cache.clear()


def _ttl() -> float:
    try:
        return float(_settings().tdx_xdxr_cache_ttl_s)
    except Exception:  # 配置缺失时退回默认值, 不该把整个端点拖垮
        return 1800.0


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


#: 板块前缀 —— 用**两位前缀**而不是首位判券商们常见的写法, 因为 "9" 既是沪 B(900xxx)
#: 又是北交所(920xxx), 只判首位会把北交所当成沪市去问 xdxr(实测过期路口令没法改, 必须拦住)。
_SH_PREFIXES = ("60", "68", "90")
_SZ_PREFIXES = ("00", "30", "20")
#: 北交所: easy_tdx 的 Market.BJ 存在, 但 xdxr 在这批节点上没实测过 —— 一律 fail-closed。
_BJ_PREFIXES = ("43", "83", "87", "88", "92")


def _market_for(code: str) -> Market:
    """6 位代码 → 通达信 market; 北交所与非 A股代码一律 fail-closed。"""
    if len(code) == 6 and code.isdigit():
        prefix = code[:2]
        if prefix in _SH_PREFIXES:
            return Market.SH
        if prefix in _SZ_PREFIXES:
            return Market.SZ
    raise TdxXdxrUnavailableError(
        f"bad_code: {code!r} 不是沪深 A股代码"
        + ("(北交所暂不支持: xdxr 未实测)" if code[:2] in _BJ_PREFIXES else ""))


def _exchange(code: str) -> str:
    """腾讯 K 线的交易所前缀 —— 与 ``_market_for`` 同口径。"""
    return "sh" if code[:2] in _SH_PREFIXES else "sz"


def _exec(cmd_factory: Callable[[], object]) -> T:
    """连一个节点执行命令; 失败逐个降级。

    标准市不需要像 MAC-EX 那样先发登录包 —— 但它对未知/超限命令同样会直接断连接,
    所以这里**每条命令一次连接**, 不在连接里复用第二个命令。
    """
    hosts = _hosts()
    if not hosts:
        raise TdxXdxrUnavailableError("no_host: 未配置通达信标准市节点")
    errors: list[str] = []
    for host in hosts:
        try:
            with TdxConnection(host, port=_port(), timeout=_TIMEOUT_S) as conn:
                return conn.execute(cmd_factory())
        except Exception as exc:
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
    raise TdxXdxrUnavailableError("connect_failed: " + " | ".join(errors))


def _cached(key: str) -> dict | None:
    with _LOCK:
        hit = _cache.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > _ttl():
        with _LOCK:
            _cache.pop(key, None)
        return None
    return value


def _put(key: str, value: dict) -> None:
    with _LOCK:
        _cache[key] = (time.time(), value)


def _event_payload(r) -> dict:
    """把 vendor 的 XdxrRecord 摊平成 JSON 友好的 dict, 单位全部写进字段名里。"""
    out = {
        "date": f"{r.year:04d}-{r.month:02d}-{r.day:02d}",
        "category": r.category,
        "name": r.name,
    }
    if r.category == 1:
        # vendor 已把协议里的"每 10 股"口径除成每股(见 commands/xdxr_info.py)
        out.update({
            "cash_per_share": round(r.fenhong or 0.0, 6),
            "bonus_share_ratio": round(r.songzhuangu or 0.0, 6),
            "rights_issue_ratio": round(r.peigu or 0.0, 6),
            "rights_issue_price": round(r.peigujia or 0.0, 6),
        })
    elif r.category in (11, 12):
        out["shrink_ratio"] = r.suogu
    elif r.category in (13, 14):
        out["warrant_strike_price"] = r.xingquanjia
        out["warrant_ratio"] = r.fenshu
    else:
        # 股本变动类: 单位是**万股**(vendor 用成交量那套自定义浮点解出来就是万股)
        out.update({
            "pre_float_shares_wan": r.panqian_liutong,
            "post_float_shares_wan": r.panhou_liutong,
            "pre_total_shares_wan": r.qian_zongguben,
            "post_total_shares_wan": r.hou_zongguben,
        })
    return out


def xdxr_events(code: str) -> dict:
    """某只票的**全部**权益事件(含股本变动类), 按日期升序。

    这是本模块最硬的数据: 日期与新浪复权因子的生效日逐条对齐, 金额与交易所公告同口径。
    """
    code = code.strip()
    market = _market_for(code)
    key = f"events|{market}|{code}"
    hit = _cached(key)
    if hit:
        return hit

    try:
        recs = _exec(lambda: GetXdxrInfoCmd(market, code))
    except TdxXdxrUnavailableError:
        raise
    except Exception as exc:
        raise TdxXdxrUnavailableError(f"upstream_error: {type(exc).__name__}: {exc}") from exc

    if not recs:
        raise TdxXdxrUnavailableError(f"empty_events: {code} 上游返回 0 条除权除息记录")

    events = sorted((_event_payload(r) for r in recs), key=lambda e: e["date"])
    counts: dict[int, int] = {}
    for e in events:
        counts[e["category"]] = counts.get(e["category"], 0) + 1

    body = {
        "code": code,
        "protocol": "GetXdxrInfoCmd(0x0f00)",
        "events": events,
        "count": len(events),
        "category_counts": counts,
        "cash_events": sum(1 for e in events if e["category"] == 1),
        "coverage": {"from": events[0]["date"], "to": events[-1]["date"]},
        "unit_note": (
            "category=1(除权除息): cash_per_share/bonus_share_ratio 等均为**每股**口径"
            "(协议原值是每 10 股, 已除 10); 其余类别的 *_shares_wan 单位是**万股**"
        ),
        # 前端要能把这句话摆给用户看 —— 用这套事件算复权的人得知道偏差在哪
        "adjust_note": _DEVIATION_NOTE,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result = {"source": SOURCE, "data": body}
    _put(key, result)          # ⚠️ 存包装后的结构: 缓存命中时直接返回它, 形状必须一致
    return result


def _daily_bars(code: str, count: int) -> list[tuple[str, float]]:
    """不复权日K ``[(YYYY-MM-DD, close)]`` 升序 —— 算因子要"除权日前一交易日收盘价"。

    为什么日K走腾讯而不是通达信:
        - 能回 xdxr 的那批 7709 属于**信息服务器**(财务/除权/公司资料这类), 实测对
          ``GetSecurityBarsCmd`` 一律回一个 2 字节空包, vendor 解析直接报错;
          真正的 K 线行情服务器是另一批节点, 不想为了一个前收盘价再探一轮风寒。
        - 腾讯 ``kline/kline`` 的 ``day`` 就是不复权 close, 项目里的 enriched 日K本来
          就是它同源的数据, 用它当"除权日前收盘"不会引入第二套口径。
        - 副作用是好的: 最终系数要和腾讯自己的 adjratio 逐日比, 两边用同一份日K,
          **唯一的自由度就只剩下权益事件本身** —— 比出来的差就是事件口径的差。

    ⚠️ 腾讯这两个参数名别写错: 复权要 ``qfq``/``hfq``, 写成 ``qfqa``/``hfqa`` 它不认,
       会静默返回不复权的 day(踩过, 详见 deliverables/xdxr-probe 里的记录)。
    """
    key = f"bars|{code}|{count}"
    hit = _cached(key)
    if hit:
        return [(row["date"], row["close"]) for row in hit["rows"]]  # type: ignore[union-attr]

    # 与 _market_for 同口径判交易所 —— 别图省事只判首位('9' 既沪B 又北交所)。
    sym = _exchange(code) + code
    try:
        resp = httpx.get(_TX_KLINE_URL, params={"param": f"{sym},day,,,{count},"},
                         timeout=_TX_TIMEOUT_S)
        resp.raise_for_status()
        node = (resp.json().get("data") or {}).get(sym) or {}
        raw_rows = node.get("day") or []
    except Exception as exc:
        raise TdxXdxrUnavailableError(f"bars_error: {type(exc).__name__}: {exc}") from exc

    rows = []
    for r in raw_rows:
        if not isinstance(r, (list, tuple)) or len(r) < 3:
            continue
        d = str(r[0]).strip()[:10]
        try:
            c = float(r[2])          # 第 3 列 = 收盘(腾讯口径, 与新/旧版 parser 一致)
        except (TypeError, ValueError):
            continue
        if c > 0 and len(d) == 10:
            rows.append({"date": d, "close": c})
    rows.sort(key=lambda x: x["date"])
    if not rows:
        raise TdxXdxrUnavailableError(f"empty_bars: {code} 腾讯未返回可解析的日K")
    _put(key, {"rows": rows})
    return [(r["date"], r["close"]) for r in rows]


def adj_ratios(code: str, mode: str = "qfq", limit: int = 640) -> dict:
    """每日复权系数(``ratio = 复权价 / 不复权价``)。

    与腾讯版 ``adjust_ratios`` 的区别: 腾讯给的是它自己复完的结果(黑盒), 这里是
    **从原始权益事件推出来的**, 每一档都能追到是哪一次分红/送转导致的, 并且把
    已知偏差写在 ``deviation_note`` 里。

    ⚠️ 为什么不用比值法(复权价/不复权价): 那样等于把腾讯的口径原样搬过来, 我们只是
       多一次网络往返。这里要的是**可解释**: events 里有每一次(除权日, 分红, 送转)。
    """
    code = code.strip()
    market = _market_for(code)
    if mode not in ("qfq", "hfq"):
        raise TdxXdxrUnavailableError(f"bad_mode: {mode!r} (qfq/hfq)")
    limit = max(MIN_FACTOR_DAYS, min(int(limit or MAX_FACTOR_DAYS), MAX_FACTOR_DAYS))

    key = f"ratios|{market}|{code}|{mode}|{limit}"
    hit = _cached(key)
    if hit:
        return hit

    events = [e for e in xdxr_events(code)["data"]["events"] if e["category"] == 1]
    # 多取几根: 最早那次除权要"前一交易日", 边界上不能刚好取没。
    bars = _daily_bars(code, min(limit + 10, MAX_DAILY_BARS_PER_REQUEST))
    dates = [d for d, _ in bars]
    closes = dict(bars)

    factor = [1.0] * len(dates)
    used_events: list[dict] = []
    skipped_missing_prev: list[str] = []

    index = {d: i for i, d in enumerate(dates)}
    for ev in events:
        idx = index.get(ev["date"])
        if idx is None:
            continue                      # 该除权日不在日K覆盖区间内(太老), 不影响这段序列
        if idx == 0:
            # 第一段之外: 没有前收盘可用 —— 这一段因子算不出来, 不能猜。
            skipped_missing_prev.append(ev["date"])
            continue
        prev_close = closes[dates[idx - 1]]
        num = prev_close - ev["cash_per_share"] + ev["rights_issue_price"] * ev["rights_issue_ratio"]
        den = (1.0 + ev["bonus_share_ratio"] + ev["rights_issue_ratio"]) * prev_close
        if den <= 0 or num <= 0:
            raise TdxXdxrUnavailableError(
                f"bad_event: {code} {ev['date']} 除权参数异常(prev_close={prev_close})")
        r = num / den
        for k in range(idx):
            factor[k] *= r
        used_events.append({
            "date": ev["date"],
            "cash_per_share": ev["cash_per_share"],
            "bonus_share_ratio": ev["bonus_share_ratio"],
            "rights_issue_ratio": ev["rights_issue_ratio"],
            "rights_issue_price": ev["rights_issue_price"],
            "prev_close": round(prev_close, 4),
            "r": round(r, 8),
        })

    if len(factor) > limit:
        factor = factor[-limit:]
        dates_tail = dates[-limit:]
    else:
        dates_tail = dates

    if mode == "qfq":
        ratios = [round(f / factor[-1], 8) for f in factor]
    else:
        base = factor[0]
        ratios = [round(f / base, 8) for f in factor]

    rows = [
        {"date": d, "ratio": ra, "close": round(closes[d], 4)}
        for d, ra in zip(dates_tail, ratios, strict=True)
    ]
    body = {
        "code": code,
        "mode": mode,
        "ratios": rows,
        "events_used": used_events,
        "has_adjust": any(abs(ra - 1.0) > 1e-6 for ra in ratios),
        "formula": (
            "r = (前收盘 - 每股现金分红 + 配股价×配股比例) / "
            "((1 + 送转比例 + 配股比例) × 前收盘); "
            "qfq 因子 = 该日之后所有 r 的累乘; hfq 因子 = qfq 因子 / 最早的 qfq 因子"
        ),
        "deviation_note": _DEVIATION_NOTE,
        "coverage": {
            "from": dates_tail[0],
            "to": dates_tail[-1],
            "bars": len(dates_tail),
            "events_in_range": len(used_events),
        },
        "skipped_events_without_prev_close": skipped_missing_prev,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result = {"source": SOURCE, "data": body}
    _put(key, result)          # ⚠️ 存包装后的结构: 缓存命中时直接返回它, 形状必须一致
    return result
