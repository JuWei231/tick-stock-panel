"""强硬洗盘反包 -- 长上影或大阴洗盘后次日完全反包."""

import numpy as np
from _gsgf_risk import (
    RISK_PARAM_DEFS,
    apply_shared_entry_filters,
    c_zone_death_cross,
    prior_true,
    prior_volume_mean,
    upper_shadow_and_body,
)

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "gsgf_washout_engulf",
    "name": "强硬洗盘反包",
    "description": (
        "放量长上影或大阴洗盘后, 次日开盘不低于洗盘日最低、收盘反包且突破洗盘日高点. "
        "回测建议最多3只、单票仓位约20%."
    ),
    "tags": ["股是股非", "洗盘", "反包"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "shadow_body_mult",
            "label": "上影/实体倍数",
            "type": "float",
            "default": 2.0,
            "min": 1.0,
            "max": 5.0,
            "step": 0.5,
        },
        {
            "id": "washout_drop_pct",
            "label": "大阴线最低跌幅%",
            "type": "float",
            "default": 4.0,
            "min": 2.0,
            "max": 10.0,
            "step": 0.5,
        },
        {
            "id": "engulf_vol_mult",
            "label": "反包日成交量/洗盘日下限",
            "type": "float",
            "default": 0.8,
            "min": 0.5,
            "max": 1.5,
            "step": 0.1,
        },
        *RISK_PARAM_DEFS,
    ],
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_gsgf_washout_engulf"]
EXIT_SIGNALS = ["signal_gsgf_c_zone", "signal_ma5_lose"]
STOP_LOSS = -0.06
TRAILING_STOP = 0.05
MAX_HOLD_DAYS = 10


class GsgfWashoutEngulfMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 80

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        body, upper = upper_shadow_and_body(market)
        change = matrix_feature(market, "change_pct")
        long_upper = upper > body * float(params.get("shadow_body_mult", 2.0))
        big_yin = (market.close < market.open) & (
            change < -float(params.get("washout_drop_pct", 4.0)) / 100.0
        )
        washout = (long_upper | big_yin) & (market.volume > prior_volume_mean(market, 10))
        day1 = prior_true(washout, 1)
        entry = day1
        entry &= market.open >= shift(market.low, 1)
        entry &= market.close > shift(market.open, 1)
        entry &= market.volume > shift(market.volume, 1) * float(
            params.get("engulf_vol_mult", 0.8)
        )
        # Daily proxy for breaking Day1 high: today's high reaches yesterday high.
        entry &= market.high >= shift(market.high, 1)
        entry &= market.close > matrix_feature(market, "ma60")
        entry = apply_shared_entry_filters(entry, market, params)

        death = c_zone_death_cross(market)
        ma5_lose = market.close < matrix_feature(market, "ma5")
        exit_ = death | ma5_lose
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(death, 0, np.where(ma5_lose, 1, -1)).astype(np.int16),
            entry_signal_ids=("signal_gsgf_washout_engulf",),
            exit_signal_ids=("signal_gsgf_c_zone", "signal_ma5_lose"),
        )


MATRIX_STRATEGY = GsgfWashoutEngulfMatrixStrategy()
