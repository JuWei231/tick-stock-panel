"""通达信行情主站客户端 —— 连接管理、主站池、批量请求与坏记录隔离。

设计要点:

- **主站池是必需的, 不是优化**。实测 65 个已知主站里只有 1 个能提供行情,
  且主站 IP 会随时失效。这里维护种子池 + 健康探测 + 失败自动切换,
  并把探测结果缓存到 data 目录, 避免每次启动都全量重探。
- **不依赖 pytdx/mootdx**。两者对退市标的的短记录都会解析越界并丢掉整批
  (实测 70 批里有 6 批因此整批丢失), 属于静默数据缺失。这里自己解析并
  在批量出现缺条时二分拆包隔离坏标的。
- 所有网络调用都受超时约束, 失败抛 TdxError, 由上层决定软/硬失败语义。
"""
from __future__ import annotations

import contextlib
import logging
import socket
import struct
import threading
import time
from pathlib import Path

from app.plugins.tdx import protocol as proto

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0
RECV_TIMEOUT = 10.0

# 主站池种子。实测可用率约 1/18, 因此池子要够大, 并靠探测筛出可用的。
# 来源: 通达信客户端 connect.cfg 中的主站列表(社区公开的同类列表同样适用)。
SEED_SERVERS: tuple[tuple[str, int], ...] = (
    ("110.41.174.169", 7709), ("110.41.147.114", 7709), ("110.41.2.72", 7709),
    ("110.41.4.4", 7709), ("110.41.154.219", 7709), ("110.41.174.169", 7709),
    ("124.70.176.52", 7709), ("124.70.199.56", 7709), ("124.70.133.119", 7709),
    ("124.71.85.110", 7709), ("124.71.187.72", 7709), ("124.71.187.122", 7709),
    ("124.71.9.153", 7709), ("123.60.186.45", 7709), ("123.60.164.122", 7709),
    ("123.60.70.228", 7709), ("123.60.73.44", 7709), ("123.60.84.66", 7709),
    ("123.249.15.60", 7709), ("121.36.54.217", 7709), ("121.36.81.195", 7709),
    ("121.36.225.169", 7709), ("121.37.183.82", 7709), ("47.113.94.204", 7709),
    ("47.100.236.28", 7709), ("47.116.105.28", 7709), ("8.129.174.169", 7709),
    ("139.9.51.18", 7709), ("139.159.239.163", 7709), ("106.14.201.131", 7709),
    ("106.14.190.242", 7709), ("116.205.163.254", 7709), ("116.205.171.132", 7709),
    ("116.205.183.150", 7709), ("119.97.185.59", 7709),
    # 传统主站(部分已失效, 保留以扩大可用面)
    ("123.125.108.14", 7709), ("180.153.18.170", 7709), ("115.238.90.165", 7709),
    ("60.191.117.167", 7709), ("218.75.126.9", 7709), ("114.80.63.12", 7709),
    ("119.147.212.81", 7709), ("218.108.98.244", 7709), ("180.153.39.51", 7709),
    ("58.63.254.219", 7709), ("122.51.120.217", 7709),
)

# 交易所后缀 → 通达信市场号
MARKET_OF_SUFFIX = {"SH": 1, "SZ": 0, "BJ": 0}
SUFFIX_OF_MARKET = {1: "SH", 0: "SZ"}


class TdxError(RuntimeError):
    """通达信请求失败(网络/协议/主站不可用)。"""


def to_wire(symbol: str) -> tuple[int, str]:
    """`600519.SH` → (1, '600519'); `000001.SZ` → (0, '000001')。"""
    code, _, suffix = symbol.partition(".")
    suffix = suffix.upper()
    if not code or suffix not in MARKET_OF_SUFFIX:
        raise ValueError(f"无法识别的标的代码: {symbol!r}")
    return MARKET_OF_SUFFIX[suffix], code


def from_wire(market: int, code: str) -> str:
    """(1, '600519') → `600519.SH`。"""
    return f"{code}.{SUFFIX_OF_MARKET.get(market, 'SZ')}"


