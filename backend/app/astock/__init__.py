"""a-stock-data → tick-stock-panel 集成核心（数据层）。

从 SKILL.md 移植的零鉴权取数端点，**纯 httpx + 标准库实现**、可独立测试，
目标运行环境为 tick-stock-panel 后端 ``backend/app/astock/``（进程内模块，
其 backend 已内置 httpx/pandas/fastexcel，本包零新增依赖）。

P0 试点覆盖（对应 SKILL.md 章节）：
- ``eastmoney_reports`` / ``download_pdf`` — §2.1 研报 + 评级 + 三年 EPS 预测（东财 reportapi）
- ``margin_trading`` — §4.1 融资融券明细（东财 datacenter）
- ``cls_telegraph`` — §5.2 财联社电报（v1 API + 本地签名，零 key）

包内模块一律使用**相对导入**，整体拷入 TSP 时包名从 ``astock`` 变为
``app.astock`` 无需改内部引用。

数据契约对齐：东财请求统一走 ``common.em_get()`` 进程级串行节流（防封 IP）；
个股代码入口统一 ``tickers.norm_ticker()`` 归一化为纯 6 位。
"""
from .margin import margin_trading
from .reports import download_pdf, eastmoney_reports
from .telegraph import cls_telegraph
from .tickers import em_market_code, em_secid, get_prefix, norm_ticker

__version__ = "0.1.0"

__all__ = [
    "norm_ticker",
    "get_prefix",
    "em_market_code",
    "em_secid",
    "eastmoney_reports",
    "download_pdf",
    "margin_trading",
    "cls_telegraph",
    "__version__",
]
