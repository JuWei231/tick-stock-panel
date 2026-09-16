"""人气排行 / 资金流向 两个内置预设 + 内置榜单菜单的契约测试。

背景: 上游 shy313 的 4 个导出接口中, 概念(ths-concepts)/行业(ths-industries) 早已接入;
本次新增人气排行(popularity)/资金流向(money-flow), 并为这两个「没有内置页面」的数据集
提供内置榜单菜单。

本文件锁定的关键口径 (CONTRIBUTING §3.1 单位契约):
  - 接口 change_pct 是【百分数值】(4.03 = +4.03%), 展平时不得二次换算;
  - net/inflow/outflow 单位元, 且 net == inflow - outflow;
  - 接口对停牌等标的返回 null, 展平必须保留空值, 不得补零 —— 补零会把
    「无数据」伪装成「0 净流入」, 属于会误导交易的错误金融结果。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import polars as pl
import pytest

from app.api.analysis import _default_menus
from app.services.ext_presets import (
    MONEY_FLOW_PRESET_ID,
    POPULARITY_PRESET_ID,
    _flatten_money_flow_rows,
    _flatten_popularity_rows,
    _money_flow_preset,
    _popularity_preset,
    _presets,
    get_preset,
    preset_flatten,
)

_POPULARITY_RAW = [
    {"rank": 1, "symbol": "000636.SZ", "name": "风华高科", "heat": 166401.0, "change_pct": 10.0},
    {"rank": 3, "symbol": "000823.SZ", "name": "超声电子", "heat": 109125.3, "change_pct": -3.4869240348692325},
    # 停牌/无行情: 接口返回 null change_pct
    {"rank": 42, "symbol": "600000.SH", "name": "浦发银行", "heat": 8.1, "change_pct": None},
    # 缺 symbol 的行必须被丢弃
    {"rank": 99, "symbol": "", "name": "无代码", "heat": 1.0, "change_pct": 1.0},
]

_MONEY_FLOW_RAW = [
    {"symbol": "300308.SZ", "name": "中际旭创", "net": 3685114043.12,
     "inflow": 16829593136.69, "outflow": 13144479093.57, "rank": 1, "change_pct": 4.033254690484212},
    # 停牌: 三项资金字段均为 null
    {"symbol": "600001.SH", "name": "停牌股", "net": None, "inflow": None,
     "outflow": None, "rank": 500, "change_pct": None},
]


# ---------------------------------------------------------------------------
# 预设定义
# ---------------------------------------------------------------------------

def test_new_presets_are_registered() -> None:
    ids = [p.id for p in _presets()]
    assert POPULARITY_PRESET_ID in ids
    assert MONEY_FLOW_PRESET_ID in ids
    assert len(ids) == len(set(ids)), "预设 id 必须唯一"


@pytest.mark.parametrize(
    "preset,expected_url",
    [
        (_popularity_preset(), "https://shy313.com/api/plugins/market_flow/exports/popularity"),
        (_money_flow_preset(), "https://shy313.com/api/plugins/market_flow/exports/money-flow"),
    ],
)
def test_new_presets_definition(preset, expected_url) -> None:
    assert preset.mode == "snapshot"
    assert preset.pull is not None
    assert preset.pull.url == expected_url
    assert preset.pull.method == "GET"
    # 出厂禁用: 启动不得自动拉取 (#199)
    assert preset.pull.enabled is False
    # 与概念/行业同构: symbol 走 股票代码 列, code 由 symbol 派生
    assert preset.symbol_map == {"type": "mapped", "col": "股票代码"}
    assert preset.code_map == {"type": "computed", "from": "symbol", "method": "strip_exchange"}


def test_get_preset_resolves_new_ids() -> None:
    assert get_preset(POPULARITY_PRESET_ID) is not None
    assert get_preset(MONEY_FLOW_PRESET_ID) is not None
    assert get_preset("ext_not_a_preset") is None


def test_preset_flatten_registry_covers_every_preset() -> None:
    """每个预设都必须登记行转换; 漏登记会让拉取写入原始字段名, 静默破坏 schema。"""
    for preset in _presets():
        assert preset_flatten(preset.id) is not None, f"{preset.id} 未登记 flatten"
    assert preset_flatten("ext_not_a_preset") is None


# ---------------------------------------------------------------------------
# 展平: 单位与空值口径
# ---------------------------------------------------------------------------

def test_popularity_flatten_units_and_nulls() -> None:
    rows = _flatten_popularity_rows(_POPULARITY_RAW)

    # 缺 symbol 的行被丢弃
    assert len(rows) == 3
    assert [r["symbol"] for r in rows] == ["000636.SZ", "000823.SZ", "600000.SH"]

    first = rows[0]
    assert first["股票代码"] == "000636.SZ"
    assert first["code"] == "000636"
    assert first["股票简称"] == "风华高科"
    assert first["人气排名"] == 1
    assert isinstance(first["人气排名"], int)
    assert first["热度"] == 166401.0

    # 百分数值原样透传, 不做 100 倍换算
    assert first["涨跌幅"] == 10.0
    assert rows[1]["涨跌幅"] == pytest.approx(-3.4869240348692325)

    # null 必须保留为 null, 不得补零
    assert rows[2]["涨跌幅"] is None


def test_money_flow_flatten_units_and_nulls() -> None:
    rows = _flatten_money_flow_rows(_MONEY_FLOW_RAW)
    assert len(rows) == 2

    first = rows[0]
    assert first["code"] == "300308"
    assert first["资金排名"] == 1
    assert first["净流入"] == pytest.approx(3685114043.12)
    assert first["流入"] == pytest.approx(16829593136.69)
    assert first["流出"] == pytest.approx(13144479093.57)
    # 百分数值透传
    assert first["涨跌幅"] == pytest.approx(4.033254690484212)

    # 停牌标的: 资金三项与涨跌幅全部保留为 null
    halted = rows[1]
    for field in ("净流入", "流入", "流出", "涨跌幅"):
        assert halted[field] is None, f"{field} 被补零, 会把「无数据」伪装成「0 流入」"


def test_money_flow_net_identity_is_preserved() -> None:
    """net == inflow - outflow 必须原样保留 (接口口径, 展平不得引入误差)。"""
    rows = _flatten_money_flow_rows(_MONEY_FLOW_RAW)
    first = rows[0]
    assert first["净流入"] == pytest.approx(first["流入"] - first["流出"], abs=0.01)


def test_flatten_coerces_string_numbers_and_rejects_garbage() -> None:
    """上游可能给字符串数字; 非数字文本必须归为 None 而不是抛错。"""
    rows = _flatten_popularity_rows([
        {"rank": "7", "symbol": "000001.SZ", "name": "平安银行", "heat": "12.5", "change_pct": "abc"},
        {"rank": None, "symbol": "000002.SZ", "name": "万科A", "heat": None, "change_pct": float("nan")},
    ])
    assert rows[0]["人气排名"] == 7
    assert rows[0]["热度"] == 12.5
    assert rows[0]["涨跌幅"] is None
    assert rows[1]["人气排名"] is None
    assert rows[1]["热度"] is None
    assert rows[1]["涨跌幅"] is None


# ---------------------------------------------------------------------------
# 内置榜单菜单 ←→ 预设 schema 的契约
# ---------------------------------------------------------------------------

def _menu(menu_id: str):
    for m in _default_menus(None):  # type: ignore[arg-type]
        if m.id == menu_id:
            return m
    raise AssertionError(f"内置菜单 {menu_id} 不存在")


def test_default_menus_declare_ranking_menus() -> None:
    menus = _default_menus(None)  # type: ignore[arg-type]
    by_id = {m.id: m for m in menus}
    assert {"hot_rank", "money_flow"} <= set(by_id)
    assert len(by_id) == len(menus), "内置菜单 id 必须唯一"

    for menu_id, source, rank_field in (
        ("hot_rank", POPULARITY_PRESET_ID, "人气排名"),
        ("money_flow", MONEY_FLOW_PRESET_ID, "资金排名"),
    ):
        menu = by_id[menu_id]
        assert menu.template == "ranking"
        assert menu.data_source == source
        assert menu.rank_field == rank_field
        assert menu.visible is True
        assert menu.builtin is True, "内置菜单不可被当作普通用户菜单删除"
        # 排名数字小=靠前, 默认按排名升序
        assert menu.default_sort is not None
        assert menu.default_sort.field == rank_field
        assert menu.default_sort.order == "asc"


@pytest.mark.parametrize(
    "menu_id,preset",
    [("hot_rank", _popularity_preset()), ("money_flow", _money_flow_preset())],
)
def test_menu_columns_exist_in_preset_schema(menu_id, preset) -> None:
    """菜单声明的每个列字段都必须真实存在于对应预设 schema, 否则页面恒显 0 列/空列。"""
    menu = _menu(menu_id)
    field_names = {f.name for f in preset.fields}
    assert menu.data_source == preset.id

    assert menu.rank_field is not None
    assert menu.rank_field in field_names, f"{menu_id} 的排名字段不在 preset schema 中"

    declared = {c.field for c in menu.detail_columns}
    # 页面主键列由 columnsFromFields 兜底, 明细列必须逐一落在 schema 内
    assert declared <= field_names, f"{menu_id} 声明了 schema 中不存在的列: {declared - field_names}"
    assert declared, f"{menu_id} 没有任何明细列"

    # 单位口径可见性: 百分比列必须是 percent 类型, 金额列必须带单位标注
    pct_cols = [c for c in menu.detail_columns if c.field == "涨跌幅"]
    assert pct_cols and pct_cols[0].type == "percent", "涨跌幅必须按 percent 渲染"


def test_menu_ids_do_not_collide_with_builtin_pages() -> None:
    """内置菜单 id 不能与扩展现有路由/菜单重复 (导航去重的前提)。"""
    ids = {m.id for m in _default_menus(None)}  # type: ignore[arg-type]
    # 概念/行业有内置页面兜底, 不应再出现在自动菜单里
    assert "concept" not in ids
    assert "industry" not in ids


# ---------------------------------------------------------------------------
# 端到端: fetch_preset 落盘 schema (含 polars 空值 cast)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "config_id,raw,expected_rows,rank_field,float_field",
    [
        (POPULARITY_PRESET_ID, _POPULARITY_RAW, 3, "人气排名", "热度"),
        (MONEY_FLOW_PRESET_ID, _MONEY_FLOW_RAW, 2, "资金排名", "净流入"),
    ],
)
def test_fetch_preset_writes_schema_with_nulls_preserved(
    tmp_path: Path, monkeypatch, config_id, raw, expected_rows, rank_field, float_field
) -> None:
    """fetch_preset → rows_to_parquet 全链路: 列类型正确, 空值不被 cast 成 0。

    这里同时覆盖 polars 的隐式 cast: 全为 null 的数值列若 cast 失败会抛错,
    含 null 的列若被填充 0 则会把「停牌」误报为「0 净流入」。
    """
    import asyncio

    from app.services import ext_presets
    from app.services.ext_data import ExtConfigStore

    async def _fake_fetch_json(url: str) -> list[dict]:
        return raw

    monkeypatch.setattr(ext_presets, "_fetch_json", _fake_fetch_json)

    written = asyncio.run(ext_presets.fetch_preset(config_id, tmp_path))
    assert written == expected_rows

    config = ExtConfigStore(tmp_path).get(config_id)
    assert config is not None
    parquet = tmp_path / "ext_data" / config_id / "part.parquet"
    assert parquet.exists(), "拉取未落盘"

    df = pl.read_parquet(parquet)
    assert df.height == expected_rows
    for column in ("symbol", "code", "股票代码", "股票简称", rank_field, float_field, "涨跌幅"):
        assert column in df.columns, f"落盘缺少列 {column}"

    # 排名必须是整型 (前端按 number/precision=0 渲染)
    assert df.schema[rank_field] in (pl.Int64, pl.Int32)
    # 金额/热度必须是浮点
    assert df.schema[float_field] == pl.Float64

    # 停牌行的空值必须原样保留
    halted = df.filter(pl.col("symbol") == ("600000.SH" if config_id == POPULARITY_PRESET_ID else "600001.SH"))
    assert halted.height == 1
    assert halted["涨跌幅"][0] is None
    if config_id == MONEY_FLOW_PRESET_ID:
        assert halted["净流入"][0] is None
        assert halted["流入"][0] is None
        assert halted["流出"][0] is None


def test_fetch_preset_rejects_unknown_id(tmp_path: Path) -> None:
    import asyncio

    from app.services.ext_presets import fetch_preset

    with pytest.raises(ValueError, match="未知的内置预设"):
        asyncio.run(fetch_preset("ext_not_a_preset", tmp_path))


def test_apply_preset_flatten_routes_new_ids() -> None:
    """ext_pull 的拉取路径必须对两个新预设套用同一套结构转换 (手动/定时拉取同口径)。"""
    from app.services.ext_pull import _apply_preset_flatten

    popularity = _apply_preset_flatten(POPULARITY_PRESET_ID, list(_POPULARITY_RAW))
    assert "人气排名" in popularity[0]

    money_flow = _apply_preset_flatten(MONEY_FLOW_PRESET_ID, list(_MONEY_FLOW_RAW))
    assert "净流入" in money_flow[0]

    # 非预设 id 原样返回
    passthrough = [{"raw": 1}]
    assert _apply_preset_flatten("ext_user_custom", passthrough) == passthrough


# ---------------------------------------------------------------------------
# 上游抖动重试 (实测 502 连续出现, 稍后重试即成功)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.request = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self  # type: ignore[arg-type]
            )


class _FakeClient:
    """按脚本依次返回响应或抛异常, 记录调用次数。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    async def get(self, url, headers=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _run_retry(script: list):
    import asyncio

    from app.services import ext_presets

    client = _FakeClient(script)
    original = ext_presets._FETCH_BACKOFF_SECONDS
    ext_presets._FETCH_BACKOFF_SECONDS = 0  # 测试不睡真实退避
    try:
        result = asyncio.run(ext_presets._get_with_retry(client, "https://example.test/x", {}))
    finally:
        ext_presets._FETCH_BACKOFF_SECONDS = original
    return result, client


def test_retry_recovers_from_transient_5xx() -> None:
    """连续 502 后成功: 必须重试并返回成功响应 (实测上游就是这个形态)。"""
    resp, client = _run_retry([_FakeResponse(502), _FakeResponse(502), _FakeResponse(200)])
    assert resp.status_code == 200
    assert client.calls == 3


def test_retry_recovers_from_transport_error() -> None:
    import httpx

    resp, client = _run_retry([httpx.ConnectError("boom"), _FakeResponse(200)])
    assert resp.status_code == 200
    assert client.calls == 2


def test_retry_does_not_retry_4xx() -> None:
    """4xx 是请求本身的问题, 重试无意义且会放大上游压力。"""
    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        _run_retry([_FakeResponse(404), _FakeResponse(200)])


def test_retry_gives_up_after_bounded_attempts() -> None:
    """持续 5xx 必须有限重试后失败, 不能无限打上游。"""
    import httpx

    from app.services import ext_presets

    attempts = ext_presets._FETCH_ATTEMPTS
    client = _FakeClient([_FakeResponse(503)] * attempts + [_FakeResponse(200)])

    original = ext_presets._FETCH_BACKOFF_SECONDS
    ext_presets._FETCH_BACKOFF_SECONDS = 0
    try:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(ext_presets._get_with_retry(client, "https://example.test/x", {}))
    finally:
        ext_presets._FETCH_BACKOFF_SECONDS = original

    assert client.calls == attempts, "重试次数必须封顶, 不得继续打上游"
