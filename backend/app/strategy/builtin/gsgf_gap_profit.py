"""缺口盈利 -- 诱空缺口回补反转, 或未回补缺口上方再度跳空拔升."""

import numpy as np
from _gsgf_risk import (
    RISK_PARAM_DEFS,
    apply_shared_entry_filters,
    c_zone_death_cross,
    prior_true,
    prior_volume_mean,
)

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_rolling_min,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "gsgf_gap_profit",
    "name": "缺口盈利模式",
    "description": (
        "诱空缺口后3日内放量大阳回补, 或未回补向上缺口上方横盘后再度跳空. "
        "回测建议最多3只、单票仓位约20%."
    ),
    "tags": ["股是股非", "缺口", "跳空"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "enable_trap_gap", "label": "启用诱空缺口反转", "type": "bool", "default": True},
        {"id": "enable_repeat_gap", "label": "启用再度缺口拔升", "type": "bool", "default": True},
        {
            "id": "trap_fill_days",
            "label": "诱空回补确认天数",
            "type": "int",
            "default": 3,
            "min": 1,
            "max": 5,
            "step": 1,
        },
        {
            "id": "trap_change_pct",
            "label": "回补阳线最低涨幅%",
            "type": "float",
            "default": 4.0,
            "min": 2.0,
            "max": 10.0,
            "step": 0.5,
        },
        {
            "id": "repeat_min_hold",
            "label": "再度跳空前最少横盘天数",
            "type": "int",
            "default": 5,
            "min": 3,
            "max": 15,
            "step": 1,
        },
        *RISK_PARAM_DEFS,
    ],
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_gsgf_trap_gap", "signal_gsgf_repeat_gap"]
EXIT_SIGNALS = ["signal_gsgf_c_zone", "signal_ma10_lose"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15


class GsgfGapProfitMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 80

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        change = matrix_feature(market, "change_pct")
        vol_ma10 = prior_volume_mean(market, 10)
        vol_ma20 = prior_volume_mean(market, 20)
        prev_low = shift(market.low, 1)
        prev_high = shift(market.high, 1)
        trap_gap = (market.open < prev_low) & (market.volume < vol_ma10)
        up_gap = market.open > prev_high
        fill_yang = (market.close > market.open) & (
            change > float(params.get("trap_change_pct", 4.0)) / 100.0
        )

        entry_trap = np.zeros(market.shape, dtype=bool)
        if params.get("enable_trap_gap", True):
            fill_days = max(1, int(params.get("trap_fill_days", 3)))
            for lag in range(1, fill_days + 1):
                fill_level = shift(market.low, lag + 1)
                filled_now = (
                    prior_true(trap_gap, lag)
                    & fill_yang
                    & np.isfinite(fill_level)
                    & (market.close >= fill_level)
                )
                already = np.zeros(market.shape, dtype=bool)
                for mid in range(1, lag):
                    already |= (
                        prior_true(fill_yang, mid)
                        & np.isfinite(fill_level)
                        & (shift(market.close, mid) >= fill_level)
                    )
                entry_trap |= filled_now & ~already

        entry_repeat = np.zeros(market.shape, dtype=bool)
        if params.get("enable_repeat_gap", True):
            min_hold = max(3, int(params.get("repeat_min_hold", 5)))
            max_hold = 20
            huge_yin = (
                (market.close < market.open)
                & (market.volume > vol_ma20 * 2.0)
            )
            prev_low_s = shift(market.low, 1)
            prev_huge = shift(huge_yin.astype(np.float32), 1)
            for lag in range(min_hold, max_hold + 1):
                gap_floor = shift(market.high, lag + 1)
                consol_low = valid_rolling_min(prev_low_s, np.isfinite(prev_low_s), lag)
                had_huge = valid_rolling_max(prev_huge, np.isfinite(prev_huge), lag)
                unfilled = np.isfinite(gap_floor) & np.isfinite(consol_low) & (
                    consol_low > gap_floor
                )
                clean = ~(np.isfinite(had_huge) & (had_huge > 0))
                entry_repeat |= (
                    prior_true(up_gap, lag)
                    & unfilled
                    & clean
                    & up_gap
                    & (market.volume > vol_ma10)
                )

        entry = entry_trap | entry_repeat
        entry = apply_shared_entry_filters(entry, market, params)

        death = c_zone_death_cross(market)
        ma10_lose = market.close < matrix_feature(market, "ma10")
        exit_ = death | ma10_lose
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(
                entry & entry_trap, 0, np.where(entry & entry_repeat, 1, -1)
            ).astype(np.int16),
            exit_signal_code=np.where(death, 0, np.where(ma10_lose, 1, -1)).astype(np.int16),
            entry_signal_ids=("signal_gsgf_trap_gap", "signal_gsgf_repeat_gap"),
            exit_signal_ids=("signal_gsgf_c_zone", "signal_ma10_lose"),
        )


MATRIX_STRATEGY = GsgfGapProfitMatrixStrategy()
