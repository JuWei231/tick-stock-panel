"""小市值轮动的标的范围过滤: 剔除 ST/退市、北交所、科创板、创业板。

策略本体是用户运行时文件 (data/strategies/custom/, 不入库), 因此这里按引擎的
文件加载入口动态导入; 文件缺失时 skip, 不让用户数据状态影响仓库测试。
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix

_STRATEGY_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "strategies"
    / "custom"
    / "custom_small_cap_rotation.py"
)
_STRATEGY_V2_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "strategies"
    / "custom"
    / "custom_small_cap_rotation_v2.py"
)

# 市值统一 25 亿(收盘价 10 元 x 总股本 2.5 亿股), 落在策略默认区间 [20, 30] 亿内,
# 保证「是否入选」只由板块 / ST 口径决定, 与市值排序无关。
_CLOSE = 10.0
_TOTAL_SHARES = 2.5e8


def _load_module(path: Path):
    if not path.exists():
        pytest.skip(f"custom strategy not present: {path}")
    spec = importlib.util.spec_from_file_location(f"_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_strategy():
    return _load_module(_STRATEGY_PATH)


@pytest.fixture(scope="module")
def strategy():
    return _load_strategy()


@pytest.fixture(scope="module")
def strategy_v2():
    return _load_module(_STRATEGY_V2_PATH)


def _panel(symbols: list[str], names: list[str]) -> pl.DataFrame:
    rows: list[dict] = []
    for day in (date(2024, 1, 2), date(2024, 1, 3)):
        for symbol, name in zip(symbols, names, strict=True):
            rows.append({
                "symbol": symbol,
                "name": name,
                "date": day,
                "open": _CLOSE,
                "high": _CLOSE,
                "low": _CLOSE,
                "close": _CLOSE,
                "raw_close": _CLOSE,  # 测试无复权: 原始价 == 前复权价
                "volume": 1000.0,
                "total_shares": _TOTAL_SHARES,
            })
    return pl.DataFrame(rows)


def test_only_main_board_non_st_is_selected(strategy):
    symbols = [
        "600519.SH",  # 沪主板 → 保留
        "000001.SZ",  # 深主板 → 保留
        "300750.SZ",  # 创业板 → 剔除
        "688981.SH",  # 科创板 → 剔除
        "920982.BJ",  # 北交所(现行 92x 号段) → 剔除
        "430047.BJ",  # 北交所(老 4x 号段) → 剔除
        "600001.SH",  # 沪主板但 ST → 剔除
    ]
    names = [
        "贵州茅台",
        "平安银行",
        "宁德时代",
        "中芯国际",
        "北证样例",
        "北证老号段",
        "ST花王",
    ]

    market = build_market_data_matrix(_panel(symbols, names), field_columns={"total_shares", "raw_close"})
    signals = strategy.MATRIX_STRATEGY.compute_signals(market, {})

    selected = [
        symbol
        for symbol, hit in zip(market.symbols, signals.entry[-1], strict=True)
        if hit
    ]
    assert set(selected) == {"600519.SH", "000001.SZ"}


def test_excluded_boards_never_get_entry_across_window(strategy):
    """整个窗口内都不得给剔除标的买点(而非仅最后一日)。"""
    symbols = ["600519.SH", "300750.SZ", "688981.SH", "920982.BJ"]
    names = ["贵州茅台", "宁德时代", "中芯国际", "北证样例"]
    market = build_market_data_matrix(_panel(symbols, names), field_columns={"total_shares", "raw_close"})
    signals = strategy.MATRIX_STRATEGY.compute_signals(market, {})

    asset_ids = [market.symbols.index(s) for s in symbols[1:]]
    assert not signals.entry[:, asset_ids].any()


def test_eligible_asset_mask_directly(strategy):
    """纯函数级断言: 号段 / 后缀 / ST 三种口径。"""
    symbols = (
        "600519.SH",  # 沪主板
        "601398.SH",  # 沪主板
        "000001.SZ",  # 深主板
        "002415.SZ",  # 深主板
        "300750.SZ",  # 创业板 300
        "301029.SZ",  # 创业板 301
        "688981.SH",  # 科创板 688
        "689009.SH",  # 科创板 689
        "920982.BJ",  # 北交所 92x
        "832982.BJ",  # 北交所 83x
        "430047.BJ",  # 北交所 43x
        "600001.SH",  # 沪主板但 ST
    )
    names = (
        "贵州茅台", "工商银行", "平安银行", "海康威视",
        "宁德时代", "利元亨", "中芯国际", "九号公司",
        "北证样例", "北证样例", "北证老号段", "*ST花王",
    )

    mask = strategy._eligible_asset_mask(symbols, names)

    expected = [True, True, True, True, False, False, False, False, False, False, False, False]
    assert list(mask) == expected
    assert mask.dtype == np.bool_


def test_eligible_asset_mask_handles_bare_numeric_codes(strategy):
    """裸 6 位码(无 .SH/.SZ/.BJ 后缀)只能靠号段判定剔除。

    实盘 symbol 都带后缀, 但号段分支必须独立成立 —— 否则一旦上游改传裸码,
    科创板/创业板/北交所会被静默放进候选池。
    """
    symbols = (
        "600519",  # 沪主板
        "000001",  # 深主板
        "300750",  # 创业板(裸码)
        "301029",  # 创业板 301(裸码)
        "688981",  # 科创板(裸码)
        "689009",  # 科创板 689(裸码)
        "920982",  # 北交所 92x(裸码)
        "832982",  # 北交所 83x(裸码)
        "430047",  # 北交所 43x(裸码)
        "870204",  # 北交所 87x(裸码)
    )
    names = ("贵州茅台", "平安银行", "宁德时代", "利元亨", "中芯国际", "九号公司",
             "北证样例", "北证样例", "北证老号段", "北证老号段")

    mask = strategy._eligible_asset_mask(symbols, names)

    expected = [True, True, False, False, False, False, False, False, False, False]
    assert list(mask) == expected


def test_meta_declares_main_board_only(strategy):
    """META.basic_filter 的 boards 声明与策略内口径保持同向(供选股页展示)。"""
    boards = strategy.META["basic_filter"]["boards"]
    assert boards == ["沪主板", "深主板"]
    assert strategy.META["basic_filter"]["exclude_st"] is True
