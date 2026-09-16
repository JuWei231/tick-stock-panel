"""融资融券明细 — 由 SKILL.md Layer4 §4.1（东财 datacenter RPTA_WEB_RZRQ_GGMX）移植。

返回日级融资/融券余额与买卖/偿还明细，金额单位均为**元**。
"""
from __future__ import annotations

from . import common
from .tickers import norm_ticker


def margin_trading(code: str, page_size: int = 30) -> list[dict]:
    """融资融券明细（日级，最新在前）。

    - ``code`` 支持任意常见写法（600519 / SH600519 / 600519.SH），内部归一化为纯 6 位
      （SKILL 原实现不做归一、要求调用方先传 6 位；这里在边界统一，避免带前缀时
      把「代码写错」静默变成「没数据」）。
    - 返回: [{date, rzye(融资余额/元), rzmre(融资买入), rzche(融资偿还),
              rqye(融券余额/元), rqmcl(融券卖出量), rqchl(融券偿还量),
              rzrqye(融资融券余额合计/元)}]
    """
    code = norm_ticker(code)
    data = common.eastmoney_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{code}")',
        page_size=page_size,
        sort_columns="DATE",
        sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("DATE", ""))[:10],
            "rzye": row.get("RZYE", 0),       # 融资余额(元)
            "rzmre": row.get("RZMRE", 0),     # 融资买入额
            "rzche": row.get("RZCHE", 0),     # 融资偿还额
            "rqye": row.get("RQYE", 0),       # 融券余额(元)
            "rqmcl": row.get("RQMCL", 0),     # 融券卖出量
            "rqchl": row.get("RQCHL", 0),     # 融券偿还量
            "rzrqye": row.get("RZRQYE", 0),   # 融资融券余额合计
        })
    return rows
