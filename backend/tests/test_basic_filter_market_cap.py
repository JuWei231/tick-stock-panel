"""basic_filter 市值必须用 raw_close, 不能用前复权 close。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.backtest.matrix import build_basic_filter_mask, build_market_data_matrix
from app.backtest.strategy import _basic_filter_dependencies
from app.strategy.engine import StrategyEngine


def _cap_panel() -> pl.DataFrame:
    # A: 前复权 5, 原始 10, 股本 3e8 -> 真实 30 亿 / 复权 15 亿
    # B: 前复权 30, 原始 10, 股本 1e8 -> 真实 10 亿 / 复权 30 亿
    return pl.DataFrame({
        "symbol": ["A.SH", "B.SH"],
        "name": ["A", "B"],
        "date": [date(2024, 1, 2)] * 2,
        "open": [10.0, 10.0],
        "high": [10.0, 10.0],
        "low": [10.0, 10.0],
        "close": [5.0, 30.0],
        "raw_close": [10.0, 10.0],
        "volume": [100.0, 100.0],
        "total_shares": [3e8, 1e8],
        "float_shares": [3e8, 1e8],
    })


def test_polars_basic_filter_market_cap_uses_raw_close():
    df = _cap_panel()
    expr = StrategyEngine._basic_filter_expr(df, {
        "enabled": True,
        "market_cap_min": 20e8,
    })
    assert expr is not None
    assert df.select(expr.alias("ok"))["ok"].to_list() == [True, False]


def test_polars_basic_filter_skips_market_cap_without_raw_close():
    df = _cap_panel().drop("raw_close")
    expr = StrategyEngine._basic_filter_expr(df, {
        "enabled": True,
        "market_cap_min": 20e8,
    })
    assert expr is None


def test_matrix_basic_filter_market_cap_uses_raw_close():
    market = build_market_data_matrix(
        _cap_panel(),
        field_columns={"raw_close", "total_shares"},
    )
    mask = build_basic_filter_mask(market, {
        "enabled": True,
        "market_cap_min": 20e8,
    })
    assert mask.tolist() == [[True, False]]


def test_basic_filter_dependencies_require_raw_close_for_cap_bounds():
    deps = _basic_filter_dependencies({"enabled": True, "market_cap_min": 10e8})
    assert "raw_close" in deps
    assert "total_shares" in deps

    price_only = _basic_filter_dependencies({"enabled": True, "price_min": 3})
    assert "raw_close" not in price_only


def test_matrix_field_columns_include_raw_close_for_cap_filter():
    strategy = SimpleNamespace(
        matrix_strategy=SimpleNamespace(
            required_fields=lambda: frozenset({"close"}),
        ),
        basic_filter={"market_cap_min": 10e8},
        meta={"scoring": {}},
    )
    fields = StrategyEngine._matrix_field_columns(strategy, None, {})
    assert "raw_close" in fields
    assert "total_shares" in fields


def test_polars_basic_filter_warns_when_cap_bound_has_no_shares(monkeypatch, caplog):
    """配了市值约束却拿不到股本列: 约束被丢弃, 必须显式告警而不是静默放行。"""
    from app import share_capital

    monkeypatch.setattr(share_capital, "_market_cap_skipped_warned", set())
    df = _cap_panel().drop("total_shares", "float_shares")

    with caplog.at_level("WARNING"):
        expr = StrategyEngine._basic_filter_expr(
            df, {"enabled": True, "market_cap_min": 20e8}
        )

    assert expr is None
    assert any("市值约束被静默跳过" in record.getMessage() for record in caplog.records)


def test_matrix_basic_filter_warns_when_cap_bound_has_no_shares(monkeypatch, caplog):
    """矩阵侧同理: 无 total_shares 字段时市值上下界不参与掩码, 但要告警。"""
    from app import share_capital

    monkeypatch.setattr(share_capital, "_market_cap_skipped_warned", set())
    market = build_market_data_matrix(_cap_panel(), field_columns={"raw_close"})

    with caplog.at_level("WARNING"):
        mask = build_basic_filter_mask(market, {"enabled": True, "market_cap_min": 20e8})

    assert mask.tolist() == [[True, True]]  # 无股本 → 该约束不生效
    assert any("市值约束被静默跳过" in record.getMessage() for record in caplog.records)
