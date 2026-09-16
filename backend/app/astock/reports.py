"""个股研报列表 + PDF 下载 — 由 SKILL.md Layer2 §2.1（东财 reportapi）移植。

A 级接口（公开 JSON API），reportapi.eastmoney.com，免费无 key。
reportapi 只认纯 6 位数字：带前缀会返回 hits=0，看起来像「这只票没研报」，
实际是格式没归一化——``eastmoney_reports()`` 内部已先过 ``norm_ticker()``。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import common
from .tickers import norm_ticker

#: 研报列表端点
REPORT_API = "https://reportapi.eastmoney.com/report/list"
#: PDF 下载模板（infoCode 见 record）
PDF_TPL = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"


def eastmoney_reports(code: str, max_pages: int = 5) -> list[dict]:
    """拉取指定股票的研报列表（含评级 / 三年 EPS 预测等字段）。

    - ``code`` 支持 600519 / SH600519 / 600519.SH 等写法（内部归一化为纯 6 位）；
      格式错误 / 显式指数码直接抛 ``ValueError``，不静默返回空。
    - 返回 ``[]`` 仅表示东财确无该标的研报覆盖。
    - record 关键字段：title / publishDate / orgSName / infoCode /
      predictThisYearEps / predictNextYearEps / predictNextTwoYearEps /
      emRatingName / indvInduName。
    """
    code = norm_ticker(code, stock_only=True)
    all_records: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*", "pageSize": "100", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": "2000-01-01", "endTime": "2030-01-01",
            "pageNo": str(page), "fields": "", "qType": "0",
            "orgCode": "", "code": code, "rcode": "",
            "p": str(page), "pageNum": str(page), "pageNumber": str(page),
        }
        r = common.em_get(
            REPORT_API,
            params=params,
            headers={"Referer": "https://data.eastmoney.com/"},
            timeout=30,
        )  # 已内置限流
        d = r.json()
        rows = d.get("data") or []
        if not rows:
            break
        all_records.extend(rows)
        if page >= (d.get("TotalPage", 1) or 1):
            break
    # 正向识别「查无结果」的真实原因，不把废码静默当成「无研报覆盖」
    if not all_records and code[:2] in ("43", "83", "87"):
        raise ValueError(
            f"{code} 属北交所老号段（43/83/87），东财研报库已不再按老码索引。"
            f"北交所存量标的已基本迁至 920xxx（如 832982→920982）；"
            f"请按股票名称反查现行 920 代码后重试。"
        )
    return all_records


def download_pdf(record: dict, target_dir: str = "./reports") -> str | None:
    """下载单份研报 PDF，返回保存路径或 None。"""
    info_code = record.get("infoCode", "")
    if not info_code:
        return None
    pub = (record.get("publishDate") or "")[:10]
    org = re.sub(r'[\\/:*?"<>|]', "_", record.get("orgSName") or "未知")[:40]
    title = re.sub(r'[\\/:*?"<>|]', "_", record.get("title", ""))[:80]
    fname = f"{pub}_{org}_{title}.pdf"
    target = Path(target_dir) / fname
    if target.exists():
        return str(target)
    url = PDF_TPL.format(info_code=info_code)
    r = common.em_get(
        url,
        headers={"Referer": "https://data.eastmoney.com/"},
        timeout=60,
    )
    if r.status_code == 200 and len(r.content) >= 1024:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(r.content)
        return str(target)
    return None
