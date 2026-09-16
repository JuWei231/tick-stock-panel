"""通达信数据源插件契约测试。

不依赖真实网络与主站: 协议层用**真实抓取的响应字节**做固定样本(bars/指数/快照
各一份, 见文件内 hex 常量), provider 层注入 FakeClient。

覆盖 CONTRIBUTING 与 plugin-development 要求的契约项:
  - 字段映射与单位转换: 价格 ÷1000/÷100、基金 ÷1000、volume 手、change_pct 小数制
  - K线增量价格解码、指数多 4 字节涨跌家数
  - 短记录(退市/停牌)容错与批量拆包隔离 —— 不得因一条坏记录丢掉整批
  - 分钟 datetime 北京墙钟 naive
  - 能力声明: financial 不声明
  - 软失败: get_realtime 返回 []; get_realtime_indices 返回 None
  - loader 集成: plugin.yaml 被正确解析注册
"""
from __future__ import annotations

import struct
import zlib
from datetime import date, datetime

import pytest

from app.plugins.tdx import protocol as proto
from app.plugins.tdx.client import (
    TdxClient,
    TdxError,
    from_wire,
    is_fund_code,
    to_wire,
)
from app.plugins.tdx.provider import MINUTE_COLS, TdxProvider

# ---------------------------------------------------------------------------
# 真实响应字节 (2026-09-11 抓取自可用主站, 未压缩)
# ---------------------------------------------------------------------------

# 600519.SH 日K 两根 (2026-09-10, 2026-09-11)
BARS_HEX = (
    "02002e283501b8cb9d01ee5b963ee88c0100a8934601c3104f2f28350114c69c01a80ffcd90200f10747a30c844f"
)

# 000001.SH 指数日K 两根 —— 每条记录比证券K线多 4 字节涨跌家数
INDEX_BARS_HEX = (
    "02002e28350192ece003d249b09e01dcb7013ee9934a1d883553ce0131072f283501f8ee02dae402b815ca9807febbb0"
    "4a5b185f5315010a08"
)

# 600519.SH + 510300.SH(ETF) 快照 —— 前 2 字节是前缀, 条数在 offset 2
QUOTES_HEX = (
    "000002000136303035313936109cc80fa50fa70f8b11ff1295dfbc0edcc80fb19f048709a30c844f909702a1880200b1"
    "9d32009401090e43a101010144a301010246ab0104014b82020101960a00000000f7ff361001353130333030a312a347"
    "260d0d6fbdc5fa91019484059b819c0987ca031b69834f83ea94049997870500000001b2a501a0a5024102af92018068"
    "4203962f814243048c449c204405af4f893c9614000000000200a312"
)


def _hex(value: str) -> bytes:
    return bytes.fromhex(value)


# ---------------------------------------------------------------------------
# 基础解码
# ---------------------------------------------------------------------------

def test_decode_varint_single_and_multi_byte() -> None:
    """单字节(低 6 位)与多字节(续读位)两种编码都要解对, 含符号位。"""
    value, pos = proto.decode_varint(bytes([0x05]), 0)
    assert (value, pos) == (5, 1)

    # 0x41 = 符号位 + 值 1 → -1
    value, pos = proto.decode_varint(bytes([0x41]), 0)
    assert (value, pos) == (-1, 1)

    # 续读: 首字节 0x80|0x01 表示低位 1, 次字节 0x02 贡献 2<<6=128
    value, pos = proto.decode_varint(bytes([0x81, 0x02]), 0)
    assert (value, pos) == (1 + (2 << 6), 2)


def test_decompress_float_zero_and_known_values() -> None:
    """压缩浮点: 0 直接返回 0; 非零值量级合理。

    真实样本: 600519 当日成交额 4,430,841,344 元, 由响应内的 4 字节还原。
    """
    assert proto.decompress_float(0) == 0.0
    # 从真实 bars 样本中取出的成交额字段
    body = _hex(BARS_HEX)
    rows = proto.parse_bars(body, proto.CAT_DAY)
    assert rows[-1]["amount"] == pytest.approx(4430841344.0, rel=1e-6)


def test_decode_bar_datetime_day_vs_minute() -> None:
    """日线用 uint32 YYYYMMDD; 分钟周期用压缩日 + 当日分钟数。"""
    day_buf = struct.pack("<I", 20260911)
    day, hour, minute, pos = proto.decode_bar_datetime(day_buf, 0, proto.CAT_DAY)
    assert (day, hour, minute, pos) == (date(2026, 9, 11), 15, 0, 4)

    # 分钟: zipday = ((year-2004) << 11) + month*100 + day
    zipday = ((2026 - 2004) << 11) + 9 * 100 + 11
    minute_buf = struct.pack("<HH", zipday, 9 * 60 + 35)
    day, hour, minute, pos = proto.decode_bar_datetime(minute_buf, 0, proto.CAT_1MIN)
    assert (day, hour, minute) == (date(2026, 9, 11), 9, 35)


