"""股是股非策略共享风控: 量时空过滤, 三阳控三阴, C区死叉."""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    matrix_feature,
    valid_rolling_max,
    valid_rolling_mean,
    valid_rolling_sum,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

RISK_PARAM_DEFS = [
    {
        "id": "use_volume_time_space_filter",
        "label": "启用量时空风险过滤",
        "type": "bool",
        "default": True,
    },
    {
        "id": "vts_proximity_pct",
        "label": "距巨量高点最小距离%",
        "type": "float",
        "default": 5.0,
        "min": 1.0,
        "max": 15.0,
        "step": 0.5,
    },
    {
        "id": "require_three_yang",
        "label": "要求三阳控三阴",
        "type": "bool",
        "default": False,
    },
]

_VTS_LOOKBACK = 60
_VTS_VOL_MULT = 2.0
_THREE_YANG_WINDOW = 20
_THREE_YANG_VOL_MULT = 1.5


def prior_volume_mean(market: MarketDataMatrix, window: int) -> np.ndarray:
    """不含当日的 window 日均量."""
    prev = shift(market.volume, 1)
    return valid_rolling_mean(prev, np.isfinite(prev), window)


def prior_true(flag: np.ndarray, periods: int) -> np.ndarray:
    prev = shift(flag.astype(np.float32), periods)
    return np.isfinite(prev) & (prev > 0)


def any_prior(flag: np.ndarray, window: int) -> np.ndarray:
    prev = shift(flag.astype(np.float32), 1)
    rolled = valid_rolling_max(prev, np.isfinite(prev), window)
    return np.isfinite(rolled) & (rolled > 0)


def volume_time_space_blocked(market: MarketDataMatrix, params: dict) -> np.ndarray:
    """拦截: 现价仍在近 60 日巨量高点下方 5% 内. 峰值不含当日; 已突破则放行."""
    lookback = _VTS_LOOKBACK
    proximity = float(params.get("vts_proximity_pct", 5.0)) / 100.0
    vol_ma20 = prior_volume_mean(market, 20)
    peak_high = shift(market.high, 1)
    vol_at_peak = shift(market.volume, 1)
    vma_at_peak = shift(vol_ma20, 1)
    for lag in range(2, lookback + 1):
        high_k = shift(market.high, lag)
        higher = np.isfinite(high_k) & (
            ~np.isfinite(peak_high) | (high_k > peak_high)
        )
        peak_high = np.where(higher, high_k, peak_high)
        vol_at_peak = np.where(higher, shift(market.volume, lag), vol_at_peak)
        vma_at_peak = np.where(higher, shift(vol_ma20, lag), vma_at_peak)

    trapping = (
        np.isfinite(vol_at_peak)
        & np.isfinite(vma_at_peak)
        & (vma_at_peak > 0)
        & (vol_at_peak > vma_at_peak * _VTS_VOL_MULT)
    )
    near_below = (
        np.isfinite(market.close)
        & np.isfinite(peak_high)
        & (peak_high > 0)
        & (market.close >= peak_high * (1.0 - proximity))
        & (market.close < peak_high)
    )
    return trapping & near_below


def three_yang_control(market: MarketDataMatrix) -> np.ndarray:
    """近 20 日阳线总成交量 > 阴线总成交量 * 1.5, 且阳线根数多于阴线."""
    yang = market.close > market.open
    yin = market.close < market.open
    valid = np.isfinite(market.close) & np.isfinite(market.open) & np.isfinite(market.volume)
    yang_vol = np.where(yang & valid, market.volume, 0.0).astype(np.float32)
    yin_vol = np.where(yin & valid, market.volume, 0.0).astype(np.float32)
    yang_n = np.where(yang & valid, 1.0, 0.0).astype(np.float32)
    yin_n = np.where(yin & valid, 1.0, 0.0).astype(np.float32)
    yang_vol_sum = valid_rolling_sum(yang_vol, valid, _THREE_YANG_WINDOW)
    yin_vol_sum = valid_rolling_sum(yin_vol, valid, _THREE_YANG_WINDOW)
    yang_count = valid_rolling_sum(yang_n, valid, _THREE_YANG_WINDOW)
    yin_count = valid_rolling_sum(yin_n, valid, _THREE_YANG_WINDOW)
    return (
        np.isfinite(yang_vol_sum)
        & np.isfinite(yin_vol_sum)
        & (yang_vol_sum > yin_vol_sum * _THREE_YANG_VOL_MULT)
        & (yang_count > yin_count)
    )


def apply_shared_entry_filters(
    entry: np.ndarray,
    market: MarketDataMatrix,
    params: dict,
) -> np.ndarray:
    result = entry
    if params.get("use_volume_time_space_filter", True):
        result = result & ~volume_time_space_blocked(market, params)
    if params.get("require_three_yang", False):
        result = result & three_yang_control(market)
    return result


def c_zone_death_cross(market: MarketDataMatrix) -> np.ndarray:
    ma5 = matrix_feature(market, "ma5")
    ma10 = matrix_feature(market, "ma10")
    return (ma5 < ma10) & (shift(ma5, 1) >= shift(ma10, 1))


def upper_shadow_and_body(market: MarketDataMatrix) -> tuple[np.ndarray, np.ndarray]:
    body = np.abs(market.close - market.open)
    upper = market.high - np.maximum(market.open, market.close)
    return body, upper
