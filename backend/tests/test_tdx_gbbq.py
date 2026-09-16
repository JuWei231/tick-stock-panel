"""本地通达信 gbbq(股本变迁)读取 + provider 离线取股本路径的测试。

真实 gbbq 文件约 5.5MB 且属于用户本机数据, 不能进仓库, 因此分两层:

- 合成样本: 测试侧实现解密算法的**逆运算**(加密)来造文件, 往返必须逐位一致 ——
  这同时锁定了记录结构(29 字节)、字段顺序与市场号映射;
- 真实文件: 只做结构与覆盖性校验, 本机没有通达信安装时跳过(CI 常态)。
"""
from __future__ import annotations

import struct
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.plugins.tdx import gbbq
from app.plugins.tdx.provider import TdxProvider

# ---------------------------------------------------------------------------
# 合成 gbbq 的构造(加密 = 产品解密的逆)
# ---------------------------------------------------------------------------

_MASK32 = 0xFFFFFFFF
_RECORD_SIZE = 29


def _round_fn(num: int, key: int) -> int:
    """与 gbbq._decrypt_record 中同一轮变换(4 次查表 + 1 次轮密钥异或)。"""
    eax = gbbq._KEYS[gbbq._T0 + ((num >> 16) & 0xFF)]
    eax = (eax + gbbq._KEYS[gbbq._T1 + (num >> 24)]) & _MASK32
    eax ^= gbbq._KEYS[gbbq._T2 + ((num >> 8) & 0xFF)]
    eax = (eax + gbbq._KEYS[gbbq._T3 + (num & 0xFF)]) & _MASK32
    return eax ^ key


def _encrypt_record(plain: bytes) -> bytes:
    """29 字节明文 -> 29 字节记录密文(前 24 字节变换, 后 5 字节原样)。"""
    out = bytearray(plain)
    for block in range(3):
        off = block * 8
        p0, p1 = struct.unpack_from("<II", plain, off)
        num, numold = p1, p0 ^ gbbq._K_FINAL
        for key in reversed(gbbq._ROUND_KEYS):
            num, numold = numold, num ^ _round_fn(numold, key)
        struct.pack_into("<II", out, off, num ^ gbbq._K_INIT, numold)
    return bytes(out)


def _plain(market: int, code: str, day: int, category: int,
           f1: float = 0.0, f2: float = 0.0, f3: float = 0.0, f4: float = 0.0) -> bytes:
    return struct.pack(
        "<B7sIBffff", market, code.encode("ascii").ljust(7, b"\x00"), day, category, f1, f2, f3, f4
    )


def _write_gbbq(path: Path, plains: list[bytes]) -> Path:
    payload = bytearray(struct.pack("<I", len(plains)))
    for item in plains:
        payload += _encrypt_record(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(payload))
    return path


def _install_root(tmp_path: Path, plains: list[bytes]) -> Path:
    """造一个"通达信安装目录"(T0002/hq_cache/gbbq), 供 TDX_HOME 指向。"""
    root = tmp_path / "TradeTool"
    _write_gbbq(root / "T0002" / "hq_cache" / "gbbq", plains)
    return root


