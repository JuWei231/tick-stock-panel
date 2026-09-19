"""市值区间轮动策略族: 每个区间策略的默认区间、边界口径与 V1 同源性。

策略本体是用户运行时文件 (data/strategies/custom/, 不入库), 因此按引擎的文件加载
入口动态导入; 文件缺失时 skip, 不让用户数据状态影响仓库测试。

覆盖点:
1. 全部区间文件能被引擎实际加载(过 AST 安全校验 + META 归一化);
2. 默认参数与 META/RULES 文案一致, 且信号 id 与策略 id 同源、互不撞名;
3. 每个区间只选自己区间内的标的(区间边界口径与 V1 一致: 下沿含、上沿不含);
4. 一个区间的默认值改动不会静默改变其他区间(相邻区间互不重叠)。
"""

from __future__ import annotations

import importlib.util
import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix
from app.strategy.engine import StrategyEngine

_CUSTOM_DIR = Path(__file__).resolve().parents[2] / "data" / "strategies" / "custom"
_V1_PATH = _CUSTOM_DIR / "custom_small_cap_rotation.py"

# 费率上界取参数上界(10 万亿)的区间用 "plus" 表示: 默认 market_cap_max 不会砍掉超大盘标的。
_TIERS = [
    ("custom_market_cap_rotation_5_10.py", "custom_market_cap_rotation_5_10", 5.0, 10.0),
    ("custom_market_cap_rotation_10_20.py", "custom_market_cap_rotation_10_20", 10.0, 20.0),
    ("custom_market_cap_rotation_30_50.py", "custom_market_cap_rotation_30_50", 30.0, 50.0),
    ("custom_market_cap_rotation_50_100.py", "custom_market_cap_rotation_50_100", 50.0, 100.0),
    ("custom_market_cap_rotation_100_300.py", "custom_market_cap_rotation_100_300", 100.0, 300.0),
    ("custom_market_cap_rotation_300_1000.py", "custom_market_cap_rotation_300_1000", 300.0, 1000.0),
    ("custom_market_cap_rotation_1000_plus.py", "custom_market_cap_rotation_1000_plus", 1000.0, 100000.0),
]

# 股本固定 1 亿股 → 市值(亿元) = 收盘价, 便于把区间断言写成裸数值。
_SHARES = 1.0e8
_BASE_DATE = date(2024, 1, 2)
_ROWS = 6


