"""内置概念/行业预设的 fuyao 数据源拉取单测 (板块→成分股反转)。

全程 httpx.MockTransport, 不出网。覆盖: 信封解包报错、反转聚合与目录顺序、
单板块失败容忍(重试后跳过)、全部板块失败报错、Key 缺失报错、
fetch_preset 端到端落盘 (config.json + parquet, 复用 flatten 与 schema)。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import polars as pl
import pytest

import app.services.ext_presets as ext_presets
from app.services.ext_presets import (
    _fetch_dimension_rows,
    _fuyao_get_items,
    fetch_preset,
)

_CATALOG = "/api/a-share-index/catalog/ths-index-list"
_CONSTITUENTS = "/api/a-share-index/constituents/ths-stock-list"


def _ok(items: list[dict]) -> httpx.Response:
    return httpx.Response(
        200, json={"code": 0, "message": "success", "data": {"item": items}}
    )


def _mock_transport(
    catalog_by_tag: dict[str, list[dict]],
    constituents_by_code: dict[str, list[dict]],
    fail_codes: tuple[str, ...] = (),
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CATALOG:
            tag = request.url.params.get("tag", "cn_concept")
            return _ok(catalog_by_tag.get(tag, []))
        if request.url.path == _CONSTITUENTS:
            code = request.url.params.get("thscode", "")
            if code in fail_codes:
                raise httpx.ConnectError(f"injected failure: {code}")
            return _ok(constituents_by_code.get(code, []))
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


_CATALOG_CN_CONCEPT = [
    {"thscode": "B1.TI", "name": "白酒概念"},
    {"thscode": "B2.TI", "name": "新能源汽车"},
]
_CONSTITUENTS_BY_CODE = {
    "B1.TI": [
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
        {"thscode": "000858.SZ", "ticker": "000858", "name": "五粮液"},
    ],
    "B2.TI": [
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
        {"thscode": "300750.SZ", "ticker": "300750", "name": "宁德时代"},
    ],
}


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext_presets, "_fuyao_api_key", lambda: "test-key")


# ---- 信封解包 ----


def test_fuyao_get_items_envelope_errors() -> None:
    async def run(transport: httpx.MockTransport) -> None:
        async with httpx.AsyncClient(
            base_url="https://fuyao.aicubes.cn", transport=transport
        ) as client:
            await _fuyao_get_items(client, _CATALOG, {})

    with pytest.raises(ValueError, match="HTTP 500"):
        asyncio.run(run(httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(ValueError, match="code=2001"):
        asyncio.run(
            run(
                httpx.MockTransport(
                    lambda r: httpx.Response(
                        200, json={"code": 2001, "message": "invalid key"}
                    )
                )
            )
        )
    with pytest.raises(ValueError, match=r"data\.item"):
        asyncio.run(
            run(httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 0})))
        )


# ---- 反转聚合 ----


def test_fetch_dimension_rows_inverts_by_catalog_order(with_key: None) -> None:
    rows = asyncio.run(
        _fetch_dimension_rows(
            "cn_concept",
            "concepts",
            transport=_mock_transport(
                {"cn_concept": _CATALOG_CN_CONCEPT}, _CONSTITUENTS_BY_CODE
            ),
        )
    )
    by_symbol = {r["symbol"]: r for r in rows}
    assert len(by_symbol) == 3
    # 板块名跟随目录顺序拼接 (B1 在前 B2 在后), 与请求完成顺序无关
    assert by_symbol["600519.SH"]["concepts"] == ["白酒概念", "新能源汽车"]
    assert by_symbol["600519.SH"]["name"] == "贵州茅台"
    assert by_symbol["000858.SZ"]["concepts"] == ["白酒概念"]
    assert by_symbol["300750.SZ"]["concepts"] == ["新能源汽车"]


def test_fetch_dimension_rows_tolerates_board_failure(with_key: None) -> None:
    """单板块重试后仍失败 → 跳过该板块, 其余正常返回。"""
    rows = asyncio.run(
        _fetch_dimension_rows(
            "cn_concept",
            "concepts",
            transport=_mock_transport(
                {"cn_concept": _CATALOG_CN_CONCEPT},
                _CONSTITUENTS_BY_CODE,
                fail_codes=("B2.TI",),
            ),
        )
    )
    by_symbol = {r["symbol"]: r for r in rows}
    assert set(by_symbol) == {"600519.SH", "000858.SZ"}  # B2 成员只剩重叠股
    assert by_symbol["600519.SH"]["concepts"] == ["白酒概念"]


def test_fetch_dimension_rows_all_failed_raises(with_key: None) -> None:
    with pytest.raises(ValueError, match="全部板块成分股拉取失败"):
        asyncio.run(
            _fetch_dimension_rows(
                "cn_concept",
                "concepts",
                transport=_mock_transport(
                    {"cn_concept": _CATALOG_CN_CONCEPT},
                    _CONSTITUENTS_BY_CODE,
                    fail_codes=("B1.TI", "B2.TI"),
                ),
            )
        )


def test_fetch_dimension_rows_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext_presets, "_fuyao_api_key", lambda: "")
    with pytest.raises(ValueError, match="FUYAO_API_KEY"):
        asyncio.run(_fetch_dimension_rows("cn_concept", "concepts"))


# ---- fetch_preset 端到端 (落盘) ----


def _inject_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    """fetch_preset 不透传 transport → 包一层原实现注入 MockTransport, 不出网。"""
    original = ext_presets._fetch_dimension_rows

    async def wrapper(tag: str, list_key: str, _transport=None):
        return await original(tag, list_key, transport=transport)

    monkeypatch.setattr(ext_presets, "_fetch_dimension_rows", wrapper)


def test_fetch_preset_concept_end_to_end(
    tmp_path: Path, with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_transport(
        monkeypatch,
        _mock_transport({"cn_concept": _CATALOG_CN_CONCEPT}, _CONSTITUENTS_BY_CODE),
    )

    n = asyncio.run(fetch_preset("ext_gn_ths", tmp_path))
    assert n == 3

    out = tmp_path / "ext_data" / "ext_gn_ths" / "part.parquet"
    assert out.exists(), "快照 parquet 未落盘"
    assert (tmp_path / "ext_data" / "ext_gn_ths" / "config.json").exists()

    df = pl.read_parquet(out)
    row = df.filter(pl.col("symbol") == "600519.SH").to_dicts()[0]
    assert row["所属概念"] == "白酒概念;新能源汽车"
    assert row["股票简称"] == "贵州茅台"
    assert row["code"] == "600519"


def test_fetch_preset_industry_end_to_end(
    tmp_path: Path, with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = [{"thscode": "H1.TI", "name": "种植业与林业"}]
    constituents = {
        "H1.TI": [{"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"}]
    }
    _inject_transport(
        monkeypatch, _mock_transport({"industry": catalog}, constituents)
    )

    n = asyncio.run(fetch_preset("ext_hy_ths", tmp_path))
    assert n == 1
    df = pl.read_parquet(tmp_path / "ext_data" / "ext_hy_ths" / "part.parquet")
    # fuyao 行业为扁平板块清单: 单级名称, 拼接后不变
    assert df["所属同花顺行业"].to_list() == ["种植业与林业"]


def test_fetch_preset_rejects_unknown_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知的内置预设"):
        asyncio.run(fetch_preset("ext_no_such", tmp_path))