def _ymd(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


# ---------------------------------------------------------------------------
# 解密与记录解析
# ---------------------------------------------------------------------------

def test_key_table_length_invariant() -> None:
    """密钥表必须覆盖查表上界: 长度不足时导入即失败, 不允许静默解出错股本。"""
    assert len(gbbq.KEYS_HEX) == 8352  # 1044 个 uint32 = 4176 字节
    assert len(gbbq._KEYS) >= 0xC48 // 4 + 256
    assert len(gbbq._ROUND_KEYS) == 16


def test_record_roundtrip_keeps_market_code_date_category(tmp_path: Path) -> None:
    """合成文件的字段必须原样解出: 市场/代码/日期/类别 + 4 个浮点槽。"""
    path = _write_gbbq(tmp_path / "gbbq", [
        _plain(0, "000001", 20200102, 5, 1.0, 2.0, 3.0, 4.0),
        _plain(1, "600519", 20210528, 2, 5.0, 6.0, 7.0, 8.0),
        _plain(2, "920002", 20260527, 5, 9.0, 10.0, 11.0, 12.0),
    ])
    snapshot = gbbq.load_gbbq(path)
    assert snapshot is not None
    assert snapshot.record_count == 3
    assert snapshot.unparsable == 0
    assert set(snapshot.events) == {(0, "000001"), (1, "600519"), (2, "920002")}

    events = gbbq.events_for(snapshot, "600519.SH")
    assert len(events) == 1
    assert events[0]["date"] == date(2021, 5, 28)
    assert events[0]["category"] == 2
    assert events[0]["name"] == "送配股上市"
    # 非 category 1: 4 槽 = 前流通 / 前总股本 / 后流通 / 后总股本
    assert (events[0]["panqianliutong"], events[0]["qianzongguben"]) == (5.0, 6.0)
    assert (events[0]["panhouliutong"], events[0]["houzongguben"]) == (7.0, 8.0)
    assert events[0]["fenhong"] is None


def test_category_field_semantics_match_online_xdxr() -> None:
    """槽位口径必须与在线 xdxr 同序, 否则送转链与绝对股本会串位。"""
    cases = [
        (1, {"fenhong": 1.0, "peigujia": 2.0, "songzhuangu": 3.0, "peigu": 4.0}),
        (2, {"panqianliutong": 1.0, "qianzongguben": 2.0, "panhouliutong": 3.0, "houzongguben": 4.0}),
        (5, {"panqianliutong": 1.0, "qianzongguben": 2.0, "panhouliutong": 3.0, "houzongguben": 4.0}),
        (11, {"suogu": 3.0}),
        (13, {}),
    ]
    for category, expected in cases:
        parsed = gbbq._parse_event(_plain(1, "600519", 20200102, category, 1.0, 2.0, 3.0, 4.0))
        assert parsed is not None
        _market, _code, row = parsed
        for key, value in expected.items():
            assert row[key] == value, (category, key)
        # 未使用的槽必须保持 None, 不能被 0.0 顶替(0 会被当成真实股本)
        for key in ("fenhong", "peigujia", "songzhuangu", "peigu", "suogu",
                    "panqianliutong", "panhouliutong", "qianzongguben", "houzongguben"):
            if key not in expected:
                assert row[key] is None, (category, key)


def test_truncated_file_parses_available_records(tmp_path: Path) -> None:
    """头部声明的条数多于文件实际内容(客户端正在写)时, 按可用部分解析且不抛异常。"""
    path = _write_gbbq(tmp_path / "gbbq", [
        _plain(1, "600519", 20200102, 5, 0.0, 1000.0, 0.0, 2000.0),
        _plain(1, "600519", 20210102, 5, 0.0, 2000.0, 0.0, 4000.0),
    ])
    data = path.read_bytes()
    path.write_bytes(struct.pack("<I", 50) + data[4:-_RECORD_SIZE])  # 声明 50 条, 实际 1 条

    snapshot = gbbq.load_gbbq(path)
    assert snapshot is not None
    assert snapshot.record_count == 1
    assert len(gbbq.events_for(snapshot, "600519.SH")) == 1


@pytest.mark.parametrize("payload", [b"", b"\x01\x00", b"\x02\x00\x00\x00", b"\xff" * 40])
def test_unusable_files_return_none(tmp_path: Path, payload: bytes) -> None:
    """空文件/过短/纯垃圾必须 fail-closed 返回 None, 不能伪造出股本行。"""
    path = tmp_path / "gbbq"
    path.write_bytes(payload)
    assert gbbq.load_gbbq(path) is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert gbbq.load_gbbq(tmp_path / "nope" / "gbbq") is None


def test_newest_event_tracked_across_records(tmp_path: Path) -> None:
    path = _write_gbbq(tmp_path / "gbbq", [
        _plain(1, "600519", 20200102, 5, 0.0, 1000.0, 0.0, 2000.0),
        _plain(1, "600519", 20240102, 5, 0.0, 1000.0, 0.0, 2000.0),
        _plain(0, "000001", 20220102, 5, 0.0, 1000.0, 0.0, 2000.0),
    ])
    snapshot = gbbq.load_gbbq(path)
    assert snapshot is not None
    assert snapshot.newest_event == date(2024, 1, 2)


# ---------------------------------------------------------------------------
# 市场号映射(北交所 = 2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,expected", [
    ("000001.SZ", (0, "000001")),
    ("600519.SH", (1, "600519")),
    ("920002.BJ", (2, "920002")),
    ("830799.BJ", (2, "830799")),
])
def test_local_market_of_includes_bj_market_2(symbol: str, expected: tuple[int, str]) -> None:
    """本地 gbbq 用市场号 2 承载北交所 —— 在线 client 把 .BJ 映射到 0, 两者不同。"""
    assert gbbq.local_market_of(symbol) == expected


