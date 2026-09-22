"""US daily bars via Sina (primary) with yfinance fallback.

数据链路复用 ``app/services/hk_data_adapter.fetch_us_daily_akshare``:
新浪主源 (共享 MiniRacer 解码, 线程安全) + yfinance 兜底, 两者都失败返回
空 df 不抛错。本 provider 只做协议包装 —— 新浪对 class share/次新股/低流动
性标的覆盖不全, 兜底语义保留在适配层, 这里不重复实现。

provider 层动机 (2026-09-22 数据地基批 #5): 此前 US 只注册 yfinance,
Yahoo 403 时 else 分支仅 warning 后 continue, 循环空烧最终**静默返回空 df**;
注册新浪主源后日 K 默认不再依赖 Yahoo。yfinance 仍保留注册 (兜底/实时)。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities

logger = logging.getLogger(__name__)


class SinaUSProvider:
    """美股日 K: 新浪主源 (经 hk_data_adapter 适配层), yfinance 兜底在适配层内。

    线程安全: 适配层的共享 MiniRacer + 锁已串行化解码临界区; 本类不引入
    额外并发, get_daily 按标的串行调用。
    """

    name = "sina"
    capabilities = ProviderCapabilities(daily=True)

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
    ) -> pl.DataFrame:
        """逐标的拉日 K 并按 [start_time, end_time] 过滤。

        约定与 YFinanceProvider.get_daily 一致: 部分标的失败不抛错 (适配层
        返回空 df), 只跳过该标的; 全部失败返回空 df。
        """
        from app.services.hk_data_adapter import fetch_us_daily_akshare

        if not symbols:
            return pl.DataFrame()
        end_d = end_time.date() if end_time else datetime.now(UTC).date()
        start_d = start_time.date() if start_time else end_d - timedelta(days=365)
        frames: list[pl.DataFrame] = []
        for symbol in symbols:
            try:
                frame = fetch_us_daily_akshare(symbol)
            except Exception as exc:  # 适配层承诺不抛, 防御性兜底
                logger.warning("sina us daily failed %s: %s", symbol, exc)
                continue
            if frame.is_empty():
                continue
            frames.append(frame.filter(
                (pl.col("date").dt.date() >= start_d) & (pl.col("date").dt.date() <= end_d)
            ))
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="vertical_relaxed")

    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        """美股证券资料由独立源提供 (与 HKDailyProvider 同款约定)。"""
        raise NotImplementedError("美股证券资料由独立证券数据源提供")

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
    ) -> pl.DataFrame:
        """新浪源无复权因子 (未复权口径), 交给 yfinance 注册项提供。"""
        return pl.DataFrame()

    def get_minute(self, *args: Any, **kwargs: Any) -> pl.DataFrame:
        raise NotImplementedError("此数据源不提供分钟行情")

    def get_realtime(self, *args: Any, **kwargs: Any) -> pl.DataFrame:
        raise NotImplementedError("此数据源不提供实时行情")