# ---------------------------------------------------------------------------
# K线解析
# ---------------------------------------------------------------------------

def test_parse_bars_real_fixture_delta_decoding() -> None:
    """真实样本: 增量价格解码必须复现出与实际行情一致的开高低收。

    价格是"上一根开+收"的增量, 且原始值 ÷1000 —— 解错不会报错, 只会静默
    给出看起来合理的错误价格, 因此这里用真实字节固定住。
    """
    rows = proto.parse_bars(_hex(BARS_HEX), proto.CAT_DAY)
    assert len(rows) == 2

    first, second = rows
    assert first["date"] == date(2026, 9, 10)
    assert (first["open"], first["high"], first["low"], first["close"]) == (
        pytest.approx(1291.0), pytest.approx(1294.99), pytest.approx(1282.0), pytest.approx(1285.13),
    )
    assert first["volume"] == pytest.approx(18900.0)

    # 第二根的基准来自第一根(开+收), 若基准处理错误这里会整体偏移
    assert second["date"] == date(2026, 9, 11)
    assert (second["open"], second["high"], second["low"], second["close"]) == (
        pytest.approx(1285.15), pytest.approx(1286.15), pytest.approx(1263.01), pytest.approx(1275.16),
    )
    assert second["volume"] == pytest.approx(34801.0)


def test_parse_bars_index_needs_is_index_flag() -> None:
    """指数记录多 4 字节涨跌家数: 不跳过则日期与价格全错。

    这正是"拿证券解析器读指数会得到 104029-99-96 这种乱码日期"的根因。
    """
    body = _hex(INDEX_BARS_HEX)

    idx = proto.parse_bars(body, proto.CAT_DAY, is_index=True)
    assert len(idx) == 2
    assert idx[-1]["date"] == date(2026, 9, 11)
    assert idx[-1]["close"] == pytest.approx(3888.11)

    # 按证券解析(不跳 4 字节)会解析失败或给出错误日期 —— 证明该标志是必需的
    wrong = proto.parse_bars(body, proto.CAT_DAY, is_index=False)
    assert not wrong or wrong[-1]["close"] != pytest.approx(3888.11)


def test_parse_bars_tolerates_truncated_body() -> None:
    """截断的响应必须返回已解析部分而不是抛异常或返回垃圾。"""
    full = _hex(BARS_HEX)
    rows = proto.parse_bars(full[:12], proto.CAT_DAY)
    assert isinstance(rows, list)
    assert len(rows) <= 2


def test_parse_bars_empty_body() -> None:
    assert proto.parse_bars(b"", proto.CAT_DAY) == []
    assert proto.parse_bars(b"\x00", proto.CAT_DAY) == []


# ---------------------------------------------------------------------------
# 快照解析
# ---------------------------------------------------------------------------

def test_parse_quotes_real_fixture_stock_and_etf() -> None:
    """真实样本(600519 个股 + 510300 ETF): 条数在 offset 2, 价格按资产类别定除数。

    个股 ÷100、基金 ÷1000。若基金也用 100, ETF 价格会整体放大 10 倍 ——
    页面照常渲染, 但数字全错。
    """
    rows = proto.parse_quotes(_hex(QUOTES_HEX), price_divisor_of=TdxClient._divisor)
    assert len(rows) == 2

    stock, etf = rows
    assert stock["code"] == "600519"
    assert stock["price"] == pytest.approx(1275.16)
    assert stock["last_close"] == pytest.approx(1285.13)
    assert stock["vol"] == pytest.approx(34801.0)

    assert etf["code"] == "510300"
    assert etf["price"] == pytest.approx(4.579)
    assert etf["last_close"] == pytest.approx(4.617)


def test_parse_quotes_etf_divisor_guard() -> None:
    """同一份字节: 全按 ÷100 解析会让 ETF 变成 10 倍, 证明除数判定是必需的。"""
    naive = proto.parse_quotes(_hex(QUOTES_HEX), price_divisor_of=lambda m, c: 100.0)
    correct = proto.parse_quotes(_hex(QUOTES_HEX), price_divisor_of=TdxClient._divisor)

    naive_etf = next(r for r in naive if r["code"] == "510300")
    correct_etf = next(r for r in correct if r["code"] == "510300")
    assert naive_etf["price"] == pytest.approx(correct_etf["price"] * 10, rel=1e-9)


def test_parse_quotes_returns_five_levels() -> None:
    """五档价量必须齐全, 且买一价与最新价一致。"""
    rows = proto.parse_quotes(_hex(QUOTES_HEX), price_divisor_of=TdxClient._divisor)
    stock = next(r for r in rows if r["code"] == "600519")
    assert len(stock["bid_prices"]) == 5
    assert len(stock["ask_prices"]) == 5
    assert len(stock["bid_volumes"]) == 5
    assert len(stock["ask_volumes"]) == 5
    assert stock["bid_prices"][0] == pytest.approx(stock["price"])
    assert stock["ask_prices"][0] > stock["bid_prices"][0]
    # 数量单位为「手」
    assert stock["bid_volumes"][0] == pytest.approx(9)


