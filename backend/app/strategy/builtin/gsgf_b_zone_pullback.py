"""B区强势回调低吸 -- 第一次拉升后缩量回踩均线, 再度放量反转."""

import numpy as np
from _gsgf_risk import (
    RISK_PARAM_DEFS,
    any_prior,
    apply_shared_entry_filters,
    c_zone_death_cross,
    prior_volume_mean,
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
    "id": "gsgf_b_zone_pullback",
    "name": "B区强势回调低吸",
    "description": (
        "近10日有放量大阳后缩量回踩MA20/MA30, 收盘站上MA10并放量收阳. "
        "回测建议最多3只、单票仓位约20%."
    ),
    "tags": ["股是股非", "B区", "回踩", "低吸"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "prior_change_pct",
            "label": "前期异动最低涨幅%",
            "type": "float",
            "default": 5.0,
            "min": 2.0,
            "max": 15.0,
            "step": 0.5,
        },
        {
            "id": "shrink_vol_mult",
            "label": "回调日成交量/10日均量上限",
            "type": "float",
            "default": 0.7,
            "min": 0.3,
            "max": 1.0,
            "step": 0.05,
        },
        {
            "id": "reversal_change_pct",
            "label": "反转日最低涨幅%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
        *RISK_PARAM_DEFS,
    ],
    "scoring": {"momentum_20d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_gsgf_b_zone"]
EXIT_SIGNALS = ["signal_gsgf_c_zone", "signal_ma20_lose", "signal_gsgf_volume_stall"]
STOP_LOSS = -0.06
TRAILING_TAKE_PROFIT_ACTIVATE = 0.15
TRAILING_TAKE_PROFIT_DRAWDOWN = 0.05
MAX_HOLD_DAYS = 20


class GsgfBZonePullbackMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 80

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        change = matrix_feature(market, "change_pct")
        prior_surge = (change > float(params.get("prior_change_pct", 5.0)) / 100.0) & (
            market.volume > prior_volume_mean(market, 5) * 1.5
        )
        ma10 = matrix_feature(market, "ma10")
        ma20 = matrix_feature(market, "ma20")
        ma30 = matrix_feature(market, "ma30")
        shrink = market.volume < prior_volume_mean(market, 10) * float(
            params.get("shrink_vol_mult", 0.7)
        )
        touch = (market.low <= ma20) | (market.low <= ma30)
        reversal = (
            (market.close > market.open)
            & (change > float(params.get("reversal_change_pct", 3.0)) / 100.0)
            & (market.volume > shift(market.volume, 1) * 1.5)
        )
        entry = any_prior(prior_surge, 10)
        entry &= shrink
        entry &= touch
        entry &= market.close > ma10
        entry &= reversal
        entry = apply_shared_entry_filters(entry, market, params)

        death = c_zone_death_cross(market)
        ma20_lose = market.close < ma20
        stall = (market.close <= shift(market.high, 1)) & (
            market.volume > prior_volume_mean(market, 10)
        )
        exit_ = death | ma20_lose | stall
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(
                death, 0, np.where(ma20_lose, 1, np.where(stall, 2, -1))
            ).astype(np.int16),
            entry_signal_ids=("signal_gsgf_b_zone",),
            exit_signal_ids=(
                "signal_gsgf_c_zone",
                "signal_ma20_lose",
                "signal_gsgf_volume_stall",
            ),
        )


MATRIX_STRATEGY = GsgfBZonePullbackMatrixStrategy()
