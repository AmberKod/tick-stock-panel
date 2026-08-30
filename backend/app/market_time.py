"""[M0 已迁移] A 股市场时间工具 — 单一事实源在 app/markets/cn.py。

本模块降级为兼容 facade: 公共函数签名不变, 逐行转发 CN_PROFILE,
既有 import (13 处) 无需改动。M3 全量收敛到 app.markets 后可移除。

原始设计说明 (仍然有效):
服务器/容器本地时区不可靠 (python:slim 镜像默认 UTC), 交易时段判断、
实时行情落盘日期等必须显式使用北京时间, 否则 Docker 部署时轮询窗口
与真实交易时段完全错开 (北京 9:15-15:05 = UTC 1:15-7:05)。
"""
from __future__ import annotations

from datetime import date, datetime

from app.markets.cn import CN_PROFILE

CN_TZ = CN_PROFILE.tz


def cn_now() -> datetime:
    """当前北京时间 (带时区)。"""
    return CN_PROFILE.now()


def cn_today() -> date:
    """当前北京日期。"""
    return CN_PROFILE.today()


def trading_minutes_elapsed_from_dt(dt: datetime) -> float:
    """根据北京时间 datetime 计算当日已交易分钟数。 (转发 CN_PROFILE)

    交易时段: 9:30-11:30 (0~120) + 13:00-15:00 (120~240)。
    - 开盘前 = 0; 午休(11:30-13:00) = 120(保持上午累计); 收盘后 = 240。
    - 非交易日(周末) = 240 (视作全天, 避免量比被折算成 0)。
    """
    return CN_PROFILE.trading_minutes_elapsed_from_dt(dt)


def trading_minutes_elapsed() -> float:
    """当前已交易分钟数 (基于服务端北京时间)。

    量比折算的兜底: 当行情 timestamp 缺失时用服务端时间。
    优先使用 trading_minutes_elapsed_from_ts (行情真实时间, 更准)。
    """
    return CN_PROFILE.trading_minutes_elapsed_from_dt(cn_now())


def trading_minutes_elapsed_from_ts(ts_ms: int | float | None) -> float:
    """从行情时间戳(毫秒)计算当日已交易分钟数。 (转发 CN_PROFILE)

    优先使用此函数: 行情 timestamp 是真实成交时间, 比服务端时间更准
    (服务端时间含网络/限流延迟)。

    Args:
        ts_ms: 毫秒级 Unix 时间戳 (TickFlow SDK quote.timestamp / kline.timestamp)

    Returns:
        已交易分钟数 (0~240)。timestamp 为 None/无效时返回 240 (视作全天,
        避免量比被折算成 0)。
    """
    return CN_PROFILE.trading_minutes_elapsed_from_ts(ts_ms)
