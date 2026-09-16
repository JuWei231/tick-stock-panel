"""通达信数据源 Provider —— 把主站原始数据归一为项目内部契约。

数据集与实现状态(全部经真实主站实测):

| 数据集        | 实现 | 实测口径 |
| ---           | ---  | --- |
| `daily`       | ✅   | 不复权原始价; 与本地 kline_daily 逐字段一致; 日线可回溯至 2003 年 |
| `adj_factor`  | ✅   | 由除权除息事件按交易所公式推导, 前收盘取自日K |
| `minute`      | ✅   | 1 分钟约 6400 根(≈27 个交易日), 5 分钟约 6 个月 |
| `full_minute` | ✅   | 复用分钟批量接口, 盘中全市场当日增量 |
| `realtime`    | ✅   | 全市场约 3.3s/轮(80 只/批), 含五档 |
| `depth5`      | ✅   | 同批量快照接口, 五档价量齐全 |
| `financial`   | ✅   | **仅 `shares`**: 由股本变迁事件还原逐日股本; 三大报表不提供 |

单位与口径(全部实测确认, 非推测):

- K线价格 `÷1000`, 且为**增量编码**; 快照价格 `÷100`, 但基金/ETF 为 `÷1000`
  (实测 510300/159915/512880 按 100 解析会整体放大 10 倍)。
- K线 `volume` 已是**手**, 与项目契约一致, 不再换算(原始 .day 文件才是股)。
- `amount` 为元, 但走的是主站压缩浮点编码, 存在约 1e-8 量级的相对精度损失
  (实测 4430841344 vs 本地 4430841445), 金额精度敏感的用途需知悉。
- 快照 `change_pct` 按契约输出**小数制**(由 price 与 last_close 推导)。
- 分钟 `datetime` 为**北京墙钟 naive**; 主站给的是"日期 + 当日分钟数"。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from app.data_providers.normalizer import ADJ_FACTOR_COLS, DAILY_COLS
from app.plugins.tdx import gbbq
from app.plugins.tdx import protocol as proto
from app.plugins.tdx.client import (
    TdxClient,
    TdxError,
    find_working_server,
    is_fund_code,
    to_wire,
)

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "adj_factor", "minute", "full_minute", "realtime", "depth5", "financial")

# 主站单页上限(实测 800 有效, 超过会被截断)
_PAGE = 800
# 单标的日线最多翻页数: 800 * 24 = 19200 根, 足以覆盖 2003 年至今
_MAX_PAGES = 24
# 分钟线单标的翻页上限: 800 * 8 = 6400 根(与主站单周期实际上限一致)
_MAX_MINUTE_PAGES = 8

_MINUTE_CATEGORY = {"1m": proto.CAT_1MIN, "5m": proto.CAT_5MIN}

MINUTE_COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

_BEIJING = timezone(timedelta(hours=8))

# 股本变迁事件里的绝对股本单位为**万股** (实测与 instruments 比值 1.00000)。
_SHARE_UNIT = 1e4
# 首个事件之前的基准行: 取足够早的日期, 让下游 asof 连接能覆盖全部历史交易日。
_SEED_DATE = date(1990, 1, 1)
# "送转与其后的绝对股本事件配对"的判定窗口与倍数容差: 除权除息日(category 1)与其后
# 送股上市日(category 2)通常相差 1~3 个自然日(跨周末), 7 天足以覆盖, 又不会误配到
# 下一次无关的股本变动。
_PAIRED_EVENT_WINDOW_DAYS = 7
_PAIRED_RATIO_TOLERANCE = 0.02


def _abs_shares(event: dict, key: str) -> float | None:
    """事件的绝对股本字段 → 股; 缺失或非正返回 None (交给推定口径, 不猜)。"""
    value = event.get(key)
    if value is None or value <= 0:
        return None
    return float(value) * _SHARE_UNIT


def _songzhuan_multiplier(event: dict) -> float:
    """category 1 的送转/配股对股本的放大倍数; 其它类别或不改变股本时为 1.0。

    上游 songzhuangu / peigu 是"每 10 股"口径, 需除以 10。
    """
    if event.get("category") != 1:
        return 1.0
    song = event.get("songzhuangu") or 0.0
    pei = event.get("peigu") or 0.0
    multiplier = 1.0 + song / 10.0 + pei / 10.0
    return multiplier if multiplier > 0 else 1.0


def _share_row(symbol: str, effective: date, total: float | None, floats: float | None) -> dict:
    """一行股本事实: 生效日当日起, 股本按此值。"""
    return {
        "symbol": symbol,
        "period_end": effective,
        "announce_date": effective,
        "total_shares": total,
        "float_shares": floats,
    }


def _is_paired_with_absolute(event: dict, absolute_events: list[dict]) -> bool:
    """该送转是否已被紧随其后的绝对股本事件覆盖(倍数吻合)。

    典型形态: 除权除息日 category 1(如 10 送 10) → 次日送股上市 category 2, 且
    category 2 的 后总股本/前总股本 恰好等于该送转倍数。此时 category 2 的 `qian*`
    就是"送转生效前"的股本, 送转已体现在其中 —— 再按倍数反除会多除一次, 让
    "上市 → 首次股本变动"这段的基准偏小一个送转倍数(实测 600519 会从 IPO 真实总股本
    2.5 亿股变成 2.2727 亿股)。

    找不到配对的历史送转(确实存在没有绝对事件记录的送转)才需要反除。
    """
    multiplier = _songzhuan_multiplier(event)
    if multiplier == 1.0:
        return False
    for other in absolute_events:
        gap = (other["date"] - event["date"]).days
        if gap < 0 or gap > _PAIRED_EVENT_WINDOW_DAYS:
            continue
        before = _abs_shares(other, "qianzongguben")
        after = _abs_shares(other, "houzongguben")
        if not before or not after:
            continue
        if abs(after / before - multiplier) <= _PAIRED_RATIO_TOLERANCE * multiplier:
            return True
    return False


def _share_rows_from_events(
    symbol: str, events: list[dict] | tuple[dict, ...]
) -> list[dict]:
    """把股本变迁事件还原成"生效日 → 当日股本"的阶梯序列(在线与本地共用)。

    只用两类事件:
    - 带绝对股本的类别 (2/3/5/6/7/8/9/10 ...): 直接给 houzongguben / panhouliutong;
    - category 1 的送转/配股: 只给"每 10 股"比例, 按 (1 + 送转/10 + 配股/10) 链式推算
      —— 实测大量送转事件没有配对的绝对股本事件(如 600519.SH 9 个送转仅 1 个配对),
      不走乘数链会漏掉这些股本变动。

    单位: 通达信为**万股**, 乘 1e4 转为股 (实测与 instruments 比值 1.00000)。
    """
    if not events:
        return []
    ordered = sorted(events, key=lambda e: (e["date"], e["category"]))
    anchor = next((i for i, e in enumerate(ordered) if _abs_shares(e, "houzongguben")), None)
    if anchor is None:
        return []  # 无任何绝对股本锚点: 交给平台侧的推定口径, 不猜

    # 基准 = 首个绝对事件**之前**的股本(qian*), 它覆盖"上市 → 首次股本变动"整段区间;
    # 用 hou* 会把这一段错算成变动后的值。
    anchor_event = ordered[anchor]
    cur_total = _abs_shares(anchor_event, "qianzongguben") or _abs_shares(anchor_event, "houzongguben")
    cur_float = _abs_shares(anchor_event, "panqianliutong") or _abs_shares(anchor_event, "panhouliutong")
    if cur_total is None:
        return []

    # 锚点之前的事件只能是 category 1 送转/配股: 与绝对事件配对的那些(改动已体现在
    # 锚点 qian* 里)不反除, 只有真正没有绝对事件记录的送转才逐项反除。
    absolute_events = [e for e in ordered if _abs_shares(e, "houzongguben")]
    for event in reversed(ordered[:anchor]):
        multiplier = _songzhuan_multiplier(event)
        if multiplier != 1.0 and not _is_paired_with_absolute(event, absolute_events):
            cur_total /= multiplier
            if cur_float is not None:
                cur_float /= multiplier

    rows = [_share_row(symbol, _SEED_DATE, cur_total, cur_float)]
    for event in ordered:
        absolute = _abs_shares(event, "houzongguben")
        absolute_float = _abs_shares(event, "panhouliutong")
        if absolute is not None:
            cur_total = absolute
            if absolute_float is not None:
                cur_float = absolute_float
        else:
            multiplier = _songzhuan_multiplier(event)
            if multiplier == 1.0:
                continue  # 纯分红/权证等不改变股本
            cur_total *= multiplier
            if cur_float is not None:
                cur_float *= multiplier
        if event["date"] > _SEED_DATE:
            rows.append(_share_row(symbol, event["date"], cur_total, cur_float))

    # 同一日多类别事件只保留最终值(顺序已保证最后一次即当日生效值)。
    deduped: dict[date, dict] = {}
    for row in rows:
        deduped[row["period_end"]] = row
    return [deduped[d] for d in sorted(deduped)]


def _now_beijing() -> datetime:
    return datetime.now(_BEIJING).replace(tzinfo=None)


def _to_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def _market_of(symbol: str) -> int:
    return to_wire(symbol)[0]


def _code_of(symbol: str) -> str:
    return to_wire(symbol)[1]


@dataclass
class TdxSourceConfig:
    """满足 Provider 契约所需的 config 形状(只需 datasets 属性)。"""

    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))


class TdxProvider:
    """通达信行情主站数据源。

    无第三方依赖、无需 API Key; 可用性取决于主站池中是否存在可提供行情的主站
    (实测约 18 个可连主站里 1 个可用), 因此连接层带池化与自动切换。
    """

    name = "tdx"
    builtin = True

    # 1 分钟实测约 27 个交易日; 按保守值声明, 与 TickFlow 深历史基准区分
    minute_history_days = 20

    def __init__(self) -> None:
        self.config = TdxSourceConfig()
        self._client: TdxClient | None = None
        self._universe_cache: list[str] | None = None
        # 本地 gbbq(离线股本来源): 只探测一次, 快照按 (mtime, size) 缓存
        self._gbbq_probed = False
        self._gbbq_path: Path | None = None
        self._gbbq: gbbq.GbbqSnapshot | None = None
        self._gbbq_stamp: tuple[int, int] | None = None

    # ---- 生命周期 --------------------------------------------------------

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _cache_file(self) -> Path | None:
        try:
            from app.config import settings

            return settings.data_dir / "cache" / "tdx" / "servers.txt"
        except Exception:
            return None

    def _get_client(self) -> TdxClient:
        if self._client is None:
            self._client = TdxClient(cache_path=self._cache_file())
        return self._client

    # ---- 标的范围 --------------------------------------------------------

    def _universe(self) -> list[str]:
        """全市场标的(供 get_realtime 使用)。

        通达信快照接口需要显式标的列表, 而契约要求 get_realtime() 返回全市场,
        因此复用本项目的标的维表作为范围来源。维表不可用时返回空 → get_realtime
        软失败返回 [], 不阻断轮询线程。
        """
        if self._universe_cache is not None:
            return self._universe_cache
        symbols: list[str] = []
        try:
            from app.config import settings

            files = sorted((settings.data_dir / "instruments").glob("**/*.parquet"))
            for f in files:
                try:
                    df = pl.read_parquet(f, columns=["symbol"])
                    symbols.extend(
                        s for s in df["symbol"].to_list() if s and "." in s
                    )
                except Exception as e:
                    logger.warning("通达信读取标的维表失败 %s: %s", f.name, e)
        except Exception as e:
            logger.warning("通达信无法取得标的维表, 实时快照将为空: %s", e)

        seen: set[str] = set()
        self._universe_cache = [s for s in symbols if not (s in seen or seen.add(s))]
        return self._universe_cache

    # ---- K线 -------------------------------------------------------------

    def _paged_bars(
        self,
        symbol: str,
        category: int,
        start: date | None,
        *,
        is_index: bool,
        max_pages: int,
    ) -> list[dict]:
        """按页向前回溯取K线, 直到覆盖 start 或主站取尽。

        主站以 offset=0 表示"最新一页", 页内按时间升序, 因此要拿到历史窗口
        必须递增 offset 向前翻。
        """
        market, code = to_wire(symbol)
        client = self._get_client()
        collected: list[dict] = []

        for page in range(max_pages):
            offset = page * _PAGE
            try:
                rows = (
                    client.index_bars(category, market, code, offset, _PAGE)
                    if is_index
                    else client.bars(category, market, code, offset, _PAGE)
                )
            except TdxError:
                raise
            except Exception as e:
                logger.debug("通达信取 %s offset=%d 失败: %s", symbol, offset, e)
                break

            if not rows:
                break
            collected = rows + collected
            if len(rows) < _PAGE:
                break
            if start is not None and rows[0]["date"] <= start:
                break

        # 页间接缝可能重复, 去重后按时间升序
        dedup: dict[object, dict] = {}
        for row in collected:
            key = (row["date"], row.get("hour"), row.get("minute"))
            dedup[key] = row
        return [dedup[k] for k in sorted(dedup)]

    def _iter_symbol_daily(self, symbol: str, start: date | None, end: date | None,
                           asset_type: str) -> list[dict]:
        rows = self._paged_bars(
            symbol, proto.CAT_DAY, start, is_index=(asset_type == "index"), max_pages=_MAX_PAGES
        )
        return [
            r for r in rows
            if (start is None or r["date"] >= start) and (end is None or r["date"] <= end)
        ]

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        return self._collect_daily(symbols, start_time, end_time, asset_type, on_chunk_done)

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> Iterator[pl.DataFrame]:
        """流式分批返回, 避免全市场同步在内存里攒完整 DataFrame。"""
        total = len(symbols)
        for index, symbol in enumerate(symbols, start=1):
            frame = self._collect_daily([symbol], start_time, end_time, asset_type, None)
            if on_chunk_done is not None:
                on_chunk_done(index, total)
            if frame.height:
                yield frame
        if total == 0 and on_chunk_done is not None:
            on_chunk_done(0, 0)

    def _collect_daily(self, symbols, start_time, end_time, asset_type, on_chunk_done) -> pl.DataFrame:
        start, end = _to_date(start_time), _to_date(end_time)
        records: list[dict] = []
        total = len(symbols)
        failures = 0

        for index, symbol in enumerate(symbols, start=1):
            try:
                for row in self._iter_symbol_daily(symbol, start, end, asset_type):
                    records.append({
                        "symbol": symbol,
                        "date": row["date"],
                        "open": row["open"],
                        "high": row["high"],
                        "low": row["low"],
                        "close": row["close"],
                        "volume": row["volume"],
                        "amount": row["amount"],
                        "quote_ts": None,
                    })
            except Exception as e:
                failures += 1
                logger.warning("通达信取日K失败 %s: %s", symbol, e)
            if on_chunk_done is not None:
                on_chunk_done(index, total)

        if failures:
            logger.warning("通达信日K同步: %d/%d 只标的存在失败", failures, total)
        if not records:
            return pl.DataFrame(schema={c: pl.Null for c in DAILY_COLS})

        return pl.DataFrame(records).with_columns(
            pl.col("date").cast(pl.Date),
            *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume", "amount")],
        ).select(DAILY_COLS)

    # ---- 除权因子 --------------------------------------------------------

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """除权因子: 由除权除息事件按交易所公式推导。

        需要每个事件的前收盘, 取自该标的日K(一次翻页即可覆盖近三年事件),
        因此每只标的最多两次请求。全市场逐标的约需数分钟, 属低频任务。
        """
        start, end = _to_date(start_time), _to_date(end_time)
        records: list[dict] = []
        total = len(symbols)
        failures = 0

        for index, symbol in enumerate(symbols, start=1):
            market, code = to_wire(symbol)
            try:
                events = self._get_client().xdxr(market, code)
                ex_events = [
                    e for e in events
                    if e["category"] == 1
                    and (start is None or e["date"] >= start)
                    and (end is None or e["date"] <= end)
                ]
                if ex_events:
                    # 只需要每个事件日的前一个交易日收盘, 因此把翻页下界收到最早
                    # 事件日之前(留 40 个自然日缓冲, 覆盖长假), 避免为配价拉全历史。
                    oldest = min(e["date"] for e in ex_events)
                    closes = {
                        r["date"]: r["close"]
                        for r in self._paged_bars(
                            symbol, proto.CAT_DAY, oldest - timedelta(days=40),
                            is_index=False, max_pages=_MAX_PAGES,
                        )
                    }
                    trading_days = sorted(closes)
                    for event in ex_events:
                        prev_days = [d for d in trading_days if d < event["date"]]
                        if not prev_days:
                            continue
                        prev_close = closes[prev_days[-1]]
                        factor = proto.ex_factor_from_event(event, prev_close)
                        if factor is None:
                            logger.debug("通达信除权因子推导失败 %s %s", symbol, event["date"])
                            continue
                        records.append({
                            "symbol": symbol,
                            "trade_date": event["date"],
                            "ex_factor": factor,
                        })
            except Exception as e:
                failures += 1
                logger.warning("通达信取除权因子失败 %s: %s", symbol, e)
            if on_chunk_done is not None:
                on_chunk_done(index, total)

        if failures:
            logger.warning("通达信除权因子同步: %d/%d 只标的存在失败", failures, total)
        if not records:
            return pl.DataFrame(schema={c: pl.Null for c in ADJ_FACTOR_COLS})

        return pl.DataFrame(records).with_columns(
            pl.col("trade_date").cast(pl.Date), pl.col("ex_factor").cast(pl.Float64)
        ).select(ADJ_FACTOR_COLS)

    # ---- 财务 / 股本 -----------------------------------------------------

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """财务数据。**只支持 `shares`(逐日股本)**, 三大报表通达信不提供, 返回空表。

        通达信没有三大报表, 但股本变迁事件里带绝对股本, 足以还原"交易日当日股本":
        落盘的是**事件行**(生效日 → 当日股本)而非逐日行, 平台侧 share_capital 会按
        生效日 asof 连接, 因此下游拿到的就是逐日阶梯值。

        取数顺序: **本地 `gbbq` 优先**(离线/首刷, 见 `gbbq` 模块), 本地没有覆盖的标的
        才逐只走主站 xdxr。本地那份用市场号 2 承载北交所, 是在线路径拿不到的;
        文件缺失/损坏/过期时整体回退在线, 行为与只有在线时一致。

        `latest_only` 在此被忽略: 两种来源都是一次取全量事件(在线单次请求即返回全部
        事件, 本地一次解密全市场), 只取最新会漏掉上次同步之后发生的中间送转/配股。
        """
        if table != "shares":
            logger.info("通达信不提供财务表 %s (仅 shares), 返回空表", table)
            return pl.DataFrame()

        records: list[dict] = []
        pending = list(symbols)
        snapshot = self._local_gbbq()
        if snapshot is not None:
            covered = 0
            uncovered: list[str] = []
            for symbol in symbols:
                rows = _share_rows_from_events(symbol, gbbq.events_for(snapshot, symbol))
                if rows:
                    records.extend(rows)
                    covered += 1
                else:
                    uncovered.append(symbol)
            logger.info(
                "通达信股本: 本地 gbbq 覆盖 %d/%d 只(文件 %s, 最新事件 %s), 其余 %d 只走在线",
                covered, len(symbols), snapshot.path, snapshot.newest_event, len(uncovered),
            )
            pending = uncovered

        failures = 0
        total = len(pending)
        for index, symbol in enumerate(pending, start=1):
            try:
                records.extend(self._share_history_rows(symbol))
            except Exception as e:
                failures += 1
                logger.warning("通达信取股本变迁失败 %s: %s", symbol, e)
            if index % 500 == 0:
                logger.info("通达信股本变迁: %d/%d (累计 %d 行)", index, total, len(records))

        if failures:
            logger.warning("通达信股本变迁同步: %d/%d 只标的存在失败", failures, total)
        if not records:
            return pl.DataFrame()

        return pl.DataFrame(records).with_columns(
            pl.col("period_end").cast(pl.Date),
            pl.col("announce_date").cast(pl.Date),
            pl.col("total_shares").cast(pl.Float64),
            pl.col("float_shares").cast(pl.Float64),
        )

    def _local_gbbq(self) -> gbbq.GbbqSnapshot | None:
        """本地 gbbq 快照; 找不到 / 读不出 / 已过期时返回 None(调用方走在线)。

        过期判定用文件里"最新事件日"而不是 mtime: gbbq 是全市场事件文件, 每个交易日
        都有新公告落进来, 而 mtime 相同的拷贝(手工复制)不代表数据是新的。
        """
        if not self._gbbq_probed:
            self._gbbq_probed = True
            self._gbbq_path = gbbq.find_local_gbbq()
            if self._gbbq_path is not None:
                logger.info("通达信本地 gbbq: %s", self._gbbq_path)
        path = self._gbbq_path
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            # 文件被删/移走(客户端重装、盘符变化): 下次调用重新探测
            self._gbbq_probed = False
            self._gbbq_path = None
            return None

        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._gbbq is None or self._gbbq_stamp != stamp:
            snapshot = gbbq.load_gbbq(path)
            if snapshot is None:
                return None
            self._gbbq, self._gbbq_stamp = snapshot, stamp
        snapshot = self._gbbq
        if snapshot is None:
            return None
        if not gbbq.is_fresh(snapshot):
            logger.warning(
                "本地 gbbq 最新事件为 %s(超过 %d 天), 可能漏掉新送转, 本次回退在线 xdxr: %s",
                snapshot.newest_event, gbbq.MAX_EVENT_AGE_DAYS, snapshot.path,
            )
            return None
        return snapshot

    def _share_history_rows(self, symbol: str) -> list[dict]:
        """在线取单只标的的股本变迁事件, 还原成"生效日 → 当日股本"的阶梯序列。"""
        market, code = to_wire(symbol)
        return _share_rows_from_events(symbol, self._get_client().xdxr(market, code))

    # ---- 分钟线 ----------------------------------------------------------

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done=None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        frames: list[pl.DataFrame] = []
        total = len(symbols)
        for index, symbol in enumerate(symbols, start=1):
            frame = self._minute_frame(symbol, start_time, end_time, freq)
            if frame.height:
                frames.append(frame)
            if on_chunk_done is not None:
                on_chunk_done(index, total)
        if not frames:
            return pl.DataFrame(schema={c: pl.Null for c in MINUTE_COLS})
        return pl.concat(frames, how="vertical_relaxed").select(MINUTE_COLS)

    def _minute_frame(self, symbol: str, start_time, end_time, freq: str) -> pl.DataFrame:
        category = _MINUTE_CATEGORY.get(freq)
        if category is None:
            logger.warning("通达信不支持分钟周期 %s, 仅支持 1m/5m", freq)
            return pl.DataFrame(schema={c: pl.Null for c in MINUTE_COLS})

        start = _to_date(start_time)
        end = _to_date(end_time)
        try:
            rows = self._paged_bars(
                symbol, category, start, is_index=False, max_pages=_MAX_MINUTE_PAGES
            )
        except Exception as e:
            logger.warning("通达信取分钟线失败 %s: %s", symbol, e)
            return pl.DataFrame(schema={c: pl.Null for c in MINUTE_COLS})

        records = []
        for row in rows:
            if start is not None and row["date"] < start:
                continue
            if end is not None and row["date"] > end:
                continue
            # 主站给的是"日期 + 当日分钟数", 直接构造北京墙钟 naive datetime
            records.append({
                "symbol": symbol,
                "datetime": datetime(
                    row["date"].year, row["date"].month, row["date"].day,
                    row["hour"], row["minute"],
                ),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "amount": row["amount"],
            })
        if not records:
            return pl.DataFrame(schema={c: pl.Null for c in MINUTE_COLS})
        return pl.DataFrame(records).with_columns(
            *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume", "amount")]
        )

    def get_intraday_batch(
        self, symbols: list[str], count: int = 300, asset_type: str = "stock"
    ) -> pl.DataFrame:
        """全量分钟修复轮: 给定标的当日 1 分钟K。"""
        today = _now_beijing().date()
        frames: list[pl.DataFrame] = []
        for symbol in symbols:
            frame = self._minute_frame(symbol, today, today, "1m")
            if frame.height:
                frames.append(frame.tail(count))
        if not frames:
            return pl.DataFrame(schema={c: pl.Null for c in MINUTE_COLS})
        return pl.concat(frames, how="vertical_relaxed").select(MINUTE_COLS)

    # 刻意不实现 get_intraday_latest: 通达信分钟接口是单标的的, 拿不到
    # "一次返回全市场每只最新 N 根"。按契约, 不定义该方法时服务经
    # getattr(..., None) 判定为未实现, 干净降级为「仅修复轮」(节奏下限 60s);
    # 若定义成"抛 NotImplementedError", 会被当成失败轮并每轮刷一条 warning。

    # ---- 实时快照 / 五档 -------------------------------------------------

    def _quote_records(self, symbols: list[str]) -> list[dict]:
        rows = self._get_client().quotes(symbols)
        stamp = int(_now_beijing().replace(tzinfo=_BEIJING).timestamp() * 1000)
        records: list[dict] = []
        for row in rows:
            symbol = f"{row['code']}.{'SH' if row['market'] == 1 else 'SZ'}"
            last_price = row["price"]
            prev_close = row["last_close"]
            change_amount = None
            change_pct = None
            if last_price and prev_close:
                change_amount = last_price - prev_close
                # 契约: change_pct 为【小数制】(0.0366 = 3.66%)
                change_pct = change_amount / prev_close
            records.append({
                "symbol": symbol,
                "last_price": last_price,
                "prev_close": prev_close,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "volume": row["vol"],
                "amount": row["amount"],
                "change_amount": change_amount,
                "change_pct": change_pct,
                "timestamp": stamp,
                # 通达信快照不含名称与这些衍生字段, 按契约置 None 而非伪造
                "name": None,
                "amplitude": None,
                "turnover_rate": None,
                "session": None,
            })
        return records

    def get_realtime(self) -> list[dict]:
        """全市场实时快照。**软失败**: 任何异常返回 [] 并记 warning, 不中断轮询线程。"""
        symbols = self._universe()
        if not symbols:
            logger.warning("通达信实时快照: 标的范围为 0, 返回空")
            return []
        try:
            records = self._quote_records(symbols)
        except Exception as e:
            logger.warning("通达信实时快照失败, 本轮返回空: %s", e)
            return []
        if len(records) < len(symbols):
            logger.warning(
                "通达信实时快照缺 %d 只(请求 %d, 返回 %d)",
                len(symbols) - len(records), len(symbols), len(records),
            )
        return records

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数实时快照。失败返回 None(保留上轮缓存), 成功无数据返回 []。"""
        if not symbols:
            return []
        try:
            return self._quote_records(symbols)
        except Exception as e:
            logger.warning("通达信指数快照失败, 保留上轮缓存: %s", e)
            return None

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """五档盘口。数量单位为「手」, 与契约一致(实测 bid_vol1=9 即 9 手)。"""
        stamp = int(_now_beijing().replace(tzinfo=_BEIJING).timestamp() * 1000)
        try:
            rows = self._get_client().quotes(symbols)
        except Exception as e:
            logger.warning("通达信五档获取失败: %s", e)
            return {}

        out: dict[str, dict] = {}
        for row in rows:
            symbol = f"{row['code']}.{'SH' if row['market'] == 1 else 'SZ'}"
            out[symbol] = {
                "bid_prices": row["bid_prices"],
                "bid_volumes": row["bid_volumes"],
                "ask_prices": row["ask_prices"],
                "ask_volumes": row["ask_volumes"],
                "timestamp": stamp,
            }
        return out

    # ---- 设置页试拉 ------------------------------------------------------

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉」: 真实取一次数据并回预览, 顺带验证主站连通性。"""
        probe_symbol = (symbols or ["600519.SH"])[0]
        try:
            if dataset == "daily":
                df = self.get_daily([probe_symbol])
            elif dataset == "adj_factor":
                df = self.get_adj_factors([probe_symbol])
            elif dataset in ("minute", "full_minute"):
                df = self.get_minute([probe_symbol])
            elif dataset == "realtime":
                rows = self._quote_records([probe_symbol])
                df = pl.DataFrame(rows) if rows else pl.DataFrame()
            elif dataset == "depth5":
                rows = self._get_client().quotes([probe_symbol])
                df = pl.DataFrame(rows) if rows else pl.DataFrame()
            else:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "columns": [],
                        "preview": [], "error": f"通达信不支持数据集 {dataset}"}
        except Exception as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "columns": [],
                    "preview": [], "error": f"{type(e).__name__}: {e}"}

        # 取数为 0 时给出可定位的原因: 逐标的软失败(主站不可用/标的无数据)在
        # 同步路径上只记日志, 试拉按钮必须把「为什么是空」直接告诉用户。
        error = None
        if df.height == 0:
            error = (
                "未取到数据: 可能是无可用主站(点击试拉会重新探测), 或该标的在当前"
                "周期确无数据"
            )

        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": df.height,
            "columns": df.columns,
            "preview": df.head(5).to_dicts(),
            **({"error": error} if error else {}),
        }

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """通达信可用 get_security_list 枚举标的, 但缺少本项目维表要求的
        名称/上市日/股本等字段, 因此不声明 instruments 能力, 返回空。"""
        return []


# ---------------------------------------------------------------------------
# 插件可用性
# ---------------------------------------------------------------------------

def availability() -> tuple[bool, str]:
    """插件可用性。

    通达信无需第三方依赖、无需 API Key, 因此"已安装"恒为真; 真正的连通性
    取决于主站池(main站会失效), 由设置页的「试拉」给出确定结论。
    这里只做一次带缓存的快速探测: 有历史可用主站缓存即视为就绪。
    """
    try:
        from app.config import settings

        cache = settings.data_dir / "cache" / "tdx" / "servers.txt"
        if cache.exists():
            return True, "ok (已有可用主站缓存)"
    except Exception:
        pass
    return True, "ok (无需依赖与 Key; 点击试拉可验证主站连通性)"


def probe_best_server(limit: int = 25) -> tuple[str, int] | None:
    """诊断用: 在全池中找出第一个能提供行情的主站。"""
    return find_working_server(limit=limit)


def is_fund(symbol: str) -> bool:
    """暴露给测试的辅助: 判断是否基金/ETF(决定快照价格除数)。"""
    return is_fund_code(_code_of(symbol))