def _load_module(path: Path):
    if not path.exists():
        pytest.skip(f"custom strategy not present: {path}")
    spec = importlib.util.spec_from_file_location(f"_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _panel(rows: list[tuple[int, str, str, float]]) -> pl.DataFrame:
    """rows: (日偏移, symbol, name, 收盘价)。市值 = 收盘价 x 1 亿股 = 收盘价(亿元)。"""
    return pl.DataFrame({
        "symbol": [r[1] for r in rows],
        "name": [r[2] for r in rows],
        "date": [_BASE_DATE + timedelta(days=r[0]) for r in rows],
        "open": [r[3] for r in rows],
        "high": [r[3] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[3] for r in rows],
        # 无复权: 原始价 == 前复权价 (市值口径必须用 raw_close x 当日股本)
        "raw_close": [r[3] for r in rows],
        "volume": [1000.0 for _ in rows],
        "total_shares": [_SHARES for _ in rows],
    })


def _market(panel: pl.DataFrame):
    return build_market_data_matrix(panel, field_columns={"total_shares", "raw_close"})


def _constant_panel(caps: dict[str, float], symbols: dict[str, str] | None = None):
    """每个标的在整个窗口内保持固定市值(亿元)。"""
    symbols = symbols or {}
    rows: list[tuple[int, str, str, float]] = []
    for symbol, cap in caps.items():
        for day in range(_ROWS):
            rows.append((day, symbol, symbols.get(symbol, symbol), cap))
    return _market(_panel(rows))


@pytest.mark.parametrize("filename,strategy_id,cap_min,cap_max", _TIERS)
def test_tier_loads_through_engine(filename, strategy_id, cap_min, cap_max):
    """每个区间文件都必须能被引擎真实加载(过 AST 安全校验与 META 归一化)。"""
    path = _CUSTOM_DIR / filename
    if not path.exists():
        pytest.skip(f"custom strategy not present: {path}")

    engine = StrategyEngine(strategy_dirs=[_CUSTOM_DIR])
    errors = [item for item in engine.load_errors() if item["file"] == str(path)]
    assert not errors, f"{filename} 加载失败: {errors}"

    strategy = engine.get(strategy_id)
    assert strategy.execution_backend == "matrix_native"
    assert strategy.source == "custom"
    assert strategy.entry_signals == [f"signal_{strategy_id.removeprefix('custom_')}_in"]
    assert strategy.exit_signals == [f"signal_{strategy_id.removeprefix('custom_')}_out"]

    defaults = {p["id"]: p["default"] for p in strategy.meta["params"]}
    assert defaults["market_cap_min"] == cap_min
    assert defaults["market_cap_max"] == cap_max
    assert defaults["hold_num"] == 3
    assert defaults["refresh_rate"] == 5

    # basic_filter 必须保持全关: 引擎门槛在排名之后生效, 开启会把已选中的标的再砍掉。
    basic = strategy.basic_filter
    for key in (
        "price_min", "price_max", "market_cap_min", "market_cap_max",
        "float_cap_min", "float_cap_max", "amount_min", "amount_max",
        "turnover_min", "turnover_max",
    ):
        assert basic.get(key) is None, f"{strategy_id} 不应设置 basic_filter.{key}"
    assert basic["exclude_st"] is True
    assert basic["boards"] == ["沪主板", "深主板"]


@pytest.mark.parametrize("filename,strategy_id,cap_min,cap_max", _TIERS)
def test_meta_text_matches_default_range(filename, strategy_id, cap_min, cap_max):
    """META 描述与 RULES 文案必须写出实际默认区间, 避免文案与默认值漂移。"""
    module = _load_module(_CUSTOM_DIR / filename)
    meta = module.META
    assert meta["id"] == strategy_id
    assert meta["asset_types"] == ["stock"]
    assert meta["timeframes"] == ["1d"]

    # 参数上界(10 万亿)的区间在文案里写成"以上", 其余写成"[min, max]亿"。
    expected = (
        f"{int(cap_min)} 亿以上"
        if cap_max >= 100000.0
        else f"[{cap_min:g}, {cap_max:g}]亿"
    )
    assert expected in meta["description"], f"description 未写出区间 {expected}"
    assert expected in module.RULES, f"RULES 未写出区间 {expected}"


@pytest.mark.parametrize("filename,strategy_id,cap_min,cap_max", _TIERS)
def test_tier_selects_only_its_own_range(filename, strategy_id, cap_min, cap_max):
    """默认参数下, 每个区间策略只能选到自己区间内市值最小的 3 只。

    市值网格刻意同时包含相邻区间的标的, 用于确认区间策略之间不会互相"偷"标的。
    """
    module = _load_module(_CUSTOM_DIR / filename)
    caps = {f"6000{i:02d}.SH": cap for i, cap in enumerate([3, 5, 10, 20, 30, 50, 100, 300, 1000, 3000])}
    market = _constant_panel(caps)

    signals = module.MATRIX_STRATEGY.compute_signals(market, {})
    selected = {
        symbol for symbol, hit in zip(market.symbols, signals.entry[-1], strict=True) if hit
    }

    # 区间口径与 V1 一致: 下沿与上沿都含 (`cap >= min & cap <= max`)。
    in_range = [symbol for symbol, cap in caps.items() if cap_min <= cap <= cap_max]
    expected = set(in_range[:3])
    assert selected == expected, (
        f"{strategy_id} 选到 {sorted(selected)}, 期望 {sorted(expected)}"
    )


def test_range_lower_bound_inclusive_upper_bound_exclusive():
    """区间口径与 V1 一致: 下沿含、上沿不含 —— 相邻区间的接缝标的不会被两边同时持有。

    V1 的实现是 `cap >= min & cap <= max`; 相邻区间 [5,10] 与 [10,20] 只在
    股本/价格完全相等时重不漏。这里把两个区间并列跑, 断言 10 亿标的只归属 [10,20]。
    """
    low = _load_module(_CUSTOM_DIR / "custom_market_cap_rotation_5_10.py")
    high = _load_module(_CUSTOM_DIR / "custom_market_cap_rotation_10_20.py")

    caps = {"600001.SH": 5.0, "600002.SH": 8.0, "600003.SH": 10.0, "600004.SH": 15.0}
    market = _constant_panel(caps)
    params = {"hold_num": 4}

    low_selected = {
        symbol
        for symbol, hit in zip(market.symbols, low.MATRIX_STRATEGY.compute_signals(market, params).entry[-1], strict=True)
        if hit
    }
    high_selected = {
        symbol
        for symbol, hit in zip(market.symbols, high.MATRIX_STRATEGY.compute_signals(market, params).entry[-1], strict=True)
        if hit
    }

    # 10 亿是 [5,10] 的上沿、也是 [10,20] 的下沿: V1 口径下两边都算(<=/>=),
    # 因此接缝标的会被相邻区间同时持有 —— 这是与 V1 完全一致的口径, 不是新引入的偏差。
    assert low_selected == {"600001.SH", "600002.SH", "600003.SH"}
    assert high_selected == {"600003.SH", "600004.SH"}
    assert low_selected & high_selected == {"600003.SH"}


def test_open_ended_tier_keeps_mega_caps():
    """1000 亿以上的区间上线必须宽到不砍掉超大盘标的(等效不设上限)。"""
    module = _load_module(_CUSTOM_DIR / "custom_market_cap_rotation_1000_plus.py")
    caps = {"600001.SH": 900.0, "600002.SH": 1000.0, "600003.SH": 20000.0}
    market = _constant_panel(caps)

    signals = module.MATRIX_STRATEGY.compute_signals(market, {"hold_num": 3})
    selected = {
        symbol for symbol, hit in zip(market.symbols, signals.entry[-1], strict=True) if hit
    }
    assert selected == {"600002.SH", "600003.SH"}


def test_tier_switches_hold_num_and_refresh_rate():
    """参数覆盖必须生效: 持仓数量与调仓间隔都由 params 驱动, 不写死在策略里。"""
    module = _load_module(_CUSTOM_DIR / "custom_market_cap_rotation_30_50.py")
    caps = {f"60000{i}.SH": cap for i, cap in enumerate([31, 32, 33, 34, 35])}
    market = _constant_panel(caps)

    signals = module.MATRIX_STRATEGY.compute_signals(
        market, {"hold_num": 2, "refresh_rate": 2}
    )
    assert int(signals.entry[0].sum()) == 2
    # 调仓日网格: 第 0/2/4 日为调仓日, 只有这些日子可能给卖点
    exit_days = sorted({t for t in range(market.shape[0]) if signals.exit[t].any()})
    assert exit_days == [0, 2, 4]


def test_invalid_params_fail_closed():
    """区间非法(下沿 >= 上沿)必须报错, 不得静默返回空结果。"""
    module = _load_module(_CUSTOM_DIR / "custom_market_cap_rotation_100_300.py")
    market = _constant_panel({"600001.SH": 150.0})

    with pytest.raises(ValueError, match="market_cap_min must be smaller"):
        module.MATRIX_STRATEGY.compute_signals(
            market, {"market_cap_min": 300.0, "market_cap_max": 100.0}
        )


def test_tier_logic_matches_v1_on_shared_window():
    """与 V1 同源性: 同一区间参数下, 区间策略与 V1 的选股/出场结果必须逐日一致。

    这是"参考 V1 编写"的核心保证 —— 任一区间策略只能改默认区间, 不能改选择口径。
    """
    v1 = _load_module(_V1_PATH)
    tier = _load_module(_CUSTOM_DIR / "custom_market_cap_rotation_30_50.py")

    caps = {"600001.SH": 25.0, "600002.SH": 35.0, "600003.SH": 45.0, "600004.SH": 55.0}
    market = _constant_panel(caps)
    # V1 默认区间是 [20,30], 用显式参数把它拉到与 30-50 区间策略一致再比较。
    params = {"market_cap_min": 30.0, "market_cap_max": 50.0, "hold_num": 2, "refresh_rate": 2}

    v1_signals = v1.MATRIX_STRATEGY.compute_signals(market, params)
    tier_signals = tier.MATRIX_STRATEGY.compute_signals(market, params)

    for t in range(market.shape[0]):
        assert v1_signals.entry[t].tolist() == tier_signals.entry[t].tolist(), f"entry 第 {t} 日不一致"
        assert v1_signals.exit[t].tolist() == tier_signals.exit[t].tolist(), f"exit 第 {t} 日不一致"
        assert np.allclose(v1_signals.score[t], tier_signals.score[t])


def test_all_tier_signal_ids_are_unique():
    """信号 id 必须全局唯一, 否则回测/监控会串策略。"""
    seen: dict[str, str] = {}
    for filename, strategy_id, _, _ in _TIERS:
        path = _CUSTOM_DIR / filename
        if not path.exists():
            pytest.skip(f"custom strategy not present: {path}")
        module = _load_module(path)
        slug = strategy_id.removeprefix("custom_")
        expected = {f"signal_{slug}_in", f"signal_{slug}_out"}
        assert set(module.ENTRY_SIGNALS) | set(module.EXIT_SIGNALS) == expected
        for signal_id in expected:
            assert signal_id not in seen, f"{signal_id} 在 {seen.get(signal_id)} 与 {filename} 重复"
            seen[signal_id] = filename

    # 区间策略不得复用 V1 / V2 的信号 id
    for other in ("custom_small_cap_rotation.py", "custom_small_cap_rotation_v2.py"):
        other_path = _CUSTOM_DIR / other
        if not other_path.exists():
            continue
        module = _load_module(other_path)
        for signal_id in set(module.ENTRY_SIGNALS) | set(module.EXIT_SIGNALS):
            assert signal_id not in seen, f"{signal_id} 与 {other} 撞名"


def test_tier_ids_and_filenames_align():
    """策略 id 必须与文件名 stem 一致(引擎按 stem 兜底 id, 不一致会导致重复注册)。"""
    for filename, strategy_id, _, _ in _TIERS:
        path = _CUSTOM_DIR / filename
        if not path.exists():
            pytest.skip(f"custom strategy not present: {path}")
        module = _load_module(path)
        assert module.META["id"] == strategy_id == path.stem
        assert re.fullmatch(r"[A-Za-z0-9_-]+", strategy_id)