def is_fund_code(code: str) -> bool:
    """基金/ETF 代码判定 —— 决定快照价格除数(100 还是 1000)。

    实测 510300 / 159915 / 512880 的快照原始值需除以 1000 才与日K一致,
    个股与指数则除以 100。判据用交易所既有的代码段划分。
    """
    return code.startswith(("15", "16", "18", "50", "51", "52", "56", "58"))


class _Connection:
    """单条到主站的 TCP 连接(含握手)。非线程安全, 由 TdxClient 串行化使用。"""

    def __init__(self, host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._connect(timeout)

    def _connect(self, timeout: float) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(RECV_TIMEOUT)
        self._sock = sock
        for packet in proto.SETUP_PACKETS:
            self._exchange(packet)

    def _recv_exact(self, size: int) -> bytes:
        assert self._sock is not None
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise TdxError(f"主站 {self.host} 提前关闭连接")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _exchange(self, packet: bytes) -> bytes:
        """发送报文并返回解压后的 body。"""
        assert self._sock is not None
        self._sock.sendall(packet)
        head = self._recv_exact(proto.RSP_HEADER_LEN)
        zipped, _unzipped = proto.read_sizes(head)
        body = self._recv_exact(zipped) if zipped else b""
        return proto.split_response(head, body)

    def call(self, packet: bytes) -> bytes:
        with self._lock:
            return self._exchange(packet)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            # 对端可能已断开, shutdown 失败不影响随后 close
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()

    @property
    def alive(self) -> bool:
        return self._sock is not None


def probe_server(host: str, port: int = 7709, timeout: float = 4.0) -> bool:
    """探测主站是否真的能提供行情。

    只看 TCP 可连是不够的: 实测多数主站能完成握手却对行情指令返回空,
    因此这里用一次最小日K请求作为判据。
    """
    conn = None
    try:
        conn = _Connection(host, port, timeout)
        body = conn.call(proto.bars_packet(proto.CAT_DAY, 1, "600519", 0, 1))
        rows = proto.parse_bars(body, proto.CAT_DAY)
        return bool(rows) and rows[0]["close"] > 0
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


class TdxClient:
    """通达信主站客户端。线程内串行复用同一条连接。"""

    def __init__(
        self,
        servers: tuple[tuple[str, int], ...] = SEED_SERVERS,
        cache_path: Path | None = None,
        probe_limit: int = 6,
    ) -> None:
        self._servers = servers
        self._cache_path = cache_path
        self._probe_limit = probe_limit
        self._conn: _Connection | None = None
        self._lock = threading.Lock()
        self._verified: list[tuple[str, int]] = []

    # ---- 主站选择 --------------------------------------------------------

    def _load_verified(self) -> list[tuple[str, int]]:
        if self._verified:
            return self._verified
        cached: list[tuple[str, int]] = []
        if self._cache_path and self._cache_path.exists():
            try:
                for line in self._cache_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    host, _, port = line.strip().partition(":")
                    cached.append((host, int(port or 7709)))
            except Exception as e:
                logger.warning("通达信主站缓存读取失败, 忽略: %s", e)
        # 已探测成功过的排前面, 减少重复探测
        ordered = cached + [s for s in self._servers if s not in cached]
        self._verified = ordered
        return ordered

    def _save_verified(self, working: tuple[str, int]) -> None:
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(f"{working[0]}:{working[1]}\n", encoding="utf-8")
        except Exception as e:
            logger.warning("通达信主站缓存写入失败: %s", e)

    def _ensure_conn(self) -> _Connection:
        with self._lock:
            if self._conn is not None and self._conn.alive:
                return self._conn
            last_error: Exception | None = None
            tried = 0
            for host, port in self._load_verified():
                if tried >= self._probe_limit:
                    break
                tried += 1
                try:
                    conn = _Connection(host, port)
                    body = conn.call(proto.bars_packet(proto.CAT_DAY, 1, "600519", 0, 1))
                    if not proto.parse_bars(body, proto.CAT_DAY):
                        raise TdxError("主站不返回行情数据")
                    self._conn = conn
                    self._save_verified((host, port))
                    logger.info("通达信主站已连接: %s:%s", host, port)
                    return conn
                except Exception as e:
                    last_error = e
                    logger.debug("通达信主站 %s:%s 不可用: %s", host, port, e)
            raise TdxError(f"没有可用的通达信主站(已尝试 {tried} 个): {last_error}")

    def _call(self, packet: bytes) -> bytes:
        """发送报文; 连接失效时重建一次再重试。"""
        conn = self._ensure_conn()
        try:
            return conn.call(packet)
        except Exception as e:
            logger.warning("通达信请求失败, 重连后重试: %s", e)
            self.close()
            return self._ensure_conn().call(packet)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---- 业务调用 --------------------------------------------------------

    def bars(self, category: int, market: int, code: str, start: int, count: int) -> list[dict]:
        return proto.parse_bars(self._call(proto.bars_packet(category, market, code, start, count)), category)

    def index_bars(self, category: int, market: int, code: str, start: int, count: int) -> list[dict]:
        """指数K线: 与证券K线同一指令, 但每条记录多 4 字节涨跌家数。"""
        return proto.parse_bars(
            self._call(proto.bars_packet(category, market, code, start, count)), category, is_index=True
        )

    def xdxr(self, market: int, code: str) -> list[dict]:
        return proto.parse_xdxr(self._call(proto.xdxr_packet(market, code)))

    @staticmethod
    def _divisor(market: int, code: str) -> float:
        return 1000.0 if is_fund_code(code) else 100.0

    def quotes(self, symbols: list[str], *, max_batch: int = proto.MAX_QUOTE_BATCH) -> list[dict]:
        """批量快照(含五档)。

        缺条时二分拆包: 退市/异常标的会产生短记录, 若整批丢弃会静默丢失
        同批其它标的的行情。这里只在「返回条数 < 请求条数」时拆, 拆到单只
        仍缺就记 warning 放弃该只, 不影响其它标的。
        """
        out: list[dict] = []
        for i in range(0, len(symbols), max_batch):
            out.extend(self._quotes_chunk(symbols[i:i + max_batch]))
        return out

    def _quotes_chunk(self, symbols: list[str]) -> list[dict]:
        if not symbols:
            return []
        wire = [to_wire(s) for s in symbols]
        try:
            rows = proto.parse_quotes(
                self._call(proto.quotes_packet(wire)),
                price_divisor_of=self._divisor,
            )
        except Exception as e:
            if len(symbols) == 1:
                # 单只仍失败: 记 debug 并放弃该只, 由上层汇总缺失数, 避免逐只刷日志
                logger.debug("通达信快照单只请求失败 %s: %s", symbols[0], e)
                return []
            logger.debug("通达信快照整批失败(%d 只), 拆包重试: %s", len(symbols), e)
            return self._split_quotes(symbols)

        if len(rows) >= len(symbols):
            return rows

        if len(symbols) == 1:
            logger.debug("通达信快照缺少行情, 放弃该标的: %s", symbols[0])
            return []
        logger.debug("通达信快照缺条(%d/%d), 拆包定位", len(rows), len(symbols))
        return self._split_quotes(symbols)

    def _split_quotes(self, symbols: list[str]) -> list[dict]:
        mid = len(symbols) // 2
        return self._quotes_chunk(symbols[:mid]) + self._quotes_chunk(symbols[mid:])

    def security_count(self, market: int) -> int:
        """主站登记的证券数量(用于诊断, 不参与业务口径)。"""
        pkt = struct.pack("<HIHHIIHH", 0x10C, 0x02006320, 0x0C, 0x0C, 0x5053E, 0, 0, 0)
        body = self._call(pkt + struct.pack("<H", market))
        (count,) = struct.unpack_from("<H", body, 0)
        return count


def find_working_server(
    servers: tuple[tuple[str, int], ...] = SEED_SERVERS,
    limit: int = 25,
    per_server_timeout: float = 3.0,
) -> tuple[str, int] | None:
    """依次探测主站, 返回第一个真正能提供行情的。供设置页「试拉」与诊断使用。"""
    for host, port in servers[:limit]:
        if probe_server(host, port, per_server_timeout):
            return host, port
    return None


def sleep_brief(seconds: float) -> None:
    """限速用的短暂等待(独立出来便于测试替换)。"""
    time.sleep(seconds)
