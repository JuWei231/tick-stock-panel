"""共享 HTTP 层：东财限流请求 + 通用 GET（httpx 实现）。

由 SKILL.md「东财数据中心统一查询（共用 helper）」一节（requests 版）移植而来，
改用 httpx 以对齐 tick-stock-panel（其 backend 已内置 ``httpx>=0.27``），
做到移植零新增依赖、requests→httpx 语义一一对应。

移植说明
--------
- ``requests.Session + HTTPAdapter/Retry`` → ``httpx.Client`` + ``em_get()`` 内显式重试循环
  （429/5xx/连接错误指数退避重试；**403 不重试**——东财风控信号，重试无益反而加重，
  应靠调大 ``EM_MIN_INTERVAL`` 降频应对）。
- 全局节流（``EM_MIN_INTERVAL`` + 随机抖动 + 上次调用时间戳）与 SKILL.md 语义一致。
- 测试注入：子模块一律经 ``common.em_get`` / ``common.http_get`` 发请求，单测可
  ``mock.patch`` 这两个模块级函数，无需真实网络。

⚠️ 这是常驻服务（tick-stock-panel）与「LLM 手工调用」的本质区别：东财系接口共享
同一套风控面（每秒 >5 次 / 单 IP 并发 ≥10 / 1 分钟 ≥200 次 → 临时封 IP），
所有 eastmoney.com 流量必须走 ``em_get()`` 串行节流，宁可慢不可封。
"""
from __future__ import annotations

import random
import threading
import time

import httpx

#: 默认 UA，与 SKILL.md 保持一致
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
#: 东财数据中心统一查询入口（龙虎榜/解禁/两融/大宗/股东户数/分红共用）
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

#: 两次东财请求的最小间隔（秒）；批量任务建议调大到 1.5~2
EM_MIN_INTERVAL: float = 1.0

_client: httpx.Client | None = None
_client_lock = threading.Lock()
_throttle_lock = threading.Lock()
_last_call: float = 0.0


def get_client() -> httpx.Client:
    """返回进程级 Keep-Alive 会话（懒创建，默认带 UA）。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                headers={"User-Agent": UA},
                follow_redirects=True,
                timeout=15,
            )
        return _client


def close_client() -> None:
    """关闭进程级会话（进程退出 / 测试收尾时调用）。"""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def set_em_min_interval(seconds: float) -> None:
    """调整东财请求最小间隔（默认 1.0s；批量任务可调大）。"""
    global EM_MIN_INTERVAL
    EM_MIN_INTERVAL = max(0.0, float(seconds))


def _pace() -> None:
    """串行节流：保证任意两次东财请求的发起间隔 >= EM_MIN_INTERVAL（+随机抖动）。"""
    global _last_call
    with _throttle_lock:
        wait = EM_MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.5))
        _last_call = time.time()


def em_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 15,
    retries: int = 3,
) -> httpx.Response:
    """东财统一请求入口：自动节流 + 复用会话 + 默认 UA + 指数退避重试。

    - 403：不重试，直接返回（东财风控信号，重试无益反而加重）。
    - 429 / 500 / 502 / 503 / 504 / 连接级错误：指数退避重试 ``retries`` 次。
    """
    _pace()
    attempt = 0
    backoff = 0.6
    while True:
        try:
            resp = get_client().get(url, params=params, headers=headers, timeout=timeout)
        except httpx.TransportError:
            attempt += 1
            if attempt > retries:
                raise
            time.sleep(backoff * (2 ** (attempt - 1)))
            continue
        if resp.status_code == 403 or resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        attempt += 1
        if attempt > retries:
            return resp
        time.sleep(backoff * (2 ** (attempt - 1)))


def http_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 15,
) -> httpx.Response:
    """非东财源的普通 GET（财联社等），不带东财节流。"""
    return get_client().get(url, params=params, headers=headers, timeout=timeout)


def eastmoney_datacenter(
    report_name: str,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    """东财数据中心统一查询 — 已内置限流（龙虎榜/解禁/两融/大宗/股东户数/分红共用）。"""
    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    r = em_get(DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []
