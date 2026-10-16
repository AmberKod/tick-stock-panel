r"""中国宏观 8 指标 (akshare) —— 九章 ``macro_brief`` 的 TSP 重写版。

来源说明:
    指标选取 / 单位 / 解读文案, 以及"取最新行"的算法, 移植自九章量化终端
    (jiuzhang-algor) ``D:/jiuzhang/skills_ext.py`` 的 ``macro_brief`` (137-178)
    与 ``_macro_last_row`` (99-134) (MIT License, Copyright (c) 2026 小鱼总的小圈子)。
    **实现为 TSP 自研**, 与参考实现有四处刻意不同:

    1. **fail-closed, 零 random()**: 九章那份文件里有 15 条路由在数据源不可达时
       用 ``random()`` 造数。本模块**一个随机数都不产生** —— 取不到就
       ``status: "unavailable"`` + ``value: None``, 任一项都不填占位值
       (由 tests/test_cn_macro.py 的 grep 断言锁死)。
    2. **代理按源隔离**: akshare 内部走 ``requests`` 且从不传 ``proxies``,
       不隔离就会继承进程级 ``HTTP_PROXY``(项目铁律禁止)。这里在取数窗口内把
       akshare 子模块里的 ``requests`` 换成**显式带 proxies 的替身**
       (见 :class:`_AkProxyRequests`), 只认 ``settings.cn_macro_proxy``;
       未配置即强制直连, 等价于 urllib 的 ``ProxyHandler({})`` 显式清空。
    3. **缓存 + 后台预热**: 8 个接口串行冷启动实测 10~25s, 让首个 HTTP 请求
       干等等于拖垮首屏。缓存为空时立即回 ``available:false, reason:"warming"``
       并起**单飞**的后台线程预热; 预热完的下一个请求才是真数据。
    4. **akshare lazy import**: 装在 extra ``multi-market`` 里, 容器内是否装取决于
       宿主 ``.env`` 的 ``BACKEND_EXTRAS``。没装就声明 ``available:false`` 并给出
       安装提示, **不抛异常**炸掉调用方。

真实性:
    ``source = "live-akshare"`` —— 已登记在 ``services/source_gate.py`` 白名单内,
    过闸门判定为真实数据。**不要新增** ``akshare-live`` 之类未登记标记。

用法:
    from app.data_providers.cn_macro_provider import macro_brief
    macro_brief()   # {"available": bool, "source": "live-akshare", "items": [...]}
"""
from __future__ import annotations

import contextlib
import datetime
import logging
import re
import sys
import threading
import time

logger = logging.getLogger(__name__)

#: 已登记在 services/source_gate.py:30 白名单内的真实标记。
SOURCE = "live-akshare"

#: 宏观指标一个月才更新几次, 缓存 1 小时足够(与九章 macro_brief 同 TTL)。
_TTL_S = 3600.0
_CACHE_KEY = "cn-macro-brief"

# 结果缓存: key -> (expire_at, payload)。范式同 jiuzhang_astock_provider.py:159。
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()

#: 预热单飞锁: 只有拿到锁的线程去拉, 其余请求直接拿 warming 回包 —— 绝不排队干等。
_WARM_LOCK = threading.Lock()

#: akshare 缺依赖时的统一提示(不给占位值, 只说清楚怎么装)。
_AK_MISSING_REASON = "akshare 未安装(BACKEND_EXTRAS=multi-market)"

#: 缓存还没热时的回包原因码。
_WARMING_REASON = "warming"

