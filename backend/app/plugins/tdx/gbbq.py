"""本地通达信 gbbq(股本变迁)文件读取 —— 离线 / 首刷股本来源。

通达信客户端的 `T0002/hq_cache/gbbq` 与主站 `get_xdxr_info` 是**同一份**股本变迁
数据, 只是本地这份按客户端版本用白盒密钥表加密。读取本地文件的价值:

- 离线可用: 主站池全部失效时仍能拿到股本, 不必逐标的联网(全市场约 4 分钟);
- 首刷更快: 一次解密全市场(本机实测约 20 秒), 覆盖与在线逐标的同步等价;
- 覆盖北交所: 本地文件用**市场号 2** 承载 920xxx, 而在线请求把 `.BJ` 映射到市场 0
  (见 client.MARKET_OF_SUFFIX), 主站对市场 0 的北交所代码返回空事件 ——
  因此本地文件能补上在线路径的北交所缺口。

记录口径(对真实文件实测, 与主站 xdxr 报文同形): 每条 29 字节 =
市场(1) + 代码(6) + 保留(1) + 日期(4) + 类别(1) + 4 个 float32(16); 前 24 字节为密文,
后 5 字节明文; 文件头 4 字节为记录数(uint32 LE)。4 个 float 槽位与线上同序, 但本地
是**原始 IEEE float32**, 线上是压缩浮点:

    category 1            -> 分红 / 配股价 / 送转股 / 配股   (每 10 股口径)
    category 11/12        -> 第 3 槽 = 缩股比例
    category 13/14        -> 不使用
    其它(2/3/5/6/7/8/...) -> 前流通 / 前总股本 / 后流通 / 后总股本 (万股)

绝对股本单位是**万股**, 与在线 xdxr 一致, 由 provider 侧统一乘 1e4 换算成股。
"""
from __future__ import annotations

import logging
import os
import string
import struct
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app.plugins.tdx import protocol as proto
from app.plugins.tdx.gbbq_keys import KEYS_HEX

logger = logging.getLogger(__name__)

_RECORD_SIZE = 29
_HEADER_SIZE = 4
_ENCRYPTED_SIZE = 24
_BLOCKS = _ENCRYPTED_SIZE // 8
_MASK32 = 0xFFFFFFFF

# 密钥表(见 gbbq_keys 的说明): 解密是 16 轮 Feistel, 每轮取 4 个字节各查一次表。
# 查表用到 0xC48 + 256 项为止, 长度不足会越界取到别的字节 → 解出静默错误的股本,
# 因此这里导入即自检: 表不完整就让插件加载失败(loader 记为不可用), 不产生错误数据。
_KEYS = struct.unpack(f"<{len(KEYS_HEX) // 8}I", bytes.fromhex(KEYS_HEX))
_T0 = 0x448 // 4
_T1 = 0x48 // 4
_T2 = 0x848 // 4
_T3 = 0xC48 // 4
if len(_KEYS) < _T3 + 256:
    raise RuntimeError(
        f"gbbq 密钥表长度不足: {len(_KEYS)} 项(uint32), 至少需要 {_T3 + 256} 项; "
        "换客户端版本后需按 gbbq_keys.py 的说明重新提取"
    )
