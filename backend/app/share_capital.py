"""历史股本解析。

财务股本按公告日可用, 历史缺失时回退 instruments 最新股本快照。

两条路径同一口径:
- ``apply_historical_shares``  — polars 面板 (screener / polars_expr 回测);
- ``build_share_matrices``     — MarketDataMatrix 字段 (matrix_native 回测)。

本地 ``financials/shares`` 未同步时两者均为 no-op, 行为退回 instruments 快照。
此时 ``market_cap_expr`` 退化为 ``raw_close * 最新股本``: 精确口径是 ``raw_close * 当日股本``,
缺历史股本时含送转标的的历史市值会被**高估**(送转越大越严重), 因此该退化会打一次 WARNING
(见 ``_warn_share_history_unusable``), 不再静默。
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)

# 缺表/缺列只告警一次: 这些函数位于回测、选股、富化的热路径上, 每次调用都刷屏只会让人忽略它。
_share_history_warned: set[str] = set()
_raw_close_warned: set[str] = set()
_market_cap_skipped_warned: set[str] = set()

# 股本列。total_shares 算总市值, float_shares 算换手率与流通市值。
SHARE_COLUMNS: tuple[str, ...] = ("float_shares", "total_shares")
# 市值必须用不复权原始价: close 是前复权, 历史值含未来除权。
VALUATION_PRICE = "raw_close"


def share_history_path(data_dir: Path) -> Path:
    """历史股本表位置 (供调用方与诊断复用, 避免各处硬编码路径)。"""
    return Path(data_dir) / "financials" / "shares" / "part.parquet"


def _warn_share_history_unusable(data_dir: Path, reason: str) -> None:
    """股本表缺失/损坏时显式告警 —— 否则市值口径静默退化, 外部没有任何信号。"""
    key = str(data_dir)
    if key in _share_history_warned:
        return
    _share_history_warned.add(key)
    logger.warning(
        "历史股本表不可用(%s): %s。按公告日还原股本将整体失效, 市值/流通市值/换手率"
        "全部退回 instruments 最新快照; 而市值价格用的是不复权 %s, 这个组合会让含送转标的"
        "的历史市值被系统性高估(送转幅度越大、窗口越长越严重), 回测与市值筛选结果不可信。"
        "修: 用含 FINANCIAL 权限的数据源执行 POST /api/financials/sync/shares, 生成该表后重启。",
        reason,
        share_history_path(data_dir),
        VALUATION_PRICE,
    )


def warn_missing_valuation_price(context: str) -> None:
    """有市值/规模口径需求却缺不复权价时告警一次(不静默换价)。

    `close` 是前复权价: 拿它当市值价格会把复权比重复计入, 且各股复权比不同, 除了
    幅度偏差还会改变截面排序。因此缺 `raw_close` 时只能显式告警或 fail-closed。
    """
    if context in _raw_close_warned:
        return
    _raw_close_warned.add(context)
    logger.warning(
        "缺 %s 列(%s): close 是前复权价, 与当日股本相乘会把复权比重复计入, 历史市值/"
        "规模会系统性偏小。修: 用带 %s 的 enriched 数据。",
        VALUATION_PRICE,
        context,
        VALUATION_PRICE,
    )


def warn_market_cap_unavailable(context: str, shares_col: str) -> None:
    """配置了市值约束、但该帧没有股本列时告警一次 —— 否则约束被整条丢弃且无任何信号。"""
    key = f"{context}:{shares_col}"
    if key in _market_cap_skipped_warned:
        return
    _market_cap_skipped_warned.add(key)
    logger.warning(
        "市值约束被静默跳过(%s): 帧里没有 %s 列, market_cap_expr 返回 None, 调用方会当作"
        "'该项未配置'处理, 等于不加任何市值约束。常见原因: 该帧未关联 instruments 股本, "
        "或属无股本概念的非股票资产(ETF/指数)。",
        context,
        shares_col,
    )


def market_cap_expr(df: pl.DataFrame, shares_col: str) -> pl.Expr | None:
    """点时市值 = raw_close * 股本; 缺列时返回 None, 调用方跳过该项过滤。

    注意: 只有股本列存在而 raw_close 缺失时才告警 —— 股本列本身缺失是合法跳过
    (ETF/指数无股本概念), 但"有股本却没原始价"会让市值约束凭空消失。
    """
    if shares_col not in df.columns:
        return None
    if VALUATION_PRICE not in df.columns:
        warn_missing_valuation_price(f"market_cap_expr[{shares_col}]")
        return None
    return pl.col(VALUATION_PRICE) * pl.col(shares_col)


def attach_market_cap(
    rows: pl.DataFrame,
    instruments: pl.DataFrame | None,
    shares: pl.DataFrame | None,
    *,
    today: date,
) -> pl.DataFrame:
    """给行情行补齐"当日股本 + 总/流通市值"列(展示与筛选共用的唯一口径)。

    数据边界一次性算好, 避免各页面各接口各自用 `close`/instruments 快照相乘:
    - 以 instruments 最新快照为基准(**丢弃行内已有股本列**, 防止对已换算过的值二次乘
      复权比), 再按公告/生效日 asof 覆盖为历史股本; 无股本表时退化为 `快照 x close/raw_close`;
    - 最后按 ``market_cap_expr`` 计算 `market_cap`/`float_market_cap`(元)。

    非股票标的(无 instruments 股本)与缺 `raw_close` 的帧原样返回(后者会告警一次)。
    """
    if rows.is_empty() or not {"symbol", "date"} <= set(rows.columns):
        return rows
    if instruments is None or instruments.is_empty():
        return rows
    wanted = [c for c in SHARE_COLUMNS if c in instruments.columns]
    if not wanted:
        return rows
    symbols = rows["symbol"].unique().to_list()
    snapshot = instruments.filter(pl.col("symbol").is_in(symbols)).select(["symbol", *wanted])
    if snapshot.is_empty():
        return rows

    base = rows.drop([c for c in wanted if c in rows.columns])
    base = base.join(snapshot.unique(subset=["symbol"]), on="symbol", how="left")
    resolved = apply_historical_shares(
        base, shares, today=today, derive_snapshot_fallback=True
    )
    total = market_cap_expr(resolved, "total_shares")
    float_cap = market_cap_expr(resolved, "float_shares")
    if total is None and float_cap is None:
        return resolved
    return resolved.with_columns([
        expr.alias(name)
        for expr, name in ((total, "market_cap"), (float_cap, "float_market_cap"))
        if expr is not None
    ])


def load_share_history(data_dir: Path) -> pl.DataFrame:
    """读取本地财务股本表; 未同步或损坏时返回空表 (并告警一次)。"""
    path = share_history_path(data_dir)
    if not path.exists():
        _warn_share_history_unusable(data_dir, "文件不存在")
        return pl.DataFrame()
    try:
        shares = pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        _warn_share_history_unusable(data_dir, f"读取失败: {exc}")
        return pl.DataFrame()
    if not {"symbol", "period_end"} <= set(shares.columns):
        _warn_share_history_unusable(data_dir, "缺 symbol/period_end 列")
        return pl.DataFrame()
    if not set(SHARE_COLUMNS) & set(shares.columns):
        _warn_share_history_unusable(data_dir, "无 float_shares/total_shares 列")
        return pl.DataFrame()
    return shares


def _resolvable_columns(
    rows_columns: set[str],
    shares: pl.DataFrame,
    columns: tuple[str, ...],
) -> list[str]:
    """两侧都有的股本列才能替换; 缺一侧只能沿用快照。"""
    return [
        name
        for name in columns
        if name in rows_columns and name in shares.columns
    ]


def _share_history(shares: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """归一化股本表为 (symbol, 生效日, 各股本列), 按生效日去重排序。"""
    def as_date_expr(column: str) -> pl.Expr:
        dtype = shares.schema[column]
        if dtype == pl.Utf8:
            return pl.col(column).str.to_date(strict=False)
        return pl.col(column).cast(pl.Date, strict=False)

    available_date = as_date_expr("period_end")
    if "announce_date" in shares.columns:
        available_date = as_date_expr("announce_date").fill_null(available_date)

    # 非正股本视为缺失, 让下游 coalesce 回退快照, 而不是整行丢弃 —— 同一行
    # 可能只有一列可用 (例如推算出的总股本没有配套流通股本)。
    values = [
        pl.when(pl.col(name).cast(pl.Float64, strict=False) > 0)
        .then(pl.col(name).cast(pl.Float64, strict=False))
        .otherwise(None)
        .alias(f"_historical_{name}")
        for name in columns
    ]
    usable = pl.any_horizontal([pl.col(f"_historical_{name}").is_not_null() for name in columns])

    return (
        shares
        .select(
            pl.col("symbol").cast(pl.Utf8),
            available_date.alias("_share_available_date"),
            pl.col("period_end").cast(pl.Utf8).alias("_share_period_end"),
            *values,
        )
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("_share_available_date").is_not_null()
            & usable
        )
        .sort(["symbol", "_share_available_date", "_share_period_end"])
        .unique(subset=["symbol", "_share_available_date"], keep="last")
        .sort(["symbol", "_share_available_date"])
    )


def derive_snapshot_shares(
    rows: pl.DataFrame,
    columns: tuple[str, ...] = SHARE_COLUMNS,
) -> pl.DataFrame:
    """把 instruments 最新快照股本换算成"交易日当日股本"的推定值。

    平台的前复权口径是 ``close = raw_close * ratio`` (``ratio = cum(<=t)/total``);
    而一次送转在把价格按 ``1/k`` 缩小的同时把股本放大 ``k`` 倍, 所以当日股本的推定值
    就是 ``快照股本 * ratio`` = ``快照股本 * close / raw_close``。

    这是**没有财务股本表时的最优估计**: 代入 ``raw_close * 股本`` 后恰好退化成
    ``close * 快照股本``, 送转除权日市值连续; 现金分红不改变股本, 复权比却含分红,
    因此会略微低估(量级是分红率), 远小于送转带来的偏差。

    有真实公告股本时调用方应在其之上覆盖(见 ``apply_historical_shares``)。
    """
    if rows.is_empty() or "close" not in rows.columns or VALUATION_PRICE not in rows.columns:
        return rows
    targets = [name for name in columns if name in rows.columns]
    if not targets:
        return rows
    close = pl.col("close").cast(pl.Float64, strict=False)
    raw = pl.col(VALUATION_PRICE).cast(pl.Float64, strict=False)
    # 停牌/缺价 (close 或 raw 非正) 时 ratio 不可用, 保持快照值, 不要让缺数据放大或清零股本。
    usable = close.is_not_null() & raw.is_not_null() & (close > 0) & (raw > 0)
    return rows.with_columns([
        pl.when(usable)
        .then(pl.col(name).cast(pl.Float64, strict=False) * close / raw)
        .otherwise(pl.col(name).cast(pl.Float64, strict=False))
        .alias(name)
        for name in targets
    ])


def apply_historical_shares(
    rows: pl.DataFrame,
    shares: pl.DataFrame | None,
    *,
    today: date,
    columns: tuple[str, ...] = SHARE_COLUMNS,
    derive_snapshot_fallback: bool = False,
) -> pl.DataFrame:
    """为行情行解析有效股本。

    当日保留 rows 里的快照值; 历史日期使用公告日不晚于交易日的最新股本,
    找不到历史记录时继续使用快照值。

    ``derive_snapshot_fallback=True`` 时, 先把快照股本按复权比换算成当日股本的推定值
    (``derive_snapshot_shares``), 真实公告股本再覆盖其上。**市值口径需要这个开关**:
    ``market_cap_expr`` 用的是不复权 ``raw_close``, 股本若停留在最新快照, 含送转标的的
    历史市值会被系统性高估。默认关闭, 是为了不影响已落盘的 enriched 换手率
    (``indicators/pipeline.py`` 的换手率路径), 避免静默作废历史分区。
    """
    if rows.is_empty() or not {"symbol", "date"} <= set(rows.columns):
        return rows

    if derive_snapshot_fallback:
        rows = derive_snapshot_shares(rows, columns)

    if shares is None or shares.is_empty() or "period_end" not in shares.columns:
        return rows

    targets = _resolvable_columns(set(rows.columns), shares, columns)
    if not targets:
        return rows

    history = _share_history(shares, targets)
    if history.is_empty():
        return rows

    resolved = (
        rows
        .with_row_index("_share_row_order")
        .with_columns(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("date").cast(pl.Date, strict=False).alias("_share_trade_date"),
        )
        .sort(["symbol", "_share_trade_date"])
        .join_asof(
            history,
            left_on="_share_trade_date",
            right_on="_share_available_date",
            by="symbol",
            strategy="backward",
            check_sortedness=False,
        )
        .with_columns([
            pl.when(pl.col("_share_trade_date") == pl.lit(today))
            .then(pl.col(name))
            .otherwise(pl.coalesce(f"_historical_{name}", name))
            .alias(name)
            for name in targets
        ])
        .sort("_share_row_order")
    )
    return resolved.drop(
        "_share_row_order",
        "_share_trade_date",
        "_share_available_date",
        "_share_period_end",
        *[f"_historical_{name}" for name in targets],
    )


def _matrix_pit_ratio(market: Any) -> np.ndarray:
    """矩阵的 close/raw_close —— 前复权比, 即"当日股本 / 最新股本"的推定比例。

    缺 raw_close 时取 1.0, 与 matrix 里 raw_close 缺省回退 close 的口径一致
    (此时推定等于快照, 不引入额外偏差)。
    """
    shape = market.shape
    close = np.asarray(market.close, dtype=np.float64)
    raw = market.fields.get(VALUATION_PRICE)
    raw = close if raw is None else np.broadcast_to(np.asarray(raw, dtype=np.float64), shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = close / raw
    ok = np.isfinite(ratio) & (ratio > 0) & (raw > 0)
    return np.where(ok, ratio, 1.0)


def build_share_matrices(
    market: Any,
    shares: pl.DataFrame | None,
    names: list[str],
    *,
    today: date,
) -> dict[str, np.ndarray]:
    """为 MarketDataMatrix 构建逐日股本 TxN float32 字段。

    基底是"快照股本 x close/raw_close"的推定当日股本 (见 ``derive_snapshot_shares``),
    真实公告股本表可用时再按公告日覆盖其上。**没有表时也返回推定基底** —— 否则
    ``raw_close * 快照股本`` 会让含送转标的历史市值被系统性高估。

    推定结果与快照完全一致 (无复权且无表) 时返回 {}, 不无谓地把字段 materialize 成 TxN。
    """
    requested = [str(name) for name in names if str(name) in SHARE_COLUMNS]
    fields = getattr(market, "fields", None)
    if not requested or fields is None:
        return {}  # 不是矩阵 (无字段映射): 无可附加
    targets = [name for name in requested if name in fields]
    if not targets:
        return {}

    shape = market.shape
    asset_index = {symbol: index for index, symbol in enumerate(market.symbols)}
    label_dates = np.array(
        [label[:10] for label in market.timestamp_labels], dtype="datetime64[D]"
    )
    ratios = _matrix_pit_ratio(market)

    # instruments 快照: 推定基底的来源, 也是当日的权威值。
    snapshots = {
        name: np.broadcast_to(market.field(name), shape).astype(np.float32)
        for name in targets
    }
    result = {
        name: (values.astype(np.float64) * ratios).astype(np.float32)
        for name, values in snapshots.items()
    }

    history_applied = False
    if shares is not None and not shares.is_empty() and "period_end" in shares.columns:
        hist_targets = _resolvable_columns(set(market.fields), shares, tuple(targets))
        history = _share_history(shares, hist_targets) if hist_targets else pl.DataFrame()
        if not history.is_empty():
            symbols = history["symbol"].to_list()
            available = history["_share_available_date"].to_numpy().astype("datetime64[D]")
            series = {name: history[f"_historical_{name}"].to_list() for name in hist_targets}
            for row_index, symbol in enumerate(symbols):
                column_index = asset_index.get(symbol)
                if column_index is None:
                    continue
                start = int(np.searchsorted(label_dates, available[row_index], side="left"))
                if start >= shape[0]:
                    continue
                for name in hist_targets:
                    value = series[name][row_index]
                    if value is None:
                        continue
                    result[name][start:, column_index] = float(value)
            history_applied = True

    if not history_applied and np.all(ratios == 1.0):
        return {}  # 推定等于快照: 附加没有意义

    today_rows = np.flatnonzero(label_dates == np.datetime64(today, "D"))
    traded = np.isfinite(market.close)
    for name in targets:
        # 当日以快照为权威 (最新交易日 ratio 恰为 1, 两者本就一致, 这里显式对齐语义)。
        result[name][today_rows] = snapshots[name][today_rows]
        # 与磁盘缓存投影 (_project_matrix_slice) 同一语义: 无行情的格子留 NaN,
        # 免得矩阵是否命中缓存会改变字段取值。
        result[name] = np.where(traded, result[name], np.nan).astype(np.float32)
    return result


def attach_matrix_historical_shares(
    market: Any,
    data_dir: Path | None,
    names: Any = SHARE_COLUMNS,
    *,
    today: date,
) -> Any:
    """把逐日股本作为 TxN matrix fields 附加到 (frozen) MarketDataMatrix 副本。

    原本 total_shares/float_shares 是每标的一个标量的 vector field (instruments
    最新快照), 拿它算历史市值是未来函数。这里提升为逐日矩阵:

    - 无财务股本表: 用 close/raw_close 推定当日股本 (仍远好于快照常数);
    - 有股本表: 按公告日覆盖, 得到精确的历史股本。
    """
    requested = [str(name) for name in names if str(name) in SHARE_COLUMNS]
    if not requested:
        return market
    shares = load_share_history(Path(data_dir)) if data_dir is not None else None
    extra = build_share_matrices(market, shares, requested, today=today)
    if not extra:
        return market
    for array in extra.values():
        array.flags.writeable = False
    return dataclasses.replace(
        market,
        fields=MappingProxyType({**dict(market.fields), **extra}),
        vector_fields=frozenset(market.vector_fields) - set(extra),
    )