@pytest.mark.parametrize("symbol", ["600519", "600519.XX", ".SH", ""])
def test_local_market_of_rejects_bad_symbol(symbol: str) -> None:
    with pytest.raises(ValueError):
        gbbq.local_market_of(symbol)


def test_events_for_unknown_and_bad_symbols_is_empty(tmp_path: Path) -> None:
    path = _write_gbbq(tmp_path / "gbbq", [_plain(2, "920002", 20260527, 5, 1.0, 2.0, 3.0, 4.0)])
    snapshot = gbbq.load_gbbq(path)
    assert snapshot is not None
    assert gbbq.events_for(snapshot, "600519.SH") == ()
    assert gbbq.events_for(snapshot, "not-a-symbol") == ()


# ---------------------------------------------------------------------------
# 新鲜度与定位
# ---------------------------------------------------------------------------

def test_is_fresh_boundaries(tmp_path: Path) -> None:
    today = date(2026, 9, 16)
    path = _write_gbbq(tmp_path / "gbbq", [_plain(1, "600519", _ymd(today), 5)])
    snapshot = gbbq.load_gbbq(path)
    assert snapshot is not None
    assert gbbq.is_fresh(snapshot, today) is True

    at_limit = gbbq.GbbqSnapshot(
        path=path, mtime=0.0, record_count=1, events={},
        newest_event=today - timedelta(days=gbbq.MAX_EVENT_AGE_DAYS),
    )
    assert gbbq.is_fresh(at_limit, today) is True
    too_old = gbbq.GbbqSnapshot(
        path=path, mtime=0.0, record_count=1, events={},
        newest_event=today - timedelta(days=gbbq.MAX_EVENT_AGE_DAYS + 1),
    )
    assert gbbq.is_fresh(too_old, today) is False
    undated = gbbq.GbbqSnapshot(path=path, mtime=0.0, record_count=0, events={}, newest_event=None)
    assert gbbq.is_fresh(undated, today) is False


def test_find_local_gbbq_honours_env_install_root(tmp_path: Path, monkeypatch) -> None:
    """TDX_HOME 指向安装目录时直接命中, 不再做盘符探测。"""
    root = _install_root(tmp_path, [_plain(1, "600519", 20200102, 5)])
    monkeypatch.setenv("TDX_HOME", str(root))
    monkeypatch.delenv("TDX_DATA_DIR", raising=False)
    assert gbbq.find_local_gbbq() == root / "T0002" / "hq_cache" / "gbbq"


def test_find_local_gbbq_honours_env_file_path(tmp_path: Path, monkeypatch) -> None:
    """TDX_HOME 也可以直接指向 gbbq 文件本身。"""
    path = _write_gbbq(tmp_path / "somewhere" / "gbbq", [_plain(1, "600519", 20200102, 5)])
    monkeypatch.setenv("TDX_HOME", str(path))
    monkeypatch.delenv("TDX_DATA_DIR", raising=False)
    assert gbbq.find_local_gbbq() == path


def test_find_local_gbbq_returns_none_without_candidates(tmp_path: Path, monkeypatch) -> None:
    """无环境变量且盘符里没有通达信目录时返回 None(不得误命中无关文件)。"""
    monkeypatch.delenv("TDX_HOME", raising=False)
    monkeypatch.delenv("TDX_DATA_DIR", raising=False)
    monkeypatch.setattr(gbbq, "_candidate_roots", lambda: [tmp_path])
    assert gbbq.find_local_gbbq() is None


# ---------------------------------------------------------------------------
# Provider 契约: 本地优先 / 在线补齐 / 过期与损坏回退
# ---------------------------------------------------------------------------

class _FakeClient:
    """记录 xdxr 调用的假主站客户端。"""

    def __init__(self, xdxr: dict[str, list[dict]] | None = None) -> None:
        self._xdxr = xdxr or {}
        self.calls: list[tuple] = []

    def xdxr(self, market: int, code: str) -> list[dict]:
        self.calls.append((market, code))
        return self._xdxr.get(code, [])

    def close(self) -> None:
        pass