_K_FINAL = _KEYS[0]
_K_INIT = _KEYS[0x44 // 4]
# 16 轮, 按解密顺序(偏移 0x40 递减到 0x04)
_ROUND_KEYS = tuple(_KEYS[j // 4] for j in range(0x40, 0, -4))

# 本地 gbbq 的市场号: 0 深 / 1 沪 / 2 北。与 client.MARKET_OF_SUFFIX(.BJ -> 0) 不同,
# 后者的 0 是"在线协议里的市场号", 本地文件对北交所用的是 2。
LOCAL_MARKET_OF_SUFFIX = {"SZ": 0, "SH": 1, "BJ": 2}

# 客户端未运行超过该天数, 本地文件就可能漏掉新的送转/解禁事件 → 回退在线。
# 依据: gbbq 是全市场事件文件, 每个交易日都有新的分红送转公告落进来, 因此
# "文件里最新事件日"与"文件快照日"基本同步; 5 天足以覆盖长假 + 客户端未开。
MAX_EVENT_AGE_DAYS = 5

_ENV_VARS = ("TDX_HOME", "TDX_DATA_DIR")
# 常见安装目录名(小写比较), 仅用于零配置定位本机通达信; 不含任何机器绝对路径。
_CANDIDATE_DIRS = frozenset({"new_tdx", "tradetool", "tdx", "tdxw", "zd_web", "通达信"})
_GBBQ_RELATIVE = ("T0002/hq_cache/gbbq", "hq_cache/gbbq", "gbbq")


@dataclass(frozen=True)
class GbbqSnapshot:
    """一次解密后的本地股本变迁快照。"""

    path: Path
    mtime: float
    record_count: int
    events: dict[tuple[int, str], tuple[dict, ...]]
    newest_event: date | None
    unparsable: int = 0


def local_market_of(symbol: str) -> tuple[int, str]:
    """`920002.BJ` -> (2, '920002')。本地 gbbq 对北交所用市场号 2。"""
    code, _, suffix = symbol.partition(".")
    suffix = suffix.upper()
    market = LOCAL_MARKET_OF_SUFFIX.get(suffix)
    if not code or market is None:
        raise ValueError(f"无法识别的标的代码: {symbol!r}")
    return market, code


def events_for(snapshot: GbbqSnapshot, symbol: str) -> tuple[dict, ...]:
    """取某标的的本地事件(结构与在线 xdxr 一致); 无覆盖返回空元组。"""
    try:
        market, code = local_market_of(symbol)
    except ValueError:
        return ()
    return snapshot.events.get((market, code), ())


def is_fresh(snapshot: GbbqSnapshot, today: date | None = None) -> bool:
    """最新事件是否足够新。过期文件会漏掉新送转, 调用方必须回退在线来源。"""
    if snapshot.newest_event is None:
        return False
    return snapshot.newest_event >= (today or date.today()) - timedelta(days=MAX_EVENT_AGE_DAYS)


def _decrypt_record(raw: bytes) -> bytes:
    """一条 29 字节记录 -> 29 字节明文(前 24 字节解密, 后 5 字节原样)。"""
    out = bytearray(_RECORD_SIZE)
    for block in range(_BLOCKS):
        off = block * 8
        num = _K_INIT ^ int.from_bytes(raw[off : off + 4], "little")
        numold = int.from_bytes(raw[off + 4 : off + 8], "little")
        for key in _ROUND_KEYS:
            eax = _KEYS[_T0 + ((num >> 16) & 0xFF)]
            eax = (eax + _KEYS[_T1 + (num >> 24)]) & _MASK32
            eax ^= _KEYS[_T2 + ((num >> 8) & 0xFF)]
            eax = (eax + _KEYS[_T3 + (num & 0xFF)]) & _MASK32
            eax ^= key
            num, numold = numold ^ eax, num
        struct.pack_into("<II", out, off, numold ^ _K_FINAL, num)
    out[_ENCRYPTED_SIZE:] = raw[_ENCRYPTED_SIZE:]
    return bytes(out)


def _parse_event(plain: bytes) -> tuple[int, str, dict] | None:
    """29 字节明文 -> (市场号, 代码, 事件)。字段非法时返回 None(单条跳过, 不猜)。"""
    market, code_raw, day, category, f1, f2, f3, f4 = struct.unpack("<B7sIBffff", plain)
    try:
        effective = date(day // 10000, (day // 100) % 100, day % 100)
        code = code_raw.rstrip(b"\x00").decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    if not code:
        return None

    row: dict = {
        "date": effective,
        "category": category,
        "name": proto.XDXR_CATEGORY_NAMES.get(category, str(category)),
        "fenhong": None, "peigujia": None, "songzhuangu": None, "peigu": None,
        "suogu": None, "panqianliutong": None, "panhouliutong": None,
        "qianzongguben": None, "houzongguben": None,
    }
    if category == 1:
        row.update(fenhong=f1, peigujia=f2, songzhuangu=f3, peigu=f4)
    elif category in (11, 12):
        row["suogu"] = f3
    elif category in (13, 14):
        pass
    else:
        row.update(panqianliutong=f1, qianzongguben=f2, panhouliutong=f3, houzongguben=f4)
    return market, code, row


def load_gbbq(path: Path) -> GbbqSnapshot | None:
    """读取并解密整个 gbbq 文件。文件缺失/过短/零记录时返回 None(fail-closed)。

    头部声明的记录数超过文件实际可容纳条数时, 只按可用部分解析并记 warning:
    客户端正在写文件时会短暂出现这种半截状态, 已解析部分仍然可用。
    """
    try:
        data = path.read_bytes()
        mtime = path.stat().st_mtime
    except OSError as e:
        logger.warning("本地 gbbq 读取失败 %s: %s", path, e)
        return None
    if len(data) <= _HEADER_SIZE:
        logger.warning("本地 gbbq 过短(%d 字节), 视为不可用: %s", len(data), path)
        return None

    (declared,) = struct.unpack_from("<I", data, 0)
    capacity = (len(data) - _HEADER_SIZE) // _RECORD_SIZE
    count = min(declared, capacity)
    if declared > capacity:
        logger.warning(
            "本地 gbbq 头部声明 %d 条, 文件仅容纳 %d 条(可能正在写入), 按可用部分解析: %s",
            declared, capacity, path,
        )

    events: dict[tuple[int, str], list[dict]] = {}
    newest: date | None = None
    unparsable = 0
    pos = _HEADER_SIZE
    for _ in range(count):
        parsed = _parse_event(_decrypt_record(data[pos : pos + _RECORD_SIZE]))
        pos += _RECORD_SIZE
        if parsed is None:
            unparsable += 1
            continue
        market, code, row = parsed
        events.setdefault((market, code), []).append(row)
        if newest is None or row["date"] > newest:
            newest = row["date"]

    if not events:
        logger.warning("本地 gbbq 未解析出任何记录, 视为不可用: %s", path)
        return None
    if unparsable:
        logger.info("本地 gbbq 跳过 %d 条字段非法的记录: %s", unparsable, path)
    return GbbqSnapshot(
        path=path,
        mtime=mtime,
        record_count=count,
        events={key: tuple(rows) for key, rows in events.items()},
        newest_event=newest,
        unparsable=unparsable,
    )


def _gbbq_in(root: Path) -> Path | None:
    """在给定根目录下按已知相对路径找 gbbq。"""
    for rel in _GBBQ_RELATIVE:
        candidate = root / rel
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _candidate_roots() -> list[Path]:
    """盘根目录: Windows 逐盘探测, 其它平台只看 /。"""
    if os.name != "nt":
        return [Path("/")]
    roots: list[Path] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if root.exists():
                roots.append(root)
        except OSError:
            continue
    return roots


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def find_local_gbbq() -> Path | None:
    """定位本机通达信的 gbbq; 找不到返回 None。

    顺序: 环境变量 `TDX_HOME` / `TDX_DATA_DIR`(可指向安装目录、T0002 目录或 gbbq
    文件本身; 命中即不再探测) → 各盘根目录下常见安装目录名。装了多个版本时取
    gbbq 最新的那个(旧安装的数据可能停在几年前, 取错会静默用过期股本)。
    """
    for var in _ENV_VARS:
        raw = (os.environ.get(var) or "").strip()
        if not raw:
            continue
        root = Path(raw)
        found = _gbbq_in(root)
        if found is None and root.is_file():
            found = root
        if found is not None:
            return found

    candidates: list[Path] = []
    for root in _candidate_roots():
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir() or entry.name.lower() not in _CANDIDATE_DIRS:
                    continue
            except OSError:
                continue
            found = _gbbq_in(Path(entry.path))
            if found is not None:
                candidates.append(found)

    if not candidates:
        return None
    if len(candidates) > 1:
        candidates.sort(key=_safe_mtime, reverse=True)
        logger.info("发现多个本地 gbbq, 取最新的: %s", candidates[0])
    return candidates[0]
