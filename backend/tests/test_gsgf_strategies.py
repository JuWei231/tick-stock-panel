from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix
from app.strategy.builtin._gsgf_risk import (
    apply_shared_entry_filters,
    three_yang_control,
    volume_time_space_blocked,
)
from app.strategy.engine import StrategyEngine

_BUILTIN = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"

_OFF_RISK = {
    "use_volume_time_space_filter": False,
    "require_three_yang": False,
}


def _grind(
    symbol: str,
    n: int,
    *,
    start_px: float = 10.0,
    step: float = 0.01,
    vol: float = 1000.0,
    start: date = date(2024, 1, 2),
) -> list[dict]:
    rows = []
    for i in range(n):
        close = start_px + i * step
        rows.append({
            "symbol": symbol,
            "date": start + timedelta(days=i),
            "open": close - 0.01,
            "high": close + 0.02,
            "low": close - 0.02,
            "close": close,
            "volume": vol,
        })
    return rows


def _patch(rows: list[dict], index: int, **fields: float) -> None:
    rows[index].update(fields)


def _market(rows: list[dict]):
    return build_market_data_matrix(pl.DataFrame(rows))


def _strategy(filename: str):
    return StrategyEngine._load_file(_BUILTIN / filename).matrix_strategy


def _last_entry(strategy, rows: list[dict], params: dict | None = None) -> bool:
    merged = {**_OFF_RISK, **(params or {})}
    market = _market(rows)
    signals = strategy.compute_signals(market, merged)
    return bool(signals.entry[-1, 0])


def test_volume_time_space_blocks_only_under_trapping_high():
    n = 80
    blocked = _grind("x", n, step=0.0)
    allowed = _grind("y", n, step=0.0)
    _patch(blocked, 50, open=11.0, high=12.0, low=11.0, close=11.8, volume=8000.0)
    _patch(blocked, -1, open=11.4, high=11.6, low=11.3, close=11.5, volume=1000.0)
    _patch(allowed, 50, open=11.0, high=12.0, low=11.0, close=11.8, volume=8000.0)
    _patch(allowed, -1, open=12.0, high=12.4, low=11.9, close=12.2, volume=1000.0)

    market = _market(blocked + allowed)
    mask = volume_time_space_blocked(market, {"vts_proximity_pct": 5.0})
    assert mask[-1, market.symbols.index("x")]
    assert not mask[-1, market.symbols.index("y")]


def test_three_yang_requires_yang_volume_and_count():
    n = 40
    hit = _grind("hit", n, step=0.0)
    miss = _grind("miss", n, step=0.0)
    for i in range(n - 20, n):
        if (i - (n - 20)) % 3 != 0:
            _patch(hit, i, open=9.8, high=10.2, low=9.7, close=10.1, volume=2000.0)
        else:
            _patch(hit, i, open=10.1, high=10.2, low=9.7, close=9.8, volume=400.0)
        _patch(miss, i, open=10.1, high=10.2, low=9.7, close=9.8, volume=2000.0)

    market = _market(hit + miss)
    flag = three_yang_control(market)
    assert flag[-1, market.symbols.index("hit")]
    assert not flag[-1, market.symbols.index("miss")]


def test_shared_filter_can_require_three_yang():
    n = 40
    rows = _grind("x", n, step=0.0)
    for i in range(n - 20, n):
        _patch(rows, i, open=10.1, high=10.2, low=9.7, close=9.8, volume=2000.0)
    market = _market(rows)
    entry = np.ones(market.shape, dtype=bool)
    filtered = apply_shared_entry_filters(
        entry,
        market,
        {"use_volume_time_space_filter": False, "require_three_yang": True},
    )
    assert not filtered[-1, 0]


def test_a_zone_enters_on_aligned_volume_breakout():
    n = 90
    hit = _grind("hit", n)
    miss = _grind("miss", n)
    _patch(hit, -1, open=10.90, high=11.30, low=10.88, close=11.20, volume=5000.0)
    _patch(miss, -1, open=10.90, high=11.30, low=10.88, close=11.20, volume=1000.0)
    assert _last_entry(_strategy("gsgf_a_zone_breakout.py"), hit)
    assert not _last_entry(_strategy("gsgf_a_zone_breakout.py"), miss)


def test_a_zone_exits_on_close_below_ma10():
    n = 90
    rows = _grind("x", n)
    _patch(rows, -1, open=9.0, high=9.2, low=8.5, close=8.6, volume=1000.0)
    market = _market(rows)
    signals = _strategy("gsgf_a_zone_breakout.py").compute_signals(market, _OFF_RISK)
    assert signals.exit[-1, 0]
    code = int(signals.exit_signal_code[-1, 0])
    assert signals.exit_signal_ids[code] in {
        "signal_gsgf_c_zone",
        "signal_ma10_lose",
    }