# 指标解读库 (浓缩自九章 macro-data-monitor/references/indicators.md)。
#: unit 一列必须逐指标声明: 社融是**亿元**, PMI 是**无量纲指数**(空串),
#: 其余是百分数 —— 统一按"%"处理会让社融数字看着像通胀率。
_MACRO_DOC: dict[str, dict[str, str]] = {
    "cpi": {"name": "CPI 同比", "unit": "%",
            "explain": "追踪日常消费品与服务价格变动，反映居民购买力。",
            "read": "约2%健康；超3%通胀压力明显；低于1%消费不振、经济偏冷。"},
    "ppi": {"name": "PPI 同比", "unit": "%",
            "explain": "工业品出厂价格变动，是 CPI 的前瞻信号，反映企业成本端。",
            "read": "走高→成本上升可能推升CPI；持续为负→工业通缩、企业利润受挤压。"},
    "pmi": {"name": "制造业 PMI", "unit": "",
            "explain": "采购经理问卷编制的景气指数，经济运行晴雨表。",
            "read": "50为荣枯线：高于50扩张、订单增长；持续低于50经济面临减速压力。"},
    "gdp": {"name": "GDP 同比", "unit": "%",
            "explain": "一段时期内全部最终产品与服务的市场价值总和，经济体量核心标尺。",
            "read": "中国5-6%为健康水平；发达经济体2-3%正常；负增长为衰退。"},
    "lpr": {"name": "LPR 报价利率", "unit": "%",
            "explain": "银行对最优客户的贷款利率锚，直接左右房贷与企业融资成本。",
            "read": "下调→贷款利息减少、利好投资消费与股市；上调→抑制过热。"},
    "m2": {"name": "M2 同比", "unit": "%",
           "explain": "广义货币供应量增速，反映市场流动性总闸门。",
           "read": "增速回升→流动性宽松，对风险资产偏正面；回落→资金面收紧。"},
    "shrzgm": {"name": "社会融资规模", "unit": "亿元",
               "explain": "实体经济从金融体系获得的资金总量，信用扩张的直接度量。",
               "read": "放量→实体融资需求旺盛、经济预期改善；缩量→需求疲弱。"},
    "bond10": {"name": "中国10年期国债收益率", "unit": "%",
               "explain": "全球资产定价基准锚，反映市场对长期增长与通胀的集体预期。",
               "read": "上行→资金成本升高、对股债偏负面；下行→对风险资产偏正面。"},
}

#: (key, akshare 函数名, 候选值列名, 日期列名)。候选列按优先级逐个试 ——
#: akshare 改版会换列名, 写死单列会直接变成"取不到"。
_FETCHERS: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    ("cpi", "macro_china_cpi_monthly", ("今值", "现值", "cpi"), None),
    ("ppi", "macro_china_ppi_yearly", ("今值", "现值", "ppi"), None),
    ("pmi", "macro_china_pmi_yearly", ("今值", "现值", "pmi"), None),
    ("gdp", "macro_china_gdp_yearly", ("今值", "现值", "gdp"), None),
    ("lpr", "macro_china_lpr", ("LPR1Y", "1年期LPR", "lpr_1y"), "TRADE_DATE"),
    ("m2", "macro_china_money_supply",
     ("货币和准货币(M2)-同比增长", "M2-同比增长", "货币和准货币-同比增长"), None),
    ("shrzgm", "macro_china_shrzgm", ("社会融资规模增量", "社融增量"), None),
    ("bond10", "bond_zh_us_rate", ("中国国债收益率10年",), None),
)

#: akshare 里定义了这 8 个函数的子模块 —— 代理替身只替换这两个模块内的
#: ``requests``, 范围收窄到本源用到的代码(不是全进程)。
_AK_SUBMODULES = ("akshare.economic.macro_china", "akshare.bond.bond_em")


def _setting(name: str, default: object = None) -> object:
    """读 settings 的一项; settings 不可用(纯函数/单测场景)时退化成默认值。"""
    try:
        from app.config import settings

        return getattr(settings, name, default)
    except Exception:  # pylint: disable=broad-except
        return default


def _proxy() -> str:
    """本源专属代理。只认 settings.cn_macro_proxy, 不读任何全局代理环境变量。"""
    return str(_setting("cn_macro_proxy", "") or "").strip()


def _cache_get(key: str) -> object | None:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    return None


def _cache_put(key: str, value: object, ttl: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + ttl, value)


