"""中国宏观 8 指标 护栏测试 —— 防假数据的硬闸门。

守护四条:
1. **源码里不得出现 random() / _DEMO_ / 占位兜底** —— 九章 ``skills_ext.py``
   所在终端有 15 条路由在数据源不可达时用 ``random()`` 造数。provider 是重写版,
   grep 断言把"不得带进来"钉死在 CI 上(注释里提到这个词不算, 断言的是代码记号)。
2. **akshare 未安装 → available:false + 安装提示**, 不抛异常炸掉调用方。
3. **上游抛异常 → 只有那一项 unavailable**, 响应里不含任何伪造数值
   (value 要么来自上游真值, 要么是 None, 没有第三种)。
4. **_macro_last_row 对两种排序方向的表都取对最新行** —— 用假 DataFrame 测,
   这是算法本身(不是产品里的假数据)。

全部用例 mock 掉 akshare, 不依赖外网。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import macro as api
from app.config import settings
from app.data_providers import cn_macro_provider as prov
from app.main import app

#: 假上游的"真值"。断言里用它当白名单: 响应里出现的任何数都必须在这里。
_LIVE = {
    "cpi": 0.5, "ppi": -1.2, "pmi": 50.1, "gdp": 5.0,
    "lpr": 3.1, "m2": 8.4, "shrzgm": 35000.0, "bond10": 1.8,
}

# 排序方向: M2/社融是**首行最新**, 其余是**尾行最新**(九章 2026-07-29 实测)。
_NEWEST_FIRST = ("m2", "shrzgm")

_PROVIDER_SRC = Path(prov.__file__).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_cache():
    """provider 用模块级 _CACHE, 跨用例残留会让断言"假通过"。"""
    prov.clear_cache()
    yield
    prov.clear_cache()


@pytest.fixture(scope="session")
def client():
    # ⚠️ client=("127.0.0.1", 5555) 不能省: 后端鉴权只放行可信来源, 用默认
    # TestClient 来源打 API 会拿到 **403**(不是 401), 与被测逻辑无关。
    with TestClient(app, client=("127.0.0.1", 5555)) as c:
        yield c


def _install_fake_ak(monkeypatch, *, fail: tuple[str, ...] = (),
                     gate: threading.Event | None = None) -> None:
    """把 8 个 akshare 接口换成假 DataFrame(测试夹具)。

    ``fail`` 里的 key 让对应接口抛异常; ``gate`` 非 None 时接口先阻塞,
    用来确定性地验证"首请求不阻塞、后台预热"。
    """
    import akshare as ak
    import pandas as pd

    def _make(key: str, cols: tuple[str, ...], dcol: str | None):
        if key in fail:
            def boom() -> None:
                raise RuntimeError(f"上游 {key} 抛异常(测试注入)")
            return boom

        dcol_name = dcol or "月份"
        older = ("2026-08-01", _LIVE[key] - 0.1)
        newer = ("2026-09-01", _LIVE[key])
        rows = [newer, older] if key in _NEWEST_FIRST else [older, newer]
        df = pd.DataFrame(rows, columns=[dcol_name, cols[0]])

        def ok(_df=df):
            if gate is not None:
                gate.wait(timeout=10)
            return _df
        return ok

    for key, func_name, cols, dcol in prov._FETCHERS:
        monkeypatch.setattr(ak, func_name, _make(key, cols, dcol))


def _warm_cache(monkeypatch, **kw) -> None:
    """同步预热一次并写缓存 —— 让后续 API 请求命中缓存, 避免"预热中"竞态。"""
    _install_fake_ak(monkeypatch, **kw)
    prov._cache_put(prov._CACHE_KEY, prov._fetch_all(), prov._TTL_S)


# ── 1. 源码里不得出现伪造数据的记号 ────────────────────────────────────
def test_provider_source_has_no_fake_data_tokens():
    """grep 断言: provider 源码不含 random() / _DEMO_ / 占位兜底。"""
    for token in ("random.", "_DEMO_", "randint(", "uniform(", "fake_", "mock_"):
        assert token not in _PROVIDER_SRC, (
            f"provider 源码出现伪造数据记号 {token!r} —— 这正是要消灭的东西")

    # 代理必须按源隔离: 不许出现"设全局代理"的写法(本模块连 os 都没导入)。
    assert "os.environ" not in _PROVIDER_SRC


# ── 2. akshare 未安装 → fail-closed + 安装提示 ─────────────────────────
def test_akshare_missing_declares_unavailable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "akshare":
            raise ImportError("No module named 'akshare'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    out = prov._fetch_all()
    assert out["available"] is False
    assert "akshare" in out["reason"] and "multi-market" in out["reason"]
    assert out["live_count"] == 0 and out["total"] == 8
    # 8 项都要在, 但**一个数字都不能有**
    assert all(i["value"] is None and i["status"] == "unavailable" for i in out["items"])


def _break_akshare_import(monkeypatch):
    """让 ``import akshare`` 抛 ImportError(模拟容器没装 multi-market extra)。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "akshare":
            raise ImportError("No module named 'akshare'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_api_reports_missing_akshare_with_http_200(monkeypatch, client):
    """HTTP 200 + available:false, 不抛异常、不返回空壳装成功。"""
    monkeypatch.setattr(settings, "cn_macro_enabled", True)
    _break_akshare_import(monkeypatch)
    # 同步预热一次: 否则 API 首请求会命中"预热中", 拿不到缺依赖的原因码
    prov._cache_put(prov._CACHE_KEY, prov._fetch_all(), prov._TTL_S)

    resp = client.get("/api/cn/macro")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "multi-market" in body["reason"]
    assert body["items"] == [] or all(i["value"] is None for i in body["items"])


# ── 3. 上游抛异常 → 只有那一项 unavailable ─────────────────────────────
def test_upstream_error_marks_only_that_indicator(monkeypatch, client):
    monkeypatch.setattr(settings, "cn_macro_enabled", True)
    _warm_cache(monkeypatch, fail=("m2",))

    resp = client.get("/api/cn/macro")
    assert resp.status_code == 200
    body = resp.json()

    assert body["available"] is True          # 7/8 有数 → 整体仍可用
    assert body["live_count"] == 7 and body["total"] == 8
    assert body["source"] == "live-akshare"

    by_key = {i["key"]: i for i in body["items"]}
    assert by_key["m2"]["value"] is None
    assert by_key["m2"]["date"] is None
    assert by_key["m2"]["status"] == "unavailable"
    assert by_key["cpi"]["status"] == "ok" and by_key["cpi"]["value"] == _LIVE["cpi"]

    # 反伪造: 响应里出现的每个数都必须是上游给的真值, 不允许 0/上一期/随机数占位
    assert {i["value"] for i in body["items"] if i["value"] is not None} == set(
        _LIVE[k] for k in _LIVE if k != "m2")


def test_all_indicators_failed_is_overall_unavailable(monkeypatch):
    """8 项全挂才整体 available:false(不是"有一项挂就装死")。"""
    _install_fake_ak(monkeypatch, fail=tuple(_LIVE))
    out = prov._fetch_all()
    assert out["available"] is False
    assert out["live_count"] == 0 and out["total"] == 8
    assert all(i["value"] is None for i in out["items"])


# ── 4. _macro_last_row: 两种排序方向都得取到最新行 ──────────────────────
def _frame(rows, dcol="月份", vcol="今值"):
    import pandas as pd
    return pd.DataFrame(rows, columns=[dcol, vcol])


def test_last_row_tail_is_newest_and_skips_nan():
    """CPI/PPI 风格: 最新在尾行, 且未发布月份是 NaN —— 不能取到 NaN 或旧值。"""
    df = _frame([("2026年7月份", 0.3), ("2026年8月份", 0.5), ("2026年9月份", None)])
    value, date = prov._macro_last_row(df, ("今值",))
    assert value == 0.5
    assert date.startswith("2026-08")


def test_last_row_head_is_newest():
    """M2/社融风格: 最新在**首行** —— 直接 iloc[-1] 会拿到 2026-07 的旧值。"""
    df = _frame([("2026-09-01", 8.4), ("2026-08-01", 8.2), ("2026-07-01", 8.0)])
    value, date = prov._macro_last_row(df, ("今值",))
    assert value == 8.4
    assert date == "2026-09-01"


def test_last_row_all_nan_returns_none():
    """整列都没发布 → (None, None), 绝不退回 0 或上一期。"""
    df = _frame([("2026-08-01", None), ("2026-09-01", None)])
    assert prov._macro_last_row(df, ("今值",)) == (None, None)


def test_last_row_picks_first_available_value_column():
    """列名改版时按候选列逐个试(akshare 换列名不该变成"取不到")。"""
    df = _frame([("2026-09-01", 3.1)], dcol="TRADE_DATE", vcol="LPR1Y")
    value, date = prov._macro_last_row(df, ("今值", "LPR1Y"), "TRADE_DATE")
    assert value == 3.1 and date == "2026-09-01"


# ── 5. 缓存 / 预热: 首请求不阻塞 ───────────────────────────────────────
def test_first_call_returns_warming_without_blocking(monkeypatch):
    """缓存为空时立刻回 warming(8 个串行接口冷启动 10~25s, 不能让请求干等)。"""
    gate = threading.Event()
    _install_fake_ak(monkeypatch, gate=gate)

    first = prov.macro_brief()
    assert first["available"] is False
    assert first["reason"] == "warming"
    assert all(i["value"] is None for i in first["items"])   # 预热期不填任何数

    gate.set()
    for _ in range(200):
        if prov._cache_get(prov._CACHE_KEY) is not None:
            break
        time.sleep(0.05)

    second = prov.macro_brief()
    assert second["available"] is True and second["live_count"] == 8
    # 方向处理在端到端也成立: 首行最新的 m2/社融取的是新值
    by_key = {i["key"]: i for i in second["items"]}
    assert by_key["m2"]["value"] == _LIVE["m2"]
    assert by_key["shrzgm"]["value"] == _LIVE["shrzgm"]


def test_warmup_is_synchronous_and_raises_when_all_fail(monkeypatch):
    """warmup() 同步拉一次: 成功写缓存, 全失败上抛(范式同 astock warmup)。"""
    _install_fake_ak(monkeypatch)
    payload = prov.warmup()
    assert payload["available"] is True and payload["live_count"] == 8
    assert prov.macro_brief()["available"] is True      # 缓存已热

    prov.clear_cache()
    _install_fake_ak(monkeypatch, fail=tuple(_LIVE))
    with pytest.raises(RuntimeError):
        prov.warmup()


# ── 6. 开关默认关闭 ────────────────────────────────────────────────────
def test_api_short_circuits_when_disabled(monkeypatch, client):
    """cn_macro_enabled=false(默认)时不碰 provider, 直接声明不可用。"""
    monkeypatch.setattr(settings, "cn_macro_enabled", False)
    body = client.get("/api/cn/macro").json()
    assert body["available"] is False
    assert "CN_MACRO_ENABLED" in body["reason"]


def test_router_prefix_is_api_cn():
    assert api.router.prefix == "/api/cn"
    assert any(r.path == "/api/cn/macro" for r in api.router.routes)
