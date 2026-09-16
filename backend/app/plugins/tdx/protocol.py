"""通达信(通达信行情主站)二进制协议 —— 报文构造与响应解析。

只实现本项目需要的数据集所对应的指令, 不引入任何第三方依赖:

  - 日K / 分钟K      (get_security_bars)
  - 指数日K/分钟K    (与证券K线同一指令, 响应每条多 4 字节涨跌家数)
  - 除权除息/股本变化 (get_xdxr_info) → adj_factor 推导来源
  - 批量实时快照      (get_security_quotes, 含五档) → realtime / depth5

协议要点(实测确认, 非文档推测):

1. 响应 = 0x10 字节头 `<IIIHH` = (前缀, 前缀, 前缀, 压缩长度, 解压长度) +
   压缩长度字节的 body。压缩长度 == 解压长度时 body 未压缩, 否则 zlib。
   实测小批量不压缩、大批量(≈40 只以上)才压缩。
2. K线价格是 **增量编码**: 每根的开盘是基于上一根"开+收"的增量,
   且原始值需 **除以 1000**; 快照价格原始值需 **除以 100**(基金/ETF 为 1000)。
3. 成交量/成交额用同一套压缩浮点编码(见 decompress_float), K线的成交量
   口径为「手」, 与项目契约一致, 无需再换算。
4. 单次批量快照硬上限 **80 只**(实测请求 500 只只回 80 条)。
5. 退市/长期停牌标的在快照响应里是 **短记录**。按定额读取会越界,
   因此解析必须按实际可读长度做边界保护, 绝不能因为一条坏记录丢掉整批。

报文格式来源: 通达信行情协议的反向工程成果(社区公开), 本文件为独立实现。
"""
from __future__ import annotations

import struct
import zlib
from datetime import date, datetime

# ---------------------------------------------------------------------------
# 握手: 建连后必须顺序发送这三条, 否则后续指令被主站静默丢弃
# ---------------------------------------------------------------------------
SETUP_PACKETS: tuple[bytes, ...] = (
    bytes.fromhex("0c 02 18 93 00 01 03 00 03 00 0d 00 01"),
    bytes.fromhex("0c 02 18 94 00 01 03 00 03 00 0d 00 02"),
    bytes.fromhex(
        "0c 03 18 99 00 01 20 00 20 00 db 0f d5"
        "d0 c9 cc d6 a4 a8 af 00 00 00 8f c2 25"
        "40 13 00 00 d5 00 c9 cc bd f0 d7 ea 00"
        "00 00 02"
    ),
)

RSP_HEADER_LEN = 0x10

# 单次批量快照的协议上限
MAX_QUOTE_BATCH = 80

# K线 category: 0=5分钟 1=15分钟 2=30分钟 3=1小时 4=日线 5=周线 6=月线 7=1分钟 9=日线
CAT_5MIN = 0
CAT_15MIN = 1
CAT_30MIN = 2
CAT_60MIN = 3
CAT_DAY = 9
CAT_1MIN = 7

# 一分钟/五分钟等分钟周期的 category(<4 或 ==7/8) 用 "压缩日 + 当日分钟数" 编码日期
_MINUTE_CATEGORIES = {0, 1, 2, 3, 7, 8}


# ---------------------------------------------------------------------------
# 基础解码
# ---------------------------------------------------------------------------

