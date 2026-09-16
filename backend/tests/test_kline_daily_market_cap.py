"""个股日K接口的市值/换手口径: 按"不复权价 x 当日股本"逐行下发。

信息条与图表会在任意历史区间显示市值/换手率, 前端若自己用 `close`(前复权)
乘 instruments 快照股本, 含送转标的的历史市值会偏离数倍。这里锁住数据边界口径:
- 历史行 market_cap == raw_close x 公告日口径股本;
- 行内已带(可能被换算过的)股本列时以 instruments 快照重算, 不二次乘复权比;
- ETF 等无股本标的不注入市值列。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline

_SYMBOL = "600000.SH"
_TODAY = date(2026, 9, 16)


def _daily() -> pl.DataFrame:
    # 两行历史日K: 前复权价 = 原始价 x 0.8 (口径可区分)
    closes = [10.0, 12.5]
    return pl.DataFrame({
        "symbol": [_SYMBOL, _SYMBOL],
        "date": [date(2020, 6, 15), date(2024, 1, 2)],
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "raw_close": [c / 0.8 for c in closes],
        "volume": [1_000.0, 2_000.0],
        "amount": [10_000.0, 25_000.0],
        "turnover_rate": [3.0, 4.0],
    })


class _Repo:
    """最小 repo 桩: 只实现 /daily 依赖的方法。"""

    def __init__(self, daily: pl.DataFrame, asset_type: str = "stock") -> None:
        self.daily = daily
        self.asset_type = asset_type

    def resolve_asset_type(self, symbol: str) -> str:
        return self.asset_type

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        return self.daily

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame({
            "symbol": [_SYMBOL],
            "name": ["浦发银行"],
            "total_shares": [2e8],   # 今日快照
            "float_shares": [4e8],
        })

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        return self.get_instruments() if asset_type == "stock" else pl.DataFrame()

    def get_enriched_latest_asset(self, asset_type: str, refresh: bool = True):
        """无实时缓存: 让 /daily 不注入实时蜡烛(与本用例的市值口径无关)。"""
        return pl.DataFrame(), None

    def get_historical_shares(self) -> pl.DataFrame:
        # 公告日口径: 2023-06-01 起总股本 4e8、流通 2e8; 之前 1e8/0.5e8
        return pl.DataFrame({
            "symbol": [_SYMBOL, _SYMBOL],
            "period_end": [date(2019, 1, 1), date(2023, 6, 1)],
            "announce_date": [date(2019, 1, 1), date(2023, 6, 1)],
            "total_shares": [1e8, 4e8],
            "float_shares": [5e7, 2e8],
        })


def _client(repo: _Repo) -> TestClient:
    app = FastAPI()
    app.include_router(kline.router)
    app.state.repo = repo
    app.state.quote_service = None
    app.state.capabilities = None
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fixed_clock_and_plain_json(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_daily_batch_compress", lambda: False)
    monkeypatch.setattr(kline, "cn_today", lambda: _TODAY)


def _rows(repo: _Repo) -> list[dict]:
    resp = _client(repo).get(
        f"/api/kline/daily?symbol={_SYMBOL}&start_date=2019-01-01&end_date={_TODAY}"
    )
    assert resp.status_code == 200
    return resp.json()["rows"]


def test_daily_history_rows_use_pit_shares_and_raw_close() -> None:
    rows = {str(r["date"])[:10]: r for r in _rows(_Repo(_daily()))}

    old = rows["2020-06-15"]
    assert old["total_shares"] == pytest.approx(1e8)          # 公告日口径, 不是快照 2e8
    assert old["market_cap"] == pytest.approx(old["raw_close"] * 1e8)
    # 反例: close x 快照股本(前端旧口径)会明显偏小, 必须能被区分
    assert old["market_cap"] != pytest.approx(old["close"] * 2e8, rel=0.05)

    new = rows["2024-01-02"]
    assert new["total_shares"] == pytest.approx(4e8)
    assert new["market_cap"] == pytest.approx(new["raw_close"] * 4e8)
    assert new["float_market_cap"] == pytest.approx(new["raw_close"] * 2e8)


def test_daily_rows_recompute_from_snapshot_not_from_existing_columns() -> None:
    """行内已有(被换算过的)股本列时, 必须以快照为基准重算, 不能二次乘复权比。"""
    daily = _daily().with_columns([
        # 已被"快照 x close/raw_close"换算过的值: 2e8 x 0.8 = 1.6e8
        (pl.col("close") / pl.col("raw_close") * 2e8).alias("total_shares"),
        (pl.col("close") / pl.col("raw_close") * 4e8).alias("float_shares"),
    ])
    rows = {str(r["date"])[:10]: r for r in _rows(_Repo(daily))}
    assert rows["2020-06-15"]["total_shares"] == pytest.approx(1e8)  # 二次换算会是 0.8e8
    assert rows["2020-06-15"]["market_cap"] == pytest.approx(rows["2020-06-15"]["raw_close"] * 1e8)


def test_daily_rows_without_instruments_keep_original_shape() -> None:
    """无股本概念/无维表的标的照原样返回, 不注入市值列(ETF 走不到股本口径)。"""
    rows = _rows(_Repo(_daily(), asset_type="etf"))
    assert rows and "market_cap" not in rows[0]
