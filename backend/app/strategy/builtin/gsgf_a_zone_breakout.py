"""A区均线归位突破 -- 量价异动让均线归位后的第一启动点."""

import numpy as np
from _gsgf_risk import (
    RISK_PARAM_DEFS,
    apply_shared_entry_filters,
    c_zone_death_cross,
    prior_volume_mean,
    upper_shadow_and_body,
)

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "gsgf_a_zone_breakout",
    "name": "A区均线归位突破",
    "description": (
        "MA5>MA10>MA20 且密集、MA20 向上, 放量突破并创20日新高. "
        "回测建议最多3只、单票仓位约20%."
    ),
    "tags": ["股是股非", "A区", "均线", "放量突破"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "spread_pct_max",
            "label": "均线聚合度上限% (|MA20-MA5|/MA5)",
            "type": "float",
            "default": 3.0,
            "min": 0.5,
            "max": 8.0,
            "step": 0.5,
        },
        {
            "id": "vol_mult",
            "label": "成交量/20日均量倍数",
            "type": "float",
            "default": 2.0,
            "min": 1.0,
            "max": 5.0,
            "step": 0.1,
        },
        {
            "id": "breakout_pct",
            "label": "收盘相对MA20最低超出%",
            "type": "float",
            "default": 3.0,
            "min": 0.0,
            "max": 10.0,
            "step": 0.5,
        },
        *RISK_PARAM_DEFS,
    ],
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_gsgf_a_zone"]
EXIT_SIGNALS = [
    "signal_gsgf_c_zone",
    "signal_ma10_lose",
    "signal_gsgf_high_vol_upper_shadow",
]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 15


class GsgfAZoneBreakoutMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 80

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ma5 = matrix_feature(market, "ma5")
        ma10 = matrix_feature(market, "ma10")
        ma20 = matrix_feature(market, "ma20")
        # 文档写 (MA20-MA5)/MA5, 多头时为负会失效, 按聚合度取绝对值.
        dense = np.abs(ma20 - ma5) / ma5 < float(params.get("spread_pct_max", 3.0)) / 100.0
        prior_close = shift(market.close, 1)
        high_20_prior = valid_rolling_max(prior_close, np.isfinite(prior_close), 20)
        entry = (ma5 > ma10) & (ma10 > ma20)
        entry &= ma20 > shift(ma20, 5)
        entry &= dense
        entry &= market.volume > prior_volume_mean(market, 20) * float(params.get("vol_mult", 2.0))
        entry &= market.close > ma20 * (1.0 + float(params.get("breakout_pct", 3.0)) / 100.0)
        entry &= market.close > high_20_prior
        entry = apply_shared_entry_filters(entry, market, params)

        death = c_zone_death_cross(market)
        ma10_lose = market.close < ma10
        body, upper = upper_shadow_and_body(market)
        shadow = (upper > body * 2.0) & (market.volume > prior_volume_mean(market, 10))
        exit_ = death | ma10_lose | shadow
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(
                death, 0, np.where(ma10_lose, 1, np.where(shadow, 2, -1))
            ).astype(np.int16),
            entry_signal_ids=("signal_gsgf_a_zone",),
            exit_signal_ids=(
                "signal_gsgf_c_zone",
                "signal_ma10_lose",
                "signal_gsgf_high_vol_upper_shadow",
            ),
        )


MATRIX_STRATEGY = GsgfAZoneBreakoutMatrixStrategy()
