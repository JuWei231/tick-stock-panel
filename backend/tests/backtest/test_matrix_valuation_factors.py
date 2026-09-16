"""矩阵因子内核的市值口径: 流通市值必须用不复权 `raw_close`。

`strategy/scoring.py`(polars 侧)与 `backtest/matrix.py`(numpy 内核)是同一因子的两条实现,
必须同口径; 且都不能用前复权 `close` —— close 已经把复权比计入价格, 再乘"当日股本"等于
把复权比计入两次, 历史截面上的流通市值被低估, 且各股比例不同 → 排序失真。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix, matrix_feature

_DATES = (2, 2, 3, 3, 4, 4)
_RAW = np.array([10.0, 20.0, 11.0, 22.0, 12.0, 24.0])
_CLOSE = _RAW * 0.8  # 前复权价 = 原始价 x ratio(0.8)
_VOLUME = np.array([1_000.0, 2_000.0, 1_100.0, 2_100.0, 1_200.0, 2_200.0])
_TURNOVER = np.array([2.0, 3.0, 2.5, 3.5, 3.0, 4.0])  # 百分数(与 enriched 口径一致)


def _panel(*, with_raw_close: bool = True) -> pl.DataFrame:
    data = {
        "symbol": ["A.SH", "B.SH"] * 3,
        "date": [date(2024, 1, day) for day in _DATES],
        "open": _CLOSE,
        "high": _CLOSE,
        "low": _CLOSE,
        "close": _CLOSE,
        "volume": _VOLUME,
        "turnover_rate": _TURNOVER,
    }
    if with_raw_close:
        data["raw_close"] = _RAW
    return pl.DataFrame(data)


def _expected(price: np.ndarray, volume: np.ndarray, turnover: np.ndarray) -> np.ndarray:
    """矩阵轴为 (time, asset): 面板按 date 升序、同日按 symbol 升序 → reshape 即可。"""
    return np.log(price * volume / turnover).reshape(3, 2)


def test_matrix_log_float_mv_uses_raw_close() -> None:
    market = build_market_data_matrix(_panel(), field_columns={"raw_close", "turnover_rate"})
    got = np.asarray(matrix_feature(market, "log_float_mv"), dtype=np.float64)

    golden = _expected(_RAW, _VOLUME, _TURNOVER)
    assert np.allclose(got, golden, rtol=1e-6, atol=1e-6)
    # 反例: 用前复权 close 会整体偏移 ln(0.8), 必须能被区分(否则测试锁不住口径)
    assert not np.allclose(got, _expected(_CLOSE, _VOLUME, _TURNOVER), rtol=1e-6, atol=1e-6)


def test_matrix_log_float_mv_without_raw_close_is_nan_and_warns(monkeypatch, caplog) -> None:
    """缺 raw_close 时全 NaN + 告警, 不静默退回前复权价。"""
    from app import share_capital

    monkeypatch.setattr(share_capital, "_raw_close_warned", set())
    market = build_market_data_matrix(_panel(with_raw_close=False), field_columns={"turnover_rate"})

    with caplog.at_level("WARNING"):
        got = np.asarray(matrix_feature(market, "log_float_mv"), dtype=np.float64)

    assert np.isnan(got).all()
    assert any("raw_close" in record.getMessage() for record in caplog.records)