def clear_cache() -> None:
    """清空本模块缓存 —— 单测必须每例清一次, 否则跨用例命中旧缓存会"假通过"。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def _parse_dt(value: object) -> datetime.date | None:
    """宽容解析日期串: 支持 '2026-06-10' / '2026年06月份' / '20260610' 等。

    移植自九章 skills_ext.py:74-96 —— 宏观表的日期列格式在各接口间完全不统一,
    不解析就没法判断"哪一行最新"。
    """
    if value is None:
        return None
    nums = re.findall(r"\d+", str(value))
    if not nums:
        return None
    try:
        raw = nums[0]
        if len(raw) == 8 and len(nums) == 1:        # 20260610
            y, m, d = int(raw[:4]), int(raw[4:6]), int(raw[6:8])
        elif len(raw) == 6 and len(nums) == 1:      # 202604 (YYYYMM, 社融月份格式)
            y, m, d = int(raw[:4]), int(raw[4:6]), 1
        else:
            y = int(raw)
            m = int(nums[1]) if len(nums) > 1 else 1
            d = int(nums[2]) if len(nums) > 2 else 1
        if y < 1900 or y > 2100:
            return None
        return datetime.date(y, min(max(m, 1), 12), min(max(d, 1), 28))
    except Exception:  # pylint: disable=broad-except
        return None


def _macro_last_row(df: object, val_cols: tuple[str, ...],
                    date_col: str | None = None) -> tuple[float | None, str | None]:
    """取"最新的非空"数据行的值 —— 九章唯一真正的算法价值, 原样移植。

    关键坑(九章 2026-07-29 实测): akshare 宏观表**排序方向不一致** ——
    M2/社融最新在**首行**, CPI/PPI 最新在**尾行且未发布月份为 NaN**。
    直接 ``iloc[-1]`` 会拿到 2008 年的旧值或 NaN。所以必须:
    ① 先过滤掉值为 NaN 的行; ② 再按解析出的日期取最大者。
    """
    if df is None or len(df) == 0:  # type: ignore[arg-type]
        return None, None
    cols = list(df.columns)  # type: ignore[attr-defined]
    dcol = date_col if (date_col and date_col in cols) else None
    if not dcol:
        for c in ("日期", "月份", "统计时间", "季度", "date", "TRADE_DATE", "时间"):
            if c in cols:
                dcol = c
                break
    for c in val_cols:
        if c not in cols:
            continue
        sub = df[df[c].notna()]  # type: ignore[index]
        if len(sub) == 0:
            continue
        if dcol:
            best_row, best_dt = None, None
            for _, r in sub.iterrows():
                dt = _parse_dt(r.get(dcol))
                if dt is not None and (best_dt is None or dt > best_dt):
                    best_dt, best_row = dt, r
            if best_row is None:            # 日期都解析不了 → 退回尾行
                best_row, best_dt = sub.iloc[-1], None
            try:
                return round(float(best_row[c]), 3), (
                    best_dt.isoformat() if best_dt else str(best_row.get(dcol)))
            except Exception:  # pylint: disable=broad-except
                continue
        try:
            return round(float(sub.iloc[-1][c]), 3), None
        except Exception:  # pylint: disable=broad-except
            continue
    return None, None


class _AkProxyRequests:
    """``requests`` 的替身: 给 akshare 的每个请求**显式**注入本源代理。

    为什么需要它: akshare 的宏观接口一律 ``requests.get(...)`` / ``requests.Session()``
    且从不传 ``proxies``, 于是只能继承进程级 ``HTTP_PROXY`` —— 而"设全局代理"
    是本项目铁律禁止的(会给 SEC / 东财等其它源带来无法排查的串扰)。替身在
    ``get`` / ``post`` / ``Session.request`` 三个入口塞入:

      - 配了 ``settings.cn_macro_proxy`` → ``{"http": p, "https": p}``;
      - 未配置 → ``{"http": "", "https": ""}`` —— requests 见到空串即直连,
        语义等价于 ``urllib.request.ProxyHandler({})`` 的显式清空。

    替换范围只限 :data:`_AK_SUBMODULES` 这两个 akshare 子模块, 用完立刻还原。
    """

    def __init__(self, inner: object, proxies: dict[str, str]) -> None:
        self._inner = inner
        self._proxies = proxies

    def __getattr__(self, name: str) -> object:
        # 其余名称(exceptions / utils / Request ...)一律透传给真 requests
        return getattr(self._inner, name)

    def get(self, url: str, **kw: object) -> object:
        kw.setdefault("proxies", self._proxies)
        return self._inner.get(url, **kw)  # type: ignore[attr-defined]

    def post(self, url: str, **kw: object) -> object:
        kw.setdefault("proxies", self._proxies)
        return self._inner.post(url, **kw)  # type: ignore[attr-defined]

    def Session(self, *a: object, **kw: object) -> object:  # noqa: N802 (照抄 requests 的大小写)
        session = self._inner.Session(*a, **kw)  # type: ignore[attr-defined]
        original = session.request

        def _request(method: str, url: str, *a2: object, **kw2: object) -> object:
            kw2.setdefault("proxies", self._proxies)
            return original(method, url, *a2, **kw2)

        session.request = _request
        return session


@contextlib.contextmanager
def _ak_proxy_scope():
    """取数窗口: 把 akshare 子模块里的 requests 换成带本源代理的替身, 退出即还原。"""
    import requests

    proxy = _proxy()
    proxies = ({"http": proxy, "https": proxy} if proxy
               else {"http": "", "https": ""})
    shim = _AkProxyRequests(requests, proxies)
    patched: list[tuple[object, object]] = []
    for name in _AK_SUBMODULES:
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "requests"):
            patched.append((mod, mod.requests))
    try:
        for mod, _old in patched:
            mod.requests = shim  # type: ignore[attr-defined]
        yield
    finally:
        for mod, old in patched:
            mod.requests = old  # type: ignore[attr-defined]


def _item(key: str, value: float | None, date: str | None) -> dict:
    """单指标结果。value 为 None 时 status 是 unavailable, **绝不填 0 / 占位值**。"""
    doc = _MACRO_DOC[key]
    return {
        "key": key,
        "name": doc["name"],
        "value": value,
        "unit": doc["unit"],
        "date": date,
        "explain": doc["explain"],
        "read": doc["read"],
        "status": "ok" if value is not None else "unavailable",
    }


def _unavailable_items() -> list[dict]:
    """8 个指标全 unavailable(缓存未热 / akshare 未装时用它保持返回结构稳定)。"""
    return [_item(key, None, None) for key in _MACRO_DOC]


def _payload(items: list[dict], reason: str | None = None) -> dict:
    """组装响应。``reason`` 非空即 ``available:false``(fail-closed)。"""
    live = sum(1 for i in items if i.get("status") == "ok")
    if reason is None and live == 0:
        # 8 个接口全挂才算整体取不到; 部分挂只把那几项标 unavailable。
        reason = "all_indicators_unavailable"
    out = {
        "available": reason is None,
        "source": SOURCE,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "live_count": live,
        "total": len(items),
        "items": items,
    }
    if reason is not None:
        out["reason"] = reason
    return out


def _fetch_all() -> dict:
    """串行拉 8 个 akshare 接口。每个指标独立 try/except, 互不拖累。"""
    try:
        import akshare as ak
    except ImportError:
        logger.warning("cn macro unavailable: akshare 未安装(BACKEND_EXTRAS=multi-market)")
        return _payload(_unavailable_items(), reason=_AK_MISSING_REASON)

    items: list[dict] = []
    with _ak_proxy_scope():
        for key, func_name, val_cols, date_col in _FETCHERS:
            try:
                df = getattr(ak, func_name)()
                value, date = _macro_last_row(df, val_cols, date_col)
            except Exception as exc:  # pylint: disable=broad-except
                # 单个指标失败只让它自己 unavailable, 不编数、不影响其余指标
                logger.warning("cn macro %s unavailable: %s", key, exc)
                value, date = None, None
            items.append(_item(key, value, date))
    return _payload(items)


def _start_warmup_async() -> bool:
    """起后台预热线程(单飞)。返回本次是否真的起了线程。"""
    if not _WARM_LOCK.acquire(blocking=False):
        return False
    if isinstance(_cache_get(_CACHE_KEY), dict):
        _WARM_LOCK.release()
        return False

    def _run() -> None:
        try:
            _cache_put(_CACHE_KEY, _fetch_all(), _TTL_S)
        except Exception:  # pylint: disable=broad-except
            # _fetch_all 已逐指标吞异常, 走到这里是结构性问题: 记日志且不写缓存,
            # 让下一个请求重新预热 —— 也绝不让后台线程带着异常悄悄死掉。
            logger.exception("cn macro warmup failed")
        finally:
            _WARM_LOCK.release()

    threading.Thread(target=_run, name="cn-macro-warmup", daemon=True).start()
    return True


def macro_brief() -> dict:
    """宏观 8 指标。缓存命中即返回; **缓存为空不阻塞**, 回 warming 并后台预热。

    Returns:
        ``{"available": bool, "source": "live-akshare", "items": [...], ...}``。
        ``items`` 恒为 8 项, 每项取不到时 ``value: None, status: "unavailable"``
        —— 不填 0、不填上一期、更不 random()。
    """
    cached = _cache_get(_CACHE_KEY)
    if isinstance(cached, dict):
        return cached
    _start_warmup_async()
    cached = _cache_get(_CACHE_KEY)
    if isinstance(cached, dict):
        return cached
    return _payload(_unavailable_items(), reason=_WARMING_REASON)


def warmup() -> dict:
    """同步预热 / 自测钩子: 拉一次并写缓存, 全失败则上抛(范式同 astock warmup)。"""
    payload = _fetch_all()
    _cache_put(_CACHE_KEY, payload, _TTL_S)
    if not payload.get("available"):
        raise RuntimeError(f"cn macro warmup failed: {payload.get('reason')}")
    return payload