def test_parse_quotes_tolerates_short_record() -> None:
    """退市/异常标的的短记录不得让解析抛异常。"""
    full = _hex(QUOTES_HEX)
    for cut in (5, 20, 60, 100, 160):
        rows = proto.parse_quotes(full[:cut], price_divisor_of=TdxClient._divisor)
        assert isinstance(rows, list)


def test_parse_quotes_empty_body() -> None:
    assert proto.parse_quotes(b"", price_divisor_of=TdxClient._divisor) == []
    assert proto.parse_quotes(b"\x00\x00\x00", price_divisor_of=TdxClient._divisor) == []


# ---------------------------------------------------------------------------
# 响应拆包
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("use_zip", [False, True])
def test_split_response_handles_both_forms(use_zip: bool) -> None:
    """压缩长度 == 解压长度 表示未压缩(实测小批量走这条), 否则 zlib。"""
    plain = b"hello-body"
    if use_zip:
        payload = zlib.compress(plain)
        head = struct.pack("<IIIHH", 0, 0, 0, len(payload), len(plain))
    else:
        payload = plain
        head = struct.pack("<IIIHH", 0, 0, 0, len(plain), len(plain))
    assert proto.split_response(head, payload) == plain


def test_split_response_rejects_bad_header() -> None:
    with pytest.raises(ValueError):
        proto.split_response(b"\x00" * 4, b"")


# ---------------------------------------------------------------------------
# 报文构造
# ---------------------------------------------------------------------------

def test_quotes_packet_header_encodes_count_and_length() -> None:
    """报文头 22 字节: 条数在 offset 20, 其后每只 7 字节(market 1 + code 6)。"""
    pkt = proto.quotes_packet([(1, "600519"), (0, "000001")])
    stock_len = struct.unpack_from("<H", pkt, 20)[0]
    payload_len = struct.unpack_from("<H", pkt, 6)[0]
    assert stock_len == 2
    assert payload_len == 2 * 7 + 12
    assert len(pkt) == 22 + 2 * 7


def test_quotes_packet_rejects_empty() -> None:
    with pytest.raises(ValueError):
        proto.quotes_packet([])


def test_max_quote_batch_is_protocol_limit() -> None:
    """协议硬上限 80: 实测请求 500 只只回 80 条。"""
    assert proto.MAX_QUOTE_BATCH == 80


# ---------------------------------------------------------------------------
# 代码格式与资产类别
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "symbol,expected",
    [("600519.SH", (1, "600519")), ("000001.SZ", (0, "000001")), ("920289.BJ", (0, "920289"))],
)
def test_to_wire(symbol, expected) -> None:
    assert to_wire(symbol) == expected


def test_from_wire_roundtrip() -> None:
    assert from_wire(1, "600519") == "600519.SH"
    assert from_wire(0, "000001") == "000001.SZ"


def test_to_wire_rejects_bad_symbol() -> None:
    with pytest.raises(ValueError):
        to_wire("600519")
    with pytest.raises(ValueError):
        to_wire("600519.XX")


@pytest.mark.parametrize(
    "code,expected",
    [("510300", True), ("159915", True), ("512880", True), ("588000", True),
     ("600519", False), ("000001", False), ("300750", False), ("688981", False)],
)
def test_is_fund_code(code, expected) -> None:
    assert is_fund_code(code) is expected


def test_divisor_rule_per_asset_class() -> None:
    assert TdxClient._divisor(1, "600519") == 100.0
    assert TdxClient._divisor(0, "000001") == 100.0
    assert TdxClient._divisor(1, "510300") == 1000.0
    assert TdxClient._divisor(0, "159915") == 1000.0


# ---------------------------------------------------------------------------
# 除权因子推导
# ---------------------------------------------------------------------------

def test_ex_factor_formula() -> None:
    """除权价 = (前收 - 每股分红 + 每股配股*配股价) / (1 + 送转 + 配股)。"""
    # 每 10 股派 10 元 → 每股 1 元; 前收 10 元 → 除权价 9 元 → 因子 10/9
    factor = proto.ex_factor_from_event(
        {"category": 1, "fenhong": 10.0, "songzhuangu": 0.0, "peigu": 0.0, "peigujia": 0.0},
        10.0,
    )
    assert factor == pytest.approx(10.0 / 9.0)

    # 每 10 股送 10 股 → 除权价 5 元 → 因子 2
    factor = proto.ex_factor_from_event(
        {"category": 1, "fenhong": 0.0, "songzhuangu": 10.0, "peigu": 0.0, "peigujia": 0.0},
        10.0,
    )
    assert factor == pytest.approx(2.0)


@pytest.mark.parametrize(
    "event,prev_close",
    [
        ({"category": 5, "fenhong": 1.0}, 10.0),        # 非除权除息类别
        ({"category": 1, "fenhong": 1.0}, 0.0),         # 前收为 0
        ({"category": 1, "fenhong": 1.0}, None),        # 前收缺失
        ({"category": 1, "fenhong": 1000.0}, 10.0),     # 除权价为负 → 不可信
    ],
)
def test_ex_factor_fail_closed(event, prev_close) -> None:
    """无法可靠推导时返回 None, 绝不猜一个因子出来。"""
    assert proto.ex_factor_from_event(event, prev_close) is None


def test_ex_factor_real_fixture_plausible() -> None:
    """真实事件量级检查: 茅台 2026-06-26 分红 280.24(每10股), 因子应接近 1.02。"""
    factor = proto.ex_factor_from_event(
        {"category": 1, "fenhong": 280.2423095703125, "songzhuangu": 0.0,
         "peigu": 0.0, "peigujia": 0.0},
        1212.1,
    )
    assert factor == pytest.approx(1.0237, abs=1e-3)


# ---------------------------------------------------------------------------
# 客户端: 批量拆包与坏记录隔离
# ---------------------------------------------------------------------------

class _FakeConn:
    """按脚本返回 body 的假连接。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[bytes] = []

    def call(self, packet: bytes) -> bytes:
        self.calls.append(packet)
        item = self.script.pop(0) if self.script else b""
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass

    @property
    def alive(self) -> bool:
        return True


def _client_with(script: list) -> TdxClient:
    client = TdxClient()
    client._conn = _FakeConn(script)  # type: ignore[assignment]
    return client


def test_quotes_splits_batch_when_rows_missing() -> None:
    """缺条时二分拆包: 一条坏记录不能吞掉同批其它标的的行情。

    实测 pytdx 遇到退市标的的短记录会丢掉整批 80 只, 属于静默数据缺失。
    这里整批只回 1 条, 拆成两个单只后各自成功, 最终两只都拿到。
    """
    client = _client_with([_QUOTES_ONE_ROW, _QUOTES_ONE_ROW, _QUOTES_ONE_ROW])
    rows = client.quotes(["600519.SH", "510300.SH"])
    assert len(rows) == 2, "拆包后应两只都有行情"
    packets = client._conn.calls  # type: ignore[union-attr]
    assert len(packets) == 3, "应先整批请求, 再拆成两个单只"
    assert struct.unpack_from("<H", packets[0], 20)[0] == 2
    assert struct.unpack_from("<H", packets[1], 20)[0] == 1


def test_quotes_gives_up_on_single_bad_symbol_without_raising() -> None:
    """单只彻底无行情时放弃该只, 不抛异常也不影响其它标的。"""
    client = _client_with([b"", b"", _QUOTES_ONE_ROW])
    rows = client.quotes(["600519.SH", "510300.SH"])
    assert len(rows) == 1


def test_quotes_chunks_at_protocol_limit() -> None:
    """超过 80 只必须自动分片, 每片不超过协议上限。"""
    symbols = [f"{i:06d}.SZ" for i in range(1, 161)]
    script = [_hex(QUOTES_HEX)] * 4
    client = _client_with(script)
    client.quotes(symbols)
    packets = client._conn.calls  # type: ignore[union-attr]
    counts = [struct.unpack_from("<H", p, 20)[0] for p in packets]
    assert counts, "未发出任何请求"
    assert max(counts) <= proto.MAX_QUOTE_BATCH
    assert sum(counts) >= len(symbols)


def test_client_raises_when_no_server_available() -> None:
    """池内全部不可用时必须报错(fail-closed), 不能静默返回空数据。"""
    client = TdxClient(servers=(("127.0.0.1", 1),), probe_limit=1)
    with pytest.raises(TdxError):
        client.bars(proto.CAT_DAY, 1, "600519", 0, 1)


# 仅含第一条记录的快照响应(用于缺条场景)
_QUOTES_ONE_ROW = struct.pack("<HH", 0, 1) + _hex(QUOTES_HEX)[4:4 + 83]


# ---------------------------------------------------------------------------
# Provider 契约
# ---------------------------------------------------------------------------

class FakeTdxClient:
    """注入用假客户端: 记录调用并按设定返回。"""

    def __init__(self, bars=None, index_bars=None, xdxr=None, quotes=None, fail=()) -> None:
        self._bars = bars or {}
        self._index_bars = index_bars or {}
        self._xdxr = xdxr or {}
        self._quotes = quotes or []
        self._fail = set(fail)
        self.calls: list[tuple] = []

    def bars(self, category, market, code, start, count):
        self.calls.append(("bars", category, market, code, start, count))
        if "bars" in self._fail:
            raise TdxError("boom")
        if start > 0:
            return []
        return self._bars.get(code, [])

    def index_bars(self, category, market, code, start, count):
        self.calls.append(("index_bars", category, market, code, start, count))
        if start > 0:
            return []
        return self._index_bars.get(code, [])

    def xdxr(self, market, code):
        return self._xdxr.get(code, [])

    def quotes(self, symbols, max_batch=80):
        if "quotes" in self._fail:
            raise TdxError("boom")
        self.calls.append(("quotes", tuple(symbols)))
        return list(self._quotes)

    def close(self):
        pass


def _provider(client) -> TdxProvider:
    p = TdxProvider()
    p._client = client  # type: ignore[assignment]
    # 本文件的用例锁定**在线**分支: provider 在有本地 gbbq 的机器上会优先用本地文件
    # (见 tests/test_tdx_gbbq.py), 不固定住会让结果随开发机是否装通达信而变。
    p._gbbq_probed = True
    p._gbbq_path = None
    return p


def _bar(day: date, close: float, volume: float = 100.0) -> dict:
    return {"date": day, "hour": 15, "minute": 0, "open": close, "high": close,
            "low": close, "close": close, "volume": volume, "amount": close * volume * 100}


def test_capability_declaration_includes_financial_shares() -> None:
    """声明 7 个数据集; financial 仅提供 shares(逐日股本), 三大报表不提供。"""
    ds = TdxProvider().config.datasets
    assert set(ds) == {"daily", "adj_factor", "minute", "full_minute", "realtime", "depth5", "financial"}
    assert "financial" in ds


def test_provider_identity_and_minute_depth() -> None:
    p = TdxProvider()
    assert p.name == "tdx"
    assert p.builtin is True
    assert p.minute_history_days == 20


def test_get_daily_matches_contract_schema() -> None:
    client = FakeTdxClient(bars={"600519": [_bar(date(2026, 9, 11), 1275.16, 34801.0)]})
    df = _provider(client).get_daily(["600519.SH"])
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "quote_ts"]
    assert df.height == 1
    assert df["symbol"][0] == "600519.SH"
    assert df["date"][0] == date(2026, 9, 11)
    assert df["volume"][0] == pytest.approx(34801.0)


def test_get_daily_filters_to_requested_window() -> None:
    bars = [_bar(date(2026, 9, 1), 1.0), _bar(date(2026, 9, 11), 2.0)]
    client = FakeTdxClient(bars={"600519": bars})
    df = _provider(client).get_daily(
        ["600519.SH"], datetime(2026, 9, 10), datetime(2026, 9, 12)
    )
    assert df.height == 1
    assert df["date"][0] == date(2026, 9, 11)


def test_get_daily_index_asset_uses_index_parser() -> None:
    client = FakeTdxClient(index_bars={"000001": [_bar(date(2026, 9, 11), 3888.11)]})
    df = _provider(client).get_daily(["000001.SH"], asset_type="index")
    assert df.height == 1
    assert df["close"][0] == pytest.approx(3888.11)
    assert any(c[0] == "index_bars" for c in client.calls)


def test_get_daily_one_bad_symbol_does_not_abort_others() -> None:
    """单只失败只记 warning, 其余标的照常返回。"""
    client = FakeTdxClient(bars={"000001": [_bar(date(2026, 9, 11), 11.74)]}, fail=("bars",))
    df = _provider(client).get_daily(["600519.SH", "000001.SZ"])
    assert df.height == 0  # 两只都失败 → 空表, 但不抛异常


def test_get_daily_returns_typed_empty_frame() -> None:
    df = _provider(FakeTdxClient()).get_daily(["600519.SH"])
    assert df.height == 0
    assert "close" in df.columns


def test_iter_daily_streams_and_reports_progress() -> None:
    """全市场同步走 iter_daily, 必须分批产出且进度回调走到末尾。"""
    client = FakeTdxClient(bars={"600519": [_bar(date(2026, 9, 11), 1.0)],
                                 "000001": [_bar(date(2026, 9, 11), 2.0)]})
    seen: list[tuple[int, int]] = []
    frames = list(_provider(client).iter_daily(
        ["600519.SH", "000001.SZ"], on_chunk_done=lambda c, t: seen.append((c, t))
    ))
    assert len(frames) == 2
    assert seen[-1] == (2, 2)


def test_get_minute_datetime_is_beijing_wallclock_naive() -> None:
    """分钟 datetime 必须是北京墙钟 naive —— 带时区或 UTC 会让分时图全部落在时轴外。"""
    minute_bar = {"date": date(2026, 9, 11), "hour": 9, "minute": 35, "open": 1.0,
                  "high": 1.1, "low": 0.9, "close": 1.05, "volume": 10.0, "amount": 1000.0}
    client = FakeTdxClient(bars={"600519": [minute_bar]})
    df = _provider(client).get_minute(["600519.SH"], freq="1m")
    assert df.columns == MINUTE_COLS
    assert df.height == 1
    stamp = df["datetime"][0]
    assert stamp == datetime(2026, 9, 11, 9, 35)
    assert df.schema["datetime"].time_zone is None


def test_get_minute_rejects_unsupported_freq() -> None:
    client = FakeTdxClient(bars={"600519": [{"date": date(2026, 9, 11), "hour": 9, "minute": 35,
                                            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                                            "volume": 1.0, "amount": 1.0}]})
    df = _provider(client).get_minute(["600519.SH"], freq="15m")
    assert df.height == 0


def test_get_adj_factors_derives_from_xdxr() -> None:
    client = FakeTdxClient(
        bars={"600519": [_bar(date(2026, 6, 25), 1212.1)]},
        xdxr={"600519": [{"date": date(2026, 6, 26), "category": 1, "name": "除权除息",
                          "fenhong": 280.2423095703125, "songzhuangu": 0.0,
                          "peigu": 0.0, "peigujia": 0.0}]},
    )
    df = _provider(client).get_adj_factors(["600519.SH"])
    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert df.height == 1
    assert df["trade_date"][0] == date(2026, 6, 26)
    assert df["ex_factor"][0] == pytest.approx(1.0237, abs=1e-3)


def test_get_adj_factors_skips_events_without_prev_close() -> None:
    """缺前收盘时跳过该事件, 不产出猜出来的因子。"""
    client = FakeTdxClient(
        bars={"600519": []},
        xdxr={"600519": [{"date": date(2026, 6, 26), "category": 1, "name": "除权除息",
                          "fenhong": 1.0, "songzhuangu": 0.0, "peigu": 0.0, "peigujia": 0.0}]},
    )
    assert _provider(client).get_adj_factors(["600519.SH"]).height == 0


def _quote_row(code: str, market: int, price: float, prev: float, vol: float = 100.0) -> dict:
    return {"market": market, "code": code, "price": price, "last_close": prev,
            "open": price, "high": price, "low": price, "vol": vol, "cur_vol": 1.0,
            "amount": price * vol * 100, "s_vol": 1.0, "b_vol": 1.0,
            "bid_prices": [price] * 5, "ask_prices": [price + 0.01] * 5,
            "bid_volumes": [9.0] * 5, "ask_volumes": [14.0] * 5,
            "speed": 0.0, "active2": 0, "server_time_raw": 0}


def test_get_realtime_change_pct_is_decimal() -> None:
    """change_pct 契约是【小数制】: 涨 3.66% 必须是 0.0366, 不是 3.66。"""
    client = FakeTdxClient(quotes=[_quote_row("600519", 1, 103.66, 100.0)])
    p = _provider(client)
    p._universe_cache = ["600519.SH"]
    rows = p.get_realtime()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "600519.SH"
    assert row["change_pct"] == pytest.approx(0.0366)
    assert row["change_amount"] == pytest.approx(3.66)
    assert abs(row["change_pct"]) < 1, "change_pct 必须是小数制"


def test_get_realtime_volume_in_hands_and_none_fields_not_faked() -> None:
    client = FakeTdxClient(quotes=[_quote_row("600519", 1, 10.0, 10.0, vol=34801.0)])
    p = _provider(client)
    p._universe_cache = ["600519.SH"]
    row = p.get_realtime()[0]
    assert row["volume"] == pytest.approx(34801.0)
    # 通达信快照没有这些字段 → None, 不伪造
    for field in ("name", "amplitude", "turnover_rate", "session"):
        assert row[field] is None
    assert isinstance(row["timestamp"], int) and row["timestamp"] > 0


def test_get_realtime_soft_fails_to_empty_list() -> None:
    """契约: 快照失败必须软返回 [], 不能抛异常打断轮询线程。"""
    client = FakeTdxClient(fail=("quotes",))
    p = _provider(client)
    p._universe_cache = ["600519.SH"]
    assert p.get_realtime() == []


def test_get_realtime_empty_universe_returns_empty() -> None:
    p = _provider(FakeTdxClient())
    p._universe_cache = []
    assert p.get_realtime() == []


def test_get_realtime_indices_failure_returns_none() -> None:
    """指数快照失败返回 None(保留上轮缓存), 与 [] 语义区分。"""
    client = FakeTdxClient(fail=("quotes",))
    assert _provider(client).get_realtime_indices(["000001.SH"]) is None


def test_get_realtime_indices_empty_symbols_returns_empty() -> None:
    assert _provider(FakeTdxClient()).get_realtime_indices([]) == []


def test_get_realtime_indices_success_shape() -> None:
    client = FakeTdxClient(quotes=[_quote_row("000001", 1, 3888.11, 3934.4)])
    rows = _provider(client).get_realtime_indices(["000001.SH"])
    assert rows is not None and len(rows) == 1
    assert rows[0]["symbol"] == "000001.SH"
    assert rows[0]["change_pct"] == pytest.approx((3888.11 - 3934.4) / 3934.4)


def test_get_depth_batch_contract_shape() -> None:
    client = FakeTdxClient(quotes=[_quote_row("600519", 1, 1275.16, 1285.13)])
    book = _provider(client).get_depth_batch(["600519.SH"])
    assert set(book) == {"600519.SH"}
    entry = book["600519.SH"]
    assert set(entry) == {"bid_prices", "bid_volumes", "ask_prices", "ask_volumes", "timestamp"}
    assert len(entry["bid_prices"]) == 5
    assert entry["bid_volumes"][0] == pytest.approx(9.0)
    assert entry["timestamp"] > 0


def test_get_depth_batch_soft_failure_returns_empty_dict() -> None:
    client = FakeTdxClient(fail=("quotes",))
    assert _provider(client).get_depth_batch(["600519.SH"]) == {}


def test_test_dataset_reports_unsupported_dataset() -> None:
    res = _provider(FakeTdxClient()).test_dataset("financial", ["600519.SH"])
    assert res["rows"] == 0
    assert "financial" in res["error"]


def test_test_dataset_reports_failure_not_raises() -> None:
    client = FakeTdxClient(fail=("bars",))
    res = _provider(client).test_dataset("daily", ["600519.SH"])
    assert res["error"]
    assert res["rows"] == 0


def test_get_instruments_not_claimed() -> None:
    """维表字段不全, 不声明 instruments 能力, 返回空列表。"""
    assert _provider(FakeTdxClient()).get_instruments() == []


# ---------------------------------------------------------------------------
# loader 集成
# ---------------------------------------------------------------------------

def test_plugin_manifest_registers_tdx() -> None:
    """plugin.yaml 必须被 loader 正确解析并注册。"""
    from app.data_providers import custom as custom_sources

    plugins = {p["name"]: p for p in custom_sources.list_plugins()}
    assert "tdx" in plugins, "tdx 插件未被发现"
    manifest = plugins["tdx"]
    assert manifest["display_name"] == "通达信"
    assert set(manifest["datasets"]) == {
        "daily", "adj_factor", "minute", "full_minute", "realtime", "depth5", "financial"
    }
    assert plugins["tdx"]["available"] is True, (
        f"tdx 插件不可用: {plugins['tdx'].get('status')}"
    )


def test_provider_has_dataset_routing() -> None:
    """路由判据: 声明了才走插件。financial 已声明(仅 shares 表)。"""
    from app.data_providers import custom as custom_sources

    for ds in ("daily", "adj_factor", "minute", "full_minute", "realtime", "depth5", "financial"):
        assert custom_sources.provider_has_dataset("tdx", ds) is True


def test_get_financials_only_supports_shares() -> None:
    """三大报表通达信不提供: 非 shares 表返回空表, 不抛异常。"""
    df = _provider(FakeTdxClient()).get_financials("income", ["600519.SH"])
    assert df.is_empty()


def test_get_financials_shares_anchors_absolute_and_chains_songzhuan() -> None:
    """绝对股本事件定锚 + category 1 送转走乘数链; 通达信单位为万股, 需 ×1e4。"""
    client = FakeTdxClient(xdxr={"600519": [
        {"date": date(2020, 6, 30), "category": 5, "name": "股本变化",
         "qianzongguben": 1000.0, "houzongguben": 2000.0,
         "panqianliutong": 900.0, "panhouliutong": 1800.0},
        {"date": date(2021, 6, 30), "category": 1, "name": "除权除息",
         "fenhong": 0.0, "songzhuangu": 10.0, "peigu": 0.0, "peigujia": 0.0},
    ]})
    df = _provider(client).get_financials("shares", ["600519.SH"]).sort("period_end")
    assert df.columns == ["symbol", "period_end", "announce_date", "total_shares", "float_shares"]
    got = {str(r["period_end"]): (r["total_shares"], r["float_shares"]) for r in df.to_dicts()}
    # 首个绝对事件之前: 取该事件的"前"值 (1000 万股 → 1e7 股)
    assert got["1990-01-01"] == (1e7, 9e6)
    # 事件当日起: 取"后"值 (2000 万股 → 2e7 股)
    assert got["2020-06-30"] == (2e7, 1.8e7)
    # 10 送 10 → ×2 (没有配对的绝对股本事件, 只能靠乘数链)
    assert got["2021-06-30"] == (4e7, 3.6e7)


def test_get_financials_shares_without_anchor_is_empty() -> None:
    """只有 category 1 事件、没有任何绝对股本锚点时返回空, 交给平台推定口径。"""
    client = FakeTdxClient(xdxr={"600519": [
        {"date": date(2021, 6, 30), "category": 1, "name": "除权除息",
         "fenhong": 0.0, "songzhuangu": 10.0, "peigu": 0.0, "peigujia": 0.0},
    ]})
    assert _provider(client).get_financials("shares", ["600519.SH"]).is_empty()


def _ex_div_event(day: date, song: float = 0.0, pei: float = 0.0) -> dict:
    return {"date": day, "category": 1, "name": "除权除息",
            "fenhong": 0.0, "songzhuangu": song, "peigu": pei, "peigujia": 0.0}


def _absolute_event(day: date, qian: float, hou: float, pqian: float, phou: float,
                    category: int = 2) -> dict:
    return {"date": day, "category": category, "name": "送配股上市",
            "qianzongguben": qian, "houzongguben": hou,
            "panqianliutong": pqian, "panhouliutong": phou}


def test_shares_seed_skips_songzhuan_paired_with_absolute_event() -> None:
    """与绝对事件配对的送转不得反除: 基准 = 锚点前值 (实测 600519 的 IPO 2.5 亿股)。

    形态: 除权除息日 10 送 1 → 次日送股上市, 且 后/前 总股本恰好 = 1.1。
    锚点的 qian 已是"送转生效前"的股本, 再反除一次会得到 2.2727 亿 (错)。
    """
    client = FakeTdxClient(xdxr={"600519": [
        _ex_div_event(date(2002, 7, 25), song=1.0),
        _absolute_event(date(2002, 7, 26), 25000.0, 27500.0, 7150.0, 7865.0),
    ]})
    df = _provider(client).get_financials("shares", ["600519.SH"]).sort("period_end")
    got = {str(r["period_end"]): (r["total_shares"], r["float_shares"]) for r in df.to_dicts()}

    assert got["1990-01-01"] == (2.5e8, 7.15e7)      # 不再被 1.1 反除成 2.2727e8
    # 除权日照平台惯例按"后值"生效(与后续各次除权日一致, 市值在除权日连续)
    assert got["2002-07-25"] == (2.75e8, 7.865e7)
    assert got["2002-07-26"] == (2.75e8, 7.865e7)


def test_shares_seed_divides_songzhuan_without_paired_absolute_event() -> None:
    """没有配对绝对事件的历史送转仍要反除: 否则"上市 → 首次变动"这段基准偏大。"""
    client = FakeTdxClient(xdxr={"600519": [
        _ex_div_event(date(1995, 6, 1), song=10.0),                  # 10 送 10, 无配对事件
        _absolute_event(date(1998, 6, 1), 3000.0, 4000.0, 900.0, 1200.0),
    ]})
    df = _provider(client).get_financials("shares", ["600519.SH"]).sort("period_end")
    got = {str(r["period_end"]): (r["total_shares"], r["float_shares"]) for r in df.to_dicts()}

    assert got["1990-01-01"] == (1.5e7, 4.5e6)   # 3000 万股 / 2
    assert got["1995-06-01"] == (3e7, 9e6)       # 反除基准 x2 = 锚点前值
    assert got["1998-06-01"] == (4e7, 1.2e7)


def test_shares_seed_divides_only_unpaired_songzhuan_when_both_present() -> None:
    """同时存在配对与未配对送转时只反除未配对的那次(不能多除也不能少除)。"""
    client = FakeTdxClient(xdxr={"600519": [
        _ex_div_event(date(1995, 6, 1), song=10.0),                  # 未配对 → 反除
        _ex_div_event(date(2002, 7, 25), song=1.0),                  # 与锚点配对 → 不反除
        _absolute_event(date(2002, 7, 26), 25000.0, 27500.0, 7150.0, 7865.0),
    ]})
    df = _provider(client).get_financials("shares", ["600519.SH"]).sort("period_end")
    got = {str(r["period_end"]): (r["total_shares"], r["float_shares"]) for r in df.to_dicts()}

    assert got["1990-01-01"] == (1.25e8, 3.575e7)   # 2.5e8 / 2 (只除 1995 那次)
    assert got["1995-06-01"] == (2.5e8, 7.15e7)     # 1995 送转后 = 锚点前值
    assert got["2002-07-25"] == (2.75e8, 7.865e7)
    assert got["2002-07-26"] == (2.75e8, 7.865e7)


def test_shares_seed_divides_when_paired_ratio_does_not_match() -> None:
    """窗口内配对判据必须看倍数: 只挨得近但倍数不符的另一笔动作不构成配对。"""
    client = FakeTdxClient(xdxr={"600519": [
        _ex_div_event(date(2000, 1, 3), song=3.0),                   # 10 送 3 → x1.3
        _absolute_event(date(2000, 1, 5), 1000.0, 2000.0, 300.0, 600.0),  # x2, 非同一笔
    ]})
    df = _provider(client).get_financials("shares", ["600519.SH"]).sort("period_end")
    got = {str(r["period_end"]): (r["total_shares"], r["float_shares"]) for r in df.to_dicts()}

    assert got["1990-01-01"][0] == pytest.approx(1e7 / 1.3)   # 仍反除 x1.3
    assert got["2000-01-03"][0] == pytest.approx(1e7)         # 除权日 = 送转前值 x1.3
    assert got["2000-01-05"][0] == pytest.approx(2e7)         # 绝对事件后值