class _OfflineClient:
    """任何网络调用都失败的客户端 —— 用来证明本地路径确实离线。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def xdxr(self, market: int, code: str):
        self.calls.append((market, code))
        raise AssertionError(f"本地已覆盖, 不应联网: market={market} code={code}")

    def close(self) -> None:
        pass


def test_get_financials_shares_reads_local_offline_including_bj(
    tmp_path: Path, monkeypatch
) -> None:
    """本地 gbbq 覆盖时完全不联网, 且北交所(市场号 2)也能出阶梯。"""
    today = date.today()
    plains = [
        # 槽位 = 前流通 / 前总股本 / 后流通 / 后总股本(万股); 流通值必须非零,
        # 否则 _abs_shares 判为缺失(不猜), float_shares 会留空
        _plain(1, "600519", _ymd(today), 5, 12500.0, 12500.0, 12500.0, 12500.0),
        _plain(2, "920002", _ymd(today), 5, 3185.5, 4550.3, 4459.7, 6370.4),
    ]
    monkeypatch.setenv("TDX_HOME", str(_install_root(tmp_path, plains)))
    provider = TdxProvider()
    client = _OfflineClient()
    provider._client = client  # type: ignore[assignment]

    df = provider.get_financials("shares", ["600519.SH", "920002.BJ"]).sort("symbol")
    assert client.calls == []  # 一次网络都没打
    assert df.columns == ["symbol", "period_end", "announce_date", "total_shares", "float_shares"]
    got = {r["symbol"]: (r["total_shares"], r["float_shares"]) for r in df.to_dicts()}
    # 单位: 通达信为万股, provider 侧乘 1e4 转股。gbbq 用 IEEE float32 存股本,
    # 10 位数股本就带 ~1e-7 相对量化误差(数据源特性, 与在线压缩浮点同量级)。
    assert got["600519.SH"][0] == pytest.approx(1.25e8, rel=1e-6)
    assert got["600519.SH"][1] == pytest.approx(1.25e8, rel=1e-6)
    assert got["920002.BJ"][0] == pytest.approx(6.3704e7, rel=1e-6)
    assert got["920002.BJ"][1] == pytest.approx(4.4597e7, rel=1e-6)
    # 阶梯 = 首个事件之前的基准行 + 事件行
    for symbol in ("600519.SH", "920002.BJ"):
        dates = [r["period_end"] for r in df.filter(df["symbol"] == symbol).to_dicts()]
        assert set(dates) == {date(1990, 1, 1), today}


def test_get_financials_shares_local_matches_online_for_same_events(
    tmp_path: Path, monkeypatch
) -> None:
    """同一组事件走本地与走在线必须得到完全相同的行(口径不得分叉)。"""
    today = date.today()
    day = _ymd(today)
    plains = [
        _plain(1, "600519", day, 5, 900.0, 1000.0, 1800.0, 2000.0),
    ]
    online_events = [{
        "date": today, "category": 5, "name": "股本变化",
        "qianzongguben": 1000.0, "houzongguben": 2000.0,
        "panqianliutong": 900.0, "panhouliutong": 1800.0,
    }]

    monkeypatch.setenv("TDX_HOME", str(_install_root(tmp_path, plains)))
    local_provider = TdxProvider()
    local_provider._client = _OfflineClient()  # type: ignore[assignment]
    local = local_provider.get_financials("shares", ["600519.SH"]).sort("period_end")

    monkeypatch.delenv("TDX_HOME", raising=False)
    monkeypatch.setattr(gbbq, "_candidate_roots", lambda: [])
    online_provider = TdxProvider()
    online_provider._client = _FakeClient({"600519": online_events})  # type: ignore[assignment]
    online = online_provider.get_financials("shares", ["600519.SH"]).sort("period_end")

    assert local.equals(online)
    assert local.height == 2  # 基准行 + 事件行


def test_get_financials_shares_falls_back_online_for_uncovered_symbols(
    tmp_path: Path, monkeypatch
) -> None:
    """本地没有覆盖的标的仍逐只走在线, 且只对这些标的发请求。"""
    today = date.today()
    monkeypatch.setenv("TDX_HOME", str(_install_root(tmp_path, [
        _plain(1, "600519", _ymd(today), 5, 0.0, 12500.0, 0.0, 12500.0),
    ])))
    provider = TdxProvider()
    client = _FakeClient({"000001": [{
        "date": today, "category": 5, "name": "股本变化",
        "qianzongguben": 1000.0, "houzongguben": 2000.0,
        "panqianliutong": 900.0, "panhouliutong": 1800.0,
    }]})
    provider._client = client  # type: ignore[assignment]

    df = provider.get_financials("shares", ["600519.SH", "000001.SZ"])
    assert set(df["symbol"].to_list()) == {"600519.SH", "000001.SZ"}
    assert client.calls == [(0, "000001")]


def test_bj_online_fallback_still_uses_market_0(tmp_path: Path, monkeypatch) -> None:
    """本地未覆盖时北交所仍按在线口径请求市场 0(已知在线缺口, 这里锁住现状)。"""
    monkeypatch.delenv("TDX_HOME", raising=False)
    monkeypatch.setattr(gbbq, "_candidate_roots", lambda: [])
    provider = TdxProvider()
    client = _FakeClient()
    provider._client = client  # type: ignore[assignment]

    df = provider.get_financials("shares", ["920999.BJ"])
    assert df.is_empty()
    assert client.calls == [(0, "920999")]


def test_get_financials_shares_ignores_stale_local_file(tmp_path: Path, monkeypatch) -> None:
    """本地文件最新事件过旧(客户端久未运行)时必须回退在线, 不能静默用过期股本。"""
    stale_day = date.today() - timedelta(days=gbbq.MAX_EVENT_AGE_DAYS + 10)
    monkeypatch.setenv("TDX_HOME", str(_install_root(tmp_path, [
        _plain(1, "600519", _ymd(stale_day), 5, 0.0, 1000.0, 900.0, 2000.0),
    ])))
    provider = TdxProvider()
    client = _FakeClient({"600519": [{
        "date": date.today(), "category": 5, "name": "股本变化",
        "qianzongguben": 2000.0, "houzongguben": 4000.0,
        "panqianliutong": 1800.0, "panhouliutong": 3600.0,
    }]})
    provider._client = client  # type: ignore[assignment]

    df = provider.get_financials("shares", ["600519.SH"]).sort("period_end")
    assert client.calls == [(1, "600519")]
    assert df["total_shares"].max() == 4e7  # 在线值, 不是本地过期的 2e7


def test_get_financials_shares_survives_corrupt_local_file(tmp_path: Path, monkeypatch) -> None:
    """本地文件损坏时回退在线, 不抛异常也不产生半截数据。"""
    root = tmp_path / "TradeTool"
    target = root / "T0002" / "hq_cache" / "gbbq"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff" * 200)
    monkeypatch.setenv("TDX_HOME", str(root))

    provider = TdxProvider()
    client = _FakeClient({"600519": [{
        "date": date.today(), "category": 5, "name": "股本变化",
        "qianzongguben": 1000.0, "houzongguben": 2000.0,
        "panqianliutong": 900.0, "panhouliutong": 1800.0,
    }]})
    provider._client = client  # type: ignore[assignment]

    df = provider.get_financials("shares", ["600519.SH"])
    assert client.calls == [(1, "600519")]
    assert df["total_shares"].max() == 2e7


def test_local_snapshot_is_cached_until_file_changes(tmp_path: Path, monkeypatch) -> None:
    """同一进程内重复调用只解密一次; 文件被客户端重写后重新解密。"""
    today = date.today()
    root = _install_root(tmp_path, [
        _plain(1, "600519", _ymd(today), 5, 0.0, 1000.0, 0.0, 1000.0),
    ])
    monkeypatch.setenv("TDX_HOME", str(root))
    provider = TdxProvider()
    provider._client = _OfflineClient()  # type: ignore[assignment]

    first = provider.get_financials("shares", ["600519.SH"])
    assert first["total_shares"].max() == 1e7
    assert provider._local_gbbq() is provider._local_gbbq()  # 命中缓存

    # 客户端重写文件: 内容与大小都变(避免依赖 mtime 精度判断缓存是否失效)
    _write_gbbq(root / "T0002" / "hq_cache" / "gbbq", [
        _plain(1, "600519", _ymd(today), 5, 0.0, 2000.0, 0.0, 2000.0),
        _plain(1, "000001", _ymd(today), 5, 0.0, 3000.0, 0.0, 3000.0),
    ])
    again = provider.get_financials("shares", ["600519.SH"])
    assert again["total_shares"].max() == 2e7


# ---------------------------------------------------------------------------
# 真实文件校验(本机未装通达信时跳过)
# ---------------------------------------------------------------------------

_REAL_PATH = gbbq.find_local_gbbq()


@pytest.fixture(scope="module")
def real_snapshot() -> gbbq.GbbqSnapshot:
    assert _REAL_PATH is not None
    snapshot = gbbq.load_gbbq(_REAL_PATH)
    assert snapshot is not None
    return snapshot


@pytest.mark.skipif(_REAL_PATH is None, reason="本机无本地通达信 gbbq")
def test_real_gbbq_structure(real_snapshot: gbbq.GbbqSnapshot) -> None:
    """真实文件必须解出全市场事件: 三个市场号、纯数字代码、已知类别、无非法记录。"""
    assert real_snapshot.record_count > 10_000
    assert real_snapshot.unparsable == 0
    assert len(real_snapshot.events) > 1_000
    # 文件头声明的条数与文件大小必须自洽: 4 + 29 * N == size
    assert _REAL_PATH is not None
    assert _REAL_PATH.stat().st_size == 4 + gbbq._RECORD_SIZE * real_snapshot.record_count
    markets = {market for market, _code in real_snapshot.events}
    assert markets == {0, 1, 2}
    assert real_snapshot.newest_event is not None
    assert real_snapshot.newest_event.year >= 2015

    samples = list(real_snapshot.events.items())[:200]
    for (_market, code), rows in samples:
        assert len(code) == 6 and code.isdigit()
        for row in rows:
            assert row["category"] in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}
            assert isinstance(row["date"], date)


def _platform_shares_path() -> Path | None:
    """在线 xdxr 生成的平台股本表。测试机没同步过股本时跳过比对。"""
    try:
        from app.config import settings
    except Exception:
        return None
    path = settings.data_dir / "financials" / "shares" / "part.parquet"
    return path if path.exists() else None


@pytest.mark.skipif(
    _REAL_PATH is None or _platform_shares_path() is None,
    reason="本机无本地通达信 gbbq 或平台股本表",
)
def test_real_gbbq_reproduces_platform_shares_table(real_snapshot: gbbq.GbbqSnapshot) -> None:
    """本地 gbbq 推出的股本必须与在线 xdxr 生成的平台股本表一致(±0.1%)。

    这是"本地 == 在线"的回归证据: 平台表由逐标的在线 xdxr 落盘, 与本文件的解密
    结果完全同源。比对用 as-of 语义 —— 本地文件可能比线上旧几天, 只比到本地覆盖日。
    """
    import polars as pl

    from app.plugins.tdx.provider import _share_rows_from_events

    path = _platform_shares_path()
    assert path is not None
    table = pl.read_parquet(path)
    symbols = [s for s in table["symbol"].unique().to_list() if not s.endswith(".BJ")]
    step = max(1, len(symbols) // 120)
    sample = symbols[::step][:120]
    reference: dict[str, list[tuple[date, float]]] = {}
    for row in table.filter(pl.col("symbol").is_in(sample)).select(
        ["symbol", "period_end", "total_shares"]
    ).to_dicts():
        reference.setdefault(row["symbol"], []).append((row["period_end"], row["total_shares"]))

    checked = 0
    mismatches: list[str] = []
    for symbol in sample:
        local_rows = _share_rows_from_events(symbol, gbbq.events_for(real_snapshot, symbol))
        if len(local_rows) < 2:  # 只有基准行: 该标的本地没有可比事件
            continue
        cutoff = local_rows[-1]["period_end"]
        online_rows = reference.get(symbol, [])
        if not online_rows:
            continue
        if cutoff > max(online_rows)[0]:
            # 本地文件比平台表新(平台尚未同步到该事件): 没有对应行可比, 跳过。
            # 平台侧按 (symbol, period_end) 取并集合并, 下次同步会补上这行。
            continue
        online_total = max(r for r in online_rows if r[0] <= cutoff)[1]
        local_total = local_rows[-1]["total_shares"]
        if not online_total or not local_total:
            continue
        checked += 1
        if abs(local_total - online_total) / online_total > 0.001:
            mismatches.append(
                f"{symbol}: 本地 {local_total:,.0f} vs 在线 {online_total:,.0f} @ {cutoff}"
            )

    assert checked >= 30, f"可比对样本太少({checked}), 校验无意义"
    assert not mismatches, "本地与在线股本不一致:\n" + "\n".join(mismatches[:10])


@pytest.mark.skipif(_REAL_PATH is None, reason="本机无本地通达信 gbbq")
def test_real_gbbq_covers_bj_market(real_snapshot: gbbq.GbbqSnapshot) -> None:
    """北交所(市场号 2)在本地文件里有事件与绝对股本锚点 —— 在线路径拿不到这批。"""
    bj = {
        code: rows for (market, code), rows in real_snapshot.events.items() if market == 2
    }
    assert len(bj) > 100
    anchored = sum(
        1 for rows in bj.values() if any((row["houzongguben"] or 0) > 0 for row in rows)
    )
    assert anchored > 100, f"北交所有绝对股本锚点的标的只有 {anchored} 只"
