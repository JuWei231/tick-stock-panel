"""a-stock-data 集成数据服务 — 研报 / 融资融券 / 财联社电报。

取数能力来自 ``app.astock``（由 a-stock-data 项目的 SKILL.md 移植，Apache-2.0；
来源与有意偏差记录见 ``backend/app/astock/NOTICE.md``）。本服务把纯取数函数接到
TSP 的存储约定上，参照 ``services/dragon_tiger.py`` 的非路由数据直连模式：

- 日级稳定数据（研报、两融）：按日落 JSON 缓存
  ``data/astock/{dataset}/{symbol}/date=YYYY-MM-DD.json``（原子写，当日已取直接读缓存）
- 强时效数据（财联社电报）：短 TTL 内存缓存，避免页面高频打上游

错误语义（对齐 dragon_tiger 的 ``state`` 模式）：上游失败返回 ``state=error`` 信封，
绝不返回看似合理但错误的数字；代码格式错误抛 ``ValueError`` 由 API 层转 400。
"""
from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path

from app.astock import cls_telegraph as _fetch_telegraph
from app.astock import eastmoney_reports as _fetch_reports
from app.astock import margin_trading as _fetch_margin
from app.astock import norm_ticker
from app.market_time import cn_today
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

#: 电报内存缓存 TTL（秒）：财联社为全市场滚动快讯，无需每次刷新都打上游
TELEGRAPH_TTL_SECONDS = 45.0

_telegraph_cache: dict = {"ts": 0.0, "payload": None}


def _cache_path(data_dir: Path, dataset: str, symbol: str) -> Path:
    return data_dir / "astock" / dataset / symbol / f"date={cn_today().isoformat()}.json"


def _load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _store_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False))


def _ok(dataset: str, symbol: str, code: str, items: list[dict]) -> dict:
    return {
        "state": "ok",
        "dataset": dataset,
        "symbol": symbol,
        "code": code,
        "date": cn_today().isoformat(),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "count": len(items),
        "items": items,
    }


def _error(dataset: str, symbol: str, message: str) -> dict:
    return {
        "state": "error",
        "dataset": dataset,
        "symbol": symbol,
        "message": message,
        "count": 0,
        "items": [],
    }


def get_reports(data_dir: Path, symbol: str, max_pages: int = 3) -> dict:
    """个股研报列表（东财 reportapi，含评级与三年 EPS 预测）。当日已取则读缓存。"""
    code = norm_ticker(symbol, stock_only=True)   # 格式错 / 指数码 → ValueError → API 400
    path = _cache_path(data_dir, "reports", code)
    cached = _load_json(path)
    if cached is not None:
        return cached
    try:
        items = _fetch_reports(code, max_pages=max_pages)
    except ValueError:
        raise                                       # 北交所老号段等业务性错误 → API 400
    except Exception as exc:  # noqa: BLE001 — 上游失败降级为 state=error，不伪造数据
        logger.warning("研报拉取失败 %s: %s", code, exc)
        return _error("reports", symbol, str(exc))
    payload = _ok("reports", symbol, code, items)
    with contextlib.suppress(OSError):
        _store_json(path, payload)
    return payload


def get_margin(data_dir: Path, symbol: str, limit: int = 30) -> dict:
    """融资融券明细（日级，东财 datacenter，金额单位元）。当日已取则读缓存。"""
    code = norm_ticker(symbol)
    path = _cache_path(data_dir, "margin", code)
    cached = _load_json(path)
    if cached is not None:
        return cached
    try:
        items = _fetch_margin(code, page_size=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("两融拉取失败 %s: %s", code, exc)
        return _error("margin", symbol, str(exc))
    payload = _ok("margin", symbol, code, items)
    with contextlib.suppress(OSError):
        _store_json(path, payload)
    return payload


def get_telegraph(limit: int = 50) -> dict:
    """财联社电报（全市场实时快讯，北京时间）。TTL 内复用内存缓存。"""
    now = time.monotonic()
    if (
        _telegraph_cache["payload"] is not None
        and now - _telegraph_cache["ts"] < TELEGRAPH_TTL_SECONDS
    ):
        return _telegraph_cache["payload"]
    try:
        items = _fetch_telegraph(page_size=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("财联社电报拉取失败: %s", exc)
        return {"state": "error", "dataset": "telegraph", "message": str(exc),
                "count": 0, "items": []}
    payload = {
        "state": "ok",
        "dataset": "telegraph",
        "date": cn_today().isoformat(),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "count": len(items),
        "items": items,
    }
    _telegraph_cache.update(ts=now, payload=payload)
    return payload
