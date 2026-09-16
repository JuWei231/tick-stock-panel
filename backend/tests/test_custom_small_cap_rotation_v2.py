"""小市值轮动 v2 相对 v1 的三处行为: 流动性门槛、动态退场、目标数自适应。

策略本体是用户运行时文件 (data/strategies/custom/, 不入库), 因此按引擎的文件加载
入口动态导入; 文件缺失时 skip, 不让用户数据状态影响仓库测试。
"""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix

_CUSTOM_DIR = Path(__file__).resolve().parents[2] / "data" / "strategies" / "custom"
_V2_PATH = _CUSTOM_DIR / "custom_small_cap_rotation_v2.py"
_V1_PATH = _CUSTOM_DIR / "custom_small_cap_rotation.py"

_BASE_DATE = date(2024, 1, 2)
_MAIN_BOARD = ("600001.SH", "600002.SH")  # 两只沪主板, 便于构造"合格标的不足"场景


def _load_module(path: Path):
    if not path.exists():
        pytest.skip(f"custom strategy not present: {path}")
    spec = importlib.util.spec_from_file_location(f"_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def v2():
    return _load_module(_V2_PATH)


@pytest.fixture(scope="module")
def v1():
    return _load_module(_V1_PATH)


def _panel(
    rows: list[tuple[int, str, str, float, float]],
) -> pl.DataFrame:
    """rows: (日偏移, symbol, name, close, amount), 总股本固定 2 亿股 → 市值 = close * 2 亿。"""
    return pl.DataFrame({
        "symbol": [r[1] for r in rows],
        "name": [r[2] for r in rows],
        "date": [_BASE_DATE + timedelta(days=r[0]) for r in rows],
        "open": [r[3] for r in rows],
        "high": [r[3] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[3] for r in rows],
        # 测试无复权: 原始价 == 前复权价 (市值口径用 raw_close x 当日股本)
        "raw_close": [r[3] for r in rows],
        "volume": [1000.0 for _ in rows],
        "amount": [r[4] for r in rows],
        "total_shares": [2.0e8 for _ in rows],
    })


def _market(panel: pl.DataFrame):
    return build_market_data_matrix(
        panel, field_columns={"total_shares", "amount", "raw_close"}
    )


def test_liquidity_gate_excludes_thin_names(v2):
    """两只都符合市值区间, 但其中一只日均额低于门槛 → 只应选中达标的那只。"""
    symbols = ["600001.SH", "600002.SH"]
    names = ["标的甲", "标的乙"]
    # 标的甲 close=10 → 市值 20 亿(贴区间下沿); 标的乙 close=11 → 22 亿。两者都在 [20,30] 亿。
    rows = []
    for d in range(6):
        rows.append((d, symbols[0], names[0], 10.0, 5e7))   # 0.5 亿元/日
        rows.append((d, symbols[1], names[1], 11.0, 1e7))   # 0.1 亿元/日
    market = _market(_panel(rows))
    params = {"min_amount": 0.3, "amount_window": 3}

    signals = v2.MATRIX_STRATEGY.compute_signals(market, params)

    selected = [
        symbol
        for symbol, hit in zip(market.symbols, signals.entry[-1], strict=True)
        if hit
    ]
    assert selected == ["600001.SH"]


def test_cap_growth_does_not_force_sell(v2):
    """市值涨出区间属于被动升值: 不立即卖出, 由调仓日按排名处理。"""
    symbols = ["600001.SH", "600002.SH"]
    names = ["会涨出区间的标的", "稳定小市值标的"]
    rows = []
    for d in range(4):
        # 第 3 天起 close=20 → 市值 40 亿, 超出默认上限 30 亿
        rows.append((d, symbols[0], names[0], 20.0 if d >= 3 else 10.0, 5e7))
        rows.append((d, symbols[1], names[1], 11.0, 5e7))
    market = _market(_panel(rows))
    # refresh_rate 远大于窗口 → 窗口内没有调仓日
    params = {"refresh_rate": 60, "amount_window": 1, "min_amount": 0.0}

    signals = v2.MATRIX_STRATEGY.compute_signals(market, params)
    a = market.symbols.index(symbols[0])

    assert not signals.exit[3, a], "市值越界不应触发立即卖出"
    # 但它当日确实不再是目标持仓
    assert not signals.entry[3, a]


def test_dynamic_exit_fires_when_liquidity_collapses(v2):
    """流动性塌陷属于持有资格失效 → 不等调仓日, 当日即卖。"""
    symbols = ["600001.SH", "600002.SH"]
    names = ["会缩量的标的", "稳定标的"]
    rows = []
    for d in range(5):
        # 甲第 3 天起成交额掉到 0.01 亿, 跌破 0.5 亿门槛
        rows.append((d, symbols[0], names[0], 10.0, 1e6 if d >= 3 else 5e7))
        rows.append((d, symbols[1], names[1], 11.0, 5e7))
    market = _market(_panel(rows))
    params = {"refresh_rate": 60, "amount_window": 1, "min_amount": 0.5}

    signals = v2.MATRIX_STRATEGY.compute_signals(market, params)
    a = market.symbols.index(symbols[0])

    assert signals.exit[3, a], "流动性塌陷当日应给卖点"
    assert not signals.exit[2, a], "塌陷前不应有卖点"
    assert not signals.entry[3, a]


def test_exit_is_scoped_to_target_composition(v2):
    """退场只能作用于目标组合, 不得让全市场不合格标的每天产生卖点。

    回归用: 早期版本用 `~eligible` 直接做卖点条件, 实测 45 天窗口产生 24.7 万个卖点
    (全市场 5500+ 只/日), 淹没信号统计并污染执行计数。
    """
    symbols = ["600001.SH", "600002.SH", "600003.SH"]
    names = ["甲", "乙", "丙"]
    rows = []
    # 丙市值 100 亿, 永远不在 [20,30] 亿区间 → 永远不合格, 但也永远不该产生卖点
    for d in range(4):
        rows.append((d, symbols[0], names[0], 10.0, 5e7))
        rows.append((d, symbols[1], names[1], 11.0, 5e7))
        rows.append((d, symbols[2], names[2], 50.0, 5e7))
    market = _market(_panel(rows))
    signals = v2.MATRIX_STRATEGY.compute_signals(
        market, {"hold_num": 2, "refresh_rate": 60, "amount_window": 1, "min_amount": 0.0}
    )

    c = market.symbols.index(symbols[2])
    assert not signals.entry[:, c].any()
    assert not signals.exit[:, c].any(), "从未入选的标的不应产生卖点"

    # 全窗口卖点总数不得超过持仓规模(第一日无上一日持仓, 故为 0)
    assert int(signals.exit.sum()) == 0


def test_adaptive_target_keeps_actual_qualified_count(v2):
    """合格标的少于 hold_num 时只持有实际数量, 且得分不因池子变小而失真。

    hold_num=3, 第 3 天最便宜的一只涨出区间 → 剩 2 只合格。目标数应降为 2;
    同时 rank1 的得分必须仍是 100*(3-1)/3 = 66.7, 而不是按"合格数=2"当分母算成 50,
    更不能像按"实际持仓数"当分母那样算成 0。
    """
    symbols = ["600001.SH", "600002.SH", "600003.SH"]
    names = ["会涨出区间的标的", "稳定标的乙", "稳定标的丙"]
    rows = []
    for d in range(4):
        # 甲 20 亿 → 第 3 天涨到 40 亿失格; 乙 22 亿; 丙 24 亿
        rows.append((d, symbols[0], names[0], 20.0 if d >= 3 else 10.0, 5e7))
        rows.append((d, symbols[1], names[1], 11.0, 5e7))
        rows.append((d, symbols[2], names[2], 12.0, 5e7))
    market = _market(_panel(rows))
    params = {"hold_num": 3, "refresh_rate": 60, "amount_window": 1, "min_amount": 0.0}

    signals = v2.MATRIX_STRATEGY.compute_signals(market, params)

    day_entries = [int(signals.entry[t].sum()) for t in range(market.shape[0])]
    # 第 3 天前 3 只都合格; 第 3 天只剩 2 只合格 → 目标数自适应, 不凑数
    assert day_entries == [3, 3, 3, 2]

    a = market.symbols.index(symbols[0])
    b = market.symbols.index(symbols[1])
    c = market.symbols.index(symbols[2])
    assert signals.entry[3, b]
    assert signals.entry[3, c]
    assert not signals.entry[3, a]

    # 得分分母固定为 hold_num: rank0=100, rank1=66.7, 与当日合格数量无关
    assert float(signals.score[3, b]) == pytest.approx(100.0)
    assert float(signals.score[3, c]) == pytest.approx(100.0 * (3 - 1) / 3)
    assert signals.score[3, b] > signals.score[3, c]


def test_score_denominator_is_stable_across_pool_sizes(v2):
    """得分分母恒为 hold_num: 合格池缩到 2 只时 rank1 仍是 66.7, 而不是 50 或 0。

    把 hold_num 设为 3 但只让 2 只合格, 若分母错用"当日合格数"会得到 50,
    错用"实际持仓数(=2)"也会得到 50; 只有固定 hold_num 才得到 100*(3-1)/3。
    """
    symbols = ["600001.SH", "600002.SH"]
    names = ["甲", "乙"]
    rows = []
    for d in range(3):
        rows.append((d, symbols[0], names[0], 10.0, 5e7))   # 20 亿
        rows.append((d, symbols[1], names[1], 11.0, 5e7))   # 22 亿
    market = _market(_panel(rows))
    signals = v2.MATRIX_STRATEGY.compute_signals(
        market, {"hold_num": 3, "refresh_rate": 60, "amount_window": 1, "min_amount": 0.0}
    )

    a = market.symbols.index(symbols[0])
    b = market.symbols.index(symbols[1])
    assert int(signals.entry[-1].sum()) == 2          # 只有 2 只合格, 不凑第 3 只
    assert float(signals.score[-1, a]) == pytest.approx(100.0)
    assert float(signals.score[-1, b]) == pytest.approx(100.0 * (3 - 1) / 3)


def test_warmup_covers_amount_window_and_refresh(v2):
    assert v2.MATRIX_STRATEGY.required_warmup_bars({"amount_window": 30, "refresh_rate": 5}) == 30
    assert v2.MATRIX_STRATEGY.required_warmup_bars({"amount_window": 3, "refresh_rate": 20}) == 20


def test_rolling_mean_matches_reference(v2):
    """滚动均值: 窗口内出现非有限值(上市前 NaN)时该日无效, 不得当 0 计入。"""
    values = np.array([
        [1.0, np.nan],
        [2.0, 10.0],
        [3.0, 20.0],
        [4.0, 30.0],
    ], dtype=np.float64)

    got = v2._rolling_mean(values, 2)
    expected = np.array([
        [np.nan, np.nan],
        [1.5, np.nan],
        [2.5, 15.0],
        [3.5, 25.0],
    ])
    assert np.allclose(got, expected, equal_nan=True)

    assert np.allclose(v2._rolling_mean(values, 1), values, equal_nan=True)


def test_v2_matches_v1_selection_when_filters_neutral(v2, v1):
    """流动性门槛置 0、窗口 1 时, v2 的选股结果应与 v1 一致(板块/ST/市值口径未变)。"""
    symbols = ["600001.SH", "600002.SH", "300750.SZ", "688981.SH", "920982.BJ"]
    names = ["标的甲", "标的乙", "宁德时代", "中芯国际", "北证样例"]
    rows = []
    for d in range(4):
        for i, (symbol, name) in enumerate(zip(symbols, names, strict=True)):
            rows.append((d, symbol, name, 10.0 + i, 5e7))
    panel = _panel(rows)
    # v1 不需要 amount 字段, 但多一列不影响其矩阵
    market_v1 = build_market_data_matrix(
        panel, field_columns={"total_shares", "raw_close"}
    )
    market_v2 = _market(panel)

    sig_v1 = v1.MATRIX_STRATEGY.compute_signals(
        market_v1, {"hold_num": 2, "refresh_rate": 5}
    )
    sig_v2 = v2.MATRIX_STRATEGY.compute_signals(
        market_v2,
        {"hold_num": 2, "refresh_rate": 5, "min_amount": 0.0, "amount_window": 1},
    )

    for t in range(market_v1.shape[0]):
        assert (
            sig_v1.entry[t].tolist() == sig_v2.entry[t].tolist()
        ), f"entry 在第 {t} 日与 v1 不一致"
