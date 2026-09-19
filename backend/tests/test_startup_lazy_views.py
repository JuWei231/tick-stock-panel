"""启动路径优化回归: 惰性视图注册 + enriched 分区裁剪。

对应改动:
- `DataStore` 启动只登记视图定义, 视图在首次被查询引用时才 CREATE (原先启动时
  对 20+ 张 parquet 视图逐个建, 要 glob 数据目录并推断 schema, 是启动期最重的
  一步)。
- `_refresh_enriched_impl` 的历史读取只枚举 date >= start 的分区文件, 不再让
  Polars 扫全部 3337 个日期分区。

两者都必须保持原有查询语义: 视图最终能建出来且能查到数据; 分区裁剪不能漏读。
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import polars as pl
import pytest

from app.parquet import ENRICHED_STORAGE_SCHEMA
from app.tickflow.repository import (
    DataStore,
    KlineRepository,
    enriched_files_since,
)


def _write_enriched_partition(data_dir, day: date, symbols: list[str]) -> None:
    out = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "symbol": symbol,
            "date": day,
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 100.0, "amount": 1000.0,
            "raw_close": 10.5, "raw_high": 11.0, "raw_low": 9.0,
            "turnover_rate": 1.0,
            "consecutive_limit_ups": 0, "consecutive_limit_downs": 0,
            "quote_ts": 0,
        }
        for symbol in symbols
    ]
    pl.DataFrame(rows, schema=ENRICHED_STORAGE_SCHEMA).write_parquet(out)


def _real_view_names(store: DataStore) -> set[str]:
    rows = store._connection.execute(  # noqa: SLF001
        "select table_name from information_schema.tables where table_schema = 'main'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def test_views_are_registered_but_not_created_at_construction(tmp_path) -> None:
    """构造 DataStore 不应真的建视图 (那会枚举整个数据目录)。"""
    _write_enriched_partition(tmp_path, date(2026, 1, 5), ["600000.SH"])
    store = DataStore(data_dir=tmp_path)

    assert "kline_enriched" in store._view_sql  # noqa: SLF001
    assert _real_view_names(store) == set()  # 启动时一张都没建


def test_view_is_created_on_first_query_and_returns_data(tmp_path) -> None:
    _write_enriched_partition(tmp_path, date(2026, 1, 5), ["600000.SH", "000001.SZ"])
    repo = KlineRepository(DataStore(data_dir=tmp_path))

    # execute_one 走游标路径, 同样必须触发按需建视图
    assert repo.execute_one("SELECT count(*) FROM kline_enriched")[0] == 2
    # 直连连接路径
    assert repo.db.execute("SELECT count(*) FROM kline_enriched").fetchone()[0] == 2


def test_missing_view_still_raises_catalog_error(tmp_path) -> None:
    """建不出视图 (无分区文件) 时保持与改动前一致的报错, 不静默返回空。"""
    repo = KlineRepository(DataStore(data_dir=tmp_path))

    with pytest.raises(duckdb.CatalogException):
        repo.execute_one("SELECT count(*) FROM kline_enriched")


def test_rebuild_views_recreates_registered_views(tmp_path) -> None:
    """rebuild_views 覆盖 DataStore.view_paths() 里的全部视图。

    原先 rebuild_views 内联了第二份清单 (只 13 张, 漏了 instruments_ext / kline_ext /
    financials_* / depth5 等), 与 DataStore 启动注册的 21 张漂移。
    """
    _write_enriched_partition(tmp_path, date(2026, 1, 5), ["600000.SH"])
    repo = KlineRepository(DataStore(data_dir=tmp_path))

    repo.rebuild_views()

    assert repo.execute_one("SELECT count(*) FROM kline_enriched")[0] == 1
    assert "depth5" in repo.store.view_paths()  # 第二份清单曾漏掉的视图
    assert set(repo.store.view_paths()) >= {"kline_enriched", "depth5", "instruments_ext"}


def test_enriched_files_since_prunes_old_partitions(tmp_path) -> None:
    enriched_dir = tmp_path / "kline_daily_enriched"
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(5)]
    for day in days:
        _write_enriched_partition(tmp_path, day, ["600000.SH"])

    files = enriched_files_since(enriched_dir, days[2])

    assert len(files) == 3
    assert all(day.isoformat() in " ".join(files) for day in days[2:])


def test_enriched_files_since_falls_back_on_unknown_layout(tmp_path) -> None:
    """历史 symbol= 分区等未知布局必须返回空, 让调用方回退全量 glob 而不是漏读。"""
    enriched_dir = tmp_path / "kline_daily_enriched"
    (enriched_dir / "symbol=600000.SH").mkdir(parents=True)
    (enriched_dir / "symbol=600000.SH" / "part.parquet").write_bytes(b"")

    assert enriched_files_since(enriched_dir, date(2026, 1, 1)) == []


def test_enriched_files_since_empty_dir(tmp_path) -> None:
    assert enriched_files_since(tmp_path / "kline_daily_enriched", date(2026, 1, 1)) == []