def test_b_zone_enters_after_prior_surge_and_shrink_reversal():
    n = 80
    hit = _grind("hit", n, step=0.0, start_px=10.0)
    miss = _grind("miss", n, step=0.0, start_px=10.0)
    _patch(hit, -8, open=10.05, high=10.70, low=10.00, close=10.60, volume=4000.0)
    for i in range(-7, -2):
        px = 10.40 - (i + 7) * 0.05
        _patch(hit, i, open=px + 0.02, high=px + 0.06, low=px - 0.04, close=px, volume=500.0)
    _patch(hit, -2, open=10.16, high=10.18, low=10.08, close=10.10, volume=400.0)
    _patch(hit, -1, open=10.08, high=10.50, low=10.00, close=10.45, volume=650.0)
    _patch(miss, -2, open=10.16, high=10.18, low=10.08, close=10.10, volume=400.0)
    _patch(miss, -1, open=10.08, high=10.50, low=10.00, close=10.45, volume=650.0)
    assert _last_entry(_strategy("gsgf_b_zone_pullback.py"), hit)
    assert not _last_entry(_strategy("gsgf_b_zone_pullback.py"), miss)


def test_washout_engulf_enters_on_next_day_reversal():
    n = 80
    hit = _grind("hit", n, step=0.01)
    miss = _grind("miss", n, step=0.01)
    _patch(hit, -2, open=10.50, high=11.40, low=10.40, close=10.55, volume=4000.0)
    _patch(miss, -2, open=10.50, high=11.40, low=10.40, close=10.55, volume=4000.0)
    _patch(hit, -1, open=10.45, high=11.50, low=10.42, close=10.80, volume=3500.0)
    _patch(miss, -1, open=10.20, high=10.40, low=10.10, close=10.30, volume=3500.0)
    assert _last_entry(_strategy("gsgf_washout_engulf.py"), hit)
    assert not _last_entry(_strategy("gsgf_washout_engulf.py"), miss)


def test_trap_gap_enters_when_gap_is_filled_within_three_days():
    n = 80
    hit = _grind("hit", n, step=0.0, start_px=10.0)
    miss = _grind("miss", n, step=0.0, start_px=10.0)
    _patch(hit, -4, open=10.0, high=10.1, low=9.90, close=10.0, volume=1000.0)
    _patch(hit, -3, open=9.70, high=9.80, low=9.60, close=9.65, volume=400.0)
    _patch(hit, -2, open=9.66, high=9.78, low=9.60, close=9.70, volume=450.0)
    _patch(hit, -1, open=9.72, high=10.20, low=9.68, close=10.20, volume=1500.0)
    _patch(miss, -4, open=10.0, high=10.1, low=9.90, close=10.0, volume=1000.0)
    _patch(miss, -3, open=9.70, high=9.80, low=9.60, close=9.65, volume=400.0)
    _patch(miss, -2, open=9.66, high=9.78, low=9.60, close=9.70, volume=450.0)
    _patch(miss, -1, open=9.72, high=9.95, low=9.68, close=9.88, volume=1500.0)
    params = {**_OFF_RISK, "enable_repeat_gap": False}
    assert _last_entry(_strategy("gsgf_gap_profit.py"), hit, params)
    assert not _last_entry(_strategy("gsgf_gap_profit.py"), miss, params)


def test_repeat_gap_enters_after_unfilled_base():
    n = 80
    hit = _grind("hit", n, step=0.0, start_px=10.0)
    _patch(hit, -8, open=10.0, high=10.05, low=9.95, close=10.0, volume=1000.0)
    _patch(hit, -7, open=10.20, high=10.30, low=10.15, close=10.25, volume=1200.0)
    for i in range(-6, -1):
        _patch(hit, i, open=10.22, high=10.28, low=10.18, close=10.24, volume=800.0)
    _patch(hit, -1, open=10.35, high=10.50, low=10.30, close=10.45, volume=1600.0)
    params = {**_OFF_RISK, "enable_trap_gap": False}
    assert _last_entry(_strategy("gsgf_gap_profit.py"), hit, params)


def test_gsgf_strategies_load_as_matrix_native():
    engine = StrategyEngine(strategy_dirs=[_BUILTIN])
    for sid in (
        "gsgf_a_zone_breakout",
        "gsgf_b_zone_pullback",
        "gsgf_washout_engulf",
        "gsgf_gap_profit",
    ):
        strategy = engine.get(sid)
        assert strategy.execution_backend == "matrix_native"
        assert strategy.matrix_strategy is not None
        assert strategy.meta["asset_types"] == ["stock"]
        assert "股是股非" in strategy.meta["tags"]
