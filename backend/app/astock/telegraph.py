"""财联社电报（全市场实时快讯）— 由 SKILL.md Layer5 §5.2 移植。

新接口 ``cls.cn/v1/roll/get_roll_list`` 强制校验 ``sign``，但签名**纯本地计算、无需
任何 key**：``sign = md5(sha1(按 key 字典序拼接的 query 串))``。

⚠️ 旧接口 ``cls.cn/nodeapi/telegraphList`` 已于 2026-05 下线（旧址返回 HTML 而非 JSON），
不要回退到 nodeapi 系接口。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from . import common

#: 财联社 ctime 为北京时间 epoch 秒。这里显式按 Asia/Shanghai 转成北京墙钟（naive），
#: 满足 tick-stock-panel「北京时间墙钟」契约。SKILL.md 原实现用本机时区
#: ``datetime.fromtimestamp(ts)``，在非中国时区服务器上会错开数小时——这是移植时
#: 唯一的有意偏差，运行在任意时区都得到一致的北京时间。
_BEIJING = timezone(timedelta(hours=8))


def _signed_url(params: dict) -> str:
    """拼接带 sign 的完整 URL：md5(sha1(按 key 字典序拼接的 query 串))。"""
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    return f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"


def cls_telegraph(page_size: int = 50) -> list[dict]:
    """财联社电报（全市场实时快讯），零 key。

    返回: [{title, content, time}]；time 为北京时间 ``YYYY-MM-DD HH:MM:SS``。
    """
    params = {
        "appName": "CailianpressWeb",
        "os": "web",
        "sv": "7.7.5",
        "last_time": "",
        "refresh_type": "1",
        "rn": str(page_size),
    }
    url = _signed_url(params)
    r = common.http_get(url, headers={"Referer": "https://www.cls.cn/"}, timeout=10)
    d = r.json()

    rows = []
    for item in (d.get("data") or {}).get("roll_data") or []:
        ts = item.get("ctime")
        if ts:
            t = (
                datetime.fromtimestamp(ts, tz=_BEIJING)
                .replace(tzinfo=None)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        else:
            t = ""
        rows.append({
            "title": item.get("title", "") or item.get("brief", ""),
            "content": item.get("content", "") or item.get("brief", ""),
            "time": t,
        })
    return rows