def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """解码通达信变长有符号整数, 返回 (值, 新位置)。

    布局类似 UTF-8: 首字节 bit6 = 符号位, bit7 = 续读标志,
    低 6 位为最低有效段, 后续每字节贡献 7 位。
    """
    first = data[pos]
    value = first & 0x3F
    negative = bool(first & 0x40)
    shift = 6

    if first & 0x80:
        while True:
            pos += 1
            byte = data[pos]
            value += (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break

    pos += 1
    return (-value if negative else value), pos


def decompress_float(raw: int) -> float:
    """解码通达信压缩浮点(成交量/成交额的线上编码)。

    这是主站为省带宽用的自定义格式, 把一个近似 IEEE754 的值拆成
    指数段与三组尾数段分开传输; 这里按位重建。
    """
    if raw == 0:
        return 0.0

    sign_exp = (raw >> 24) & 0xFF          # 第 3 字节 = 指数段
    hi = (raw >> 16) & 0xFF                # 第 2 字节 = 高位尾数
    mid = (raw >> 8) & 0xFF                # 第 1 字节 = 中位尾数
    lo = raw & 0xFF                        # 第 0 字节 = 低位尾数

    ecx = sign_exp * 2 - 0x7F
    edx = sign_exp * 2 - 0x86
    esi = sign_exp * 2 - 0x8E
    eax = sign_exp * 2 - 0x96

    base = 2.0 ** abs(ecx)
    total = 1.0 / base if ecx < 0 else base

    if hi > 0x80:
        total += 2.0 ** (edx + 1) * (hi & 0x7F) + 2.0 ** edx * 128.0
    elif edx >= 0:
        total += 2.0 ** edx * hi
    else:
        total += (1.0 / 2.0 ** edx) * hi

    part_mid = 2.0 ** esi * mid
    part_lo = 2.0 ** eax * lo
    if hi & 0x80:
        part_mid *= 2.0
        part_lo *= 2.0

    return total + part_mid + part_lo


def decode_bar_datetime(data: bytes, pos: int, category: int) -> tuple[date, int, int, int]:
    """解码 K线时间, 返回 (日期, 时, 分, 新位置)。

    分钟周期用 "压缩日(uint16) + 当日分钟数(uint16)"; 日线及以上用 uint32 YYYYMMDD。
    """
    if category in _MINUTE_CATEGORIES:
        zipday, minutes = struct.unpack_from("<HH", data, pos)
        year = (zipday >> 11) + 2004
        month = (zipday % 2048) // 100
        day = (zipday % 2048) % 100
        return date(year, month, day), minutes // 60, minutes % 60, pos + 4

    (zipday,) = struct.unpack_from("<I", data, pos)
    return (
        date(zipday // 10000, (zipday % 10000) // 100, zipday % 100),
        15,
        0,
        pos + 4,
    )


# ---------------------------------------------------------------------------
# 报文构造
# ---------------------------------------------------------------------------

def _code_bytes(code: str) -> bytes:
    raw = code.encode("utf-8")
    if len(raw) > 6:
        raise ValueError(f"通达信证券代码最长 6 字节: {code!r}")
    return raw.ljust(6, b"\x00")


def bars_packet(category: int, market: int, code: str, start: int, count: int) -> bytes:
    """K线请求。日K/指数K线共用同一指令(响应结构差异在解析侧处理)。"""
    return struct.pack(
        "<HIHHHH6sHHHHIIH",
        0x10C, 0x01016408, 0x1C, 0x1C, 0x052D,
        market, _code_bytes(code), category, 1, start, count, 0, 0, 0,
    )


def quotes_packet(symbols: list[tuple[int, str]]) -> bytes:
    """批量快照请求。symbols 为 (market, code); 单次不得超过 MAX_QUOTE_BATCH。"""
    count = len(symbols)
    if count == 0:
        raise ValueError("快照请求至少需要 1 只标的")
    payload_len = count * 7 + 12
    header = struct.pack(
        "<HIHHIIHH",
        0x10C, 0x02006320, payload_len, payload_len, 0x5053E, 0, 0, count,
    )
    body = b"".join(struct.pack("<B6s", market, _code_bytes(code)) for market, code in symbols)
    return header + body


def xdxr_packet(market: int, code: str) -> bytes:
    """除权除息/股本变化请求。"""
    return bytes.fromhex("0c 1f 18 76 00 01 0b 00 0b 00 0f 00 01 00") + struct.pack(
        "<B6s", market, _code_bytes(code)
    )


# ---------------------------------------------------------------------------
# 响应拆包
# ---------------------------------------------------------------------------

def split_response(head: bytes, body_raw: bytes) -> bytes:
    """按响应头解出 body 明文(必要时 zlib 解压)。"""
    if len(head) != RSP_HEADER_LEN:
        raise ValueError(f"响应头长度异常: {len(head)}")
    _, _, _, zipped, unzipped = struct.unpack("<IIIHH", head)
    if zipped == unzipped:
        return body_raw
    return zlib.decompress(body_raw)


def read_sizes(head: bytes) -> tuple[int, int]:
    """返回 (压缩长度, 解压长度)。"""
    _, _, _, zipped, unzipped = struct.unpack("<IIIHH", head)
    return zipped, unzipped


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

def parse_bars(body: bytes, category: int, *, is_index: bool = False) -> list[dict]:
    """解析K线响应。

    价格增量解码: 每根K线的开/收都基于上一根"开+收"作基准; 指数记录在
    量额之后多 4 字节涨跌家数, 必须跳过否则整段错位(这正是拿证券解析器
    读指数会得到乱码日期的原因)。

    单条记录不足时立即停止并保留已解析部分 —— 绝不让一条坏记录吞掉整批。
    """
    if len(body) < 2:
        return []
    (count,) = struct.unpack_from("<H", body, 0)
    pos = 2
    rows: list[dict] = []
    base = 0

    for _ in range(count):
        try:
            day, hour, minute, pos = decode_bar_datetime(body, pos, category)
            open_diff, pos = decode_varint(body, pos)
            close_diff, pos = decode_varint(body, pos)
            high_diff, pos = decode_varint(body, pos)
            low_diff, pos = decode_varint(body, pos)

            (vol_raw,) = struct.unpack_from("<I", body, pos)
            (amount_raw,) = struct.unpack_from("<I", body, pos + 4)
            pos += 8

            if is_index:
                pos += 4  # up_count / down_count

            open_raw = base + open_diff
            raw = {
                "date": day,
                "hour": hour,
                "minute": minute,
                "open": open_raw / 1000.0,
                "close": (open_raw + close_diff) / 1000.0,
                "high": (open_raw + high_diff) / 1000.0,
                "low": (open_raw + low_diff) / 1000.0,
                "volume": decompress_float(vol_raw),
                "amount": decompress_float(amount_raw),
            }
            base = open_raw + close_diff
        except (struct.error, IndexError, ValueError):
            break

        rows.append(raw)

    return rows


def _quote_price(body: bytes, pos: int, base: int, divisor: float) -> tuple[float, int]:
    """读一个"基准 + 增量"的价格字段, 返回 (价格, 新位置)。

    独立成函数而非内层闭包: 闭包会捕获循环变量, 属于易错的隐式绑定。
    """
    diff, new_pos = decode_varint(body, pos)
    return (base + diff) / divisor, new_pos


def parse_quotes(body: bytes, *, price_divisor_of=None) -> list[dict]:
    """解析批量快照响应(含五档)。

    价格同样是"基准价 + 增量", 基准为该股最新价; 原始值除以 100 得到价格,
    但基金/ETF 需除以 1000(实测 ETF 按 100 解析会整体放大 10 倍)。

    注意: 快照 body 前 2 字节是前缀, **条数在 offset 2**(K线响应的条数在 offset 0),
    记录从 offset 4 开始 —— 与 K线不同, 不要套用同一套偏移。

    price_divisor_of 接收 (market, code) 返回该标的除数, 缺省一律 100。
    """
    if len(body) < 4:
        return []
    (count,) = struct.unpack_from("<H", body, 2)
    pos = 4
    rows: list[dict] = []

    for _ in range(count):
        try:
            market, code_raw, _active1 = struct.unpack_from("<B6sH", body, pos)
            pos += 9
            code = code_raw.rstrip(b"\x00").decode("utf-8", errors="replace")

            raw_price, pos = decode_varint(body, pos)
            divisor = 100.0
            if price_divisor_of is not None:
                divisor = price_divisor_of(market, code)

            last_close, pos = _quote_price(body, pos, raw_price, divisor)
            open_, pos = _quote_price(body, pos, raw_price, divisor)
            high, pos = _quote_price(body, pos, raw_price, divisor)
            low, pos = _quote_price(body, pos, raw_price, divisor)
            _reversed0, pos = decode_varint(body, pos)
            _reversed1, pos = decode_varint(body, pos)
            vol, pos = decode_varint(body, pos)
            cur_vol, pos = decode_varint(body, pos)
            (amount_raw,) = struct.unpack_from("<I", body, pos)
            pos += 4
            s_vol, pos = decode_varint(body, pos)
            b_vol, pos = decode_varint(body, pos)
            _reversed2, pos = decode_varint(body, pos)
            _reversed3, pos = decode_varint(body, pos)

            bids: list[float] = []
            asks: list[float] = []
            bid_vols: list[float] = []
            ask_vols: list[float] = []
            for _level in range(5):
                bid, pos = _quote_price(body, pos, raw_price, divisor)
                ask, pos = _quote_price(body, pos, raw_price, divisor)
                bid_vol, pos = decode_varint(body, pos)
                ask_vol, pos = decode_varint(body, pos)
                bids.append(bid)
                asks.append(ask)
                bid_vols.append(bid_vol)
                ask_vols.append(ask_vol)

            (reversed4,) = struct.unpack_from("<H", body, pos)
            pos += 2
            _reversed5, pos = decode_varint(body, pos)
            _reversed6, pos = decode_varint(body, pos)
            _reversed7, pos = decode_varint(body, pos)
            _reversed8, pos = decode_varint(body, pos)
            (reversed9, active2) = struct.unpack_from("<hH", body, pos)
            pos += 4
        except (struct.error, IndexError, ValueError):
            break

        rows.append({
            "market": market,
            "code": code,
            "price": raw_price / divisor,
            "last_close": last_close,
            "open": open_,
            "high": high,
            "low": low,
            "vol": float(vol),
            "cur_vol": float(cur_vol),
            "amount": decompress_float(amount_raw),
            "s_vol": float(s_vol),
            "b_vol": float(b_vol),
            "bid_prices": bids,
            "ask_prices": asks,
            "bid_volumes": bid_vols,
            "ask_volumes": ask_vols,
            "speed": reversed9 / 100.0,
            "active2": active2,
            "server_time_raw": reversed4,
        })

    return rows


XDXR_CATEGORY_NAMES = {
    1: "除权除息",
    2: "送配股上市",
    3: "非流通股上市",
    4: "未知股本变动",
    5: "股本变化",
    6: "增发新股",
    7: "股份回购",
    8: "增发新股上市",
    9: "转配股上市",
    10: "可转债上市",
    11: "扩缩股",
    12: "非流通股缩股",
    13: "送认购权证",
    14: "送认沽权证",
}


def parse_xdxr(body: bytes) -> list[dict]:
    """解析除权除息 / 股本变化。

    每条约 37 字节: 市场(1) + 代码(6) + 保留(1) + 日期(4) + 类别(1) + 数据(16)。
    category==1 的 16 字节是四个 float32: 分红 / 配股价 / 送转股 / 配股。
    """
    if len(body) < 11:
        return []
    pos = 9
    (count,) = struct.unpack_from("<H", body, pos)
    pos += 2
    rows: list[dict] = []

    for _ in range(count):
        try:
            pos += 7  # 市场 + 代码
            pos += 1  # 保留
            day, _hour, _minute, pos = decode_bar_datetime(body, pos, CAT_DAY)
            (category,) = struct.unpack_from("<B", body, pos)
            pos += 1
            payload = body[pos:pos + 16]
            if len(payload) < 16:
                break
            pos += 16

            row: dict = {"date": day, "category": category,
                         "name": XDXR_CATEGORY_NAMES.get(category, str(category)),
                         "fenhong": None, "peigujia": None, "songzhuangu": None,
                         "peigu": None, "suogu": None, "panqianliutong": None,
                         "panhouliutong": None, "qianzongguben": None, "houzongguben": None}

            if category == 1:
                fenhong, peigujia, songzhuangu, peigu = struct.unpack("<ffff", payload)
                row.update(fenhong=fenhong, peigujia=peigujia,
                           songzhuangu=songzhuangu, peigu=peigu)
            elif category in (11, 12):
                _, _, suogu, _ = struct.unpack("<IIfI", payload)
                row["suogu"] = suogu
            elif category in (13, 14):
                _, _, _, _ = struct.unpack("<fIfI", payload)
            else:
                qian, qianzong, hou, houzong = struct.unpack("<IIII", payload)
                row.update(panqianliutong=decompress_float(qian),
                           panhouliutong=decompress_float(hou),
                           qianzongguben=decompress_float(qianzong),
                           houzongguben=decompress_float(houzong))
        except (struct.error, IndexError, ValueError):
            break

        rows.append(row)

    return rows


def ex_factor_from_event(event: dict, prev_close: float) -> float | None:
    """由单条除权除息事件推导除权因子 ex_factor(交易所口径)。

       除权价 = (前收盘 - 每股分红 + 每股配股数 * 配股价)
                / (1 + 每股送转股数 + 每股配股数)

    上游 fenhong / songzhuangu / peigu 均为 **每 10 股** 口径, 需先除以 10。
    前收盘缺失或 <=0 时返回 None(fail-closed, 不猜)。
    """
    if event.get("category") != 1 or not prev_close or prev_close <= 0:
        return None

    fenhong = (event.get("fenhong") or 0.0) / 10.0
    songzhuangu = (event.get("songzhuangu") or 0.0) / 10.0
    peigu = (event.get("peigu") or 0.0) / 10.0
    peigujia = event.get("peigujia") or 0.0

    denominator = 1.0 + songzhuangu + peigu
    if denominator <= 0:
        return None

    ex_rights_price = (prev_close - fenhong + peigu * peigujia) / denominator
    if ex_rights_price <= 0:
        return None

    factor = prev_close / ex_rights_price
    if not (0.0 < factor < 10.0):
        return None
    return factor


def now_beijing() -> datetime:
    """北京时间墙钟(naive) —— 分钟K的 datetime 契约。"""
    from datetime import timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
