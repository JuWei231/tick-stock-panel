"""a-stock-data 集成 API（自定义扩展，L2）— 研报 / 融资融券 / 财联社电报。

放在 ``app/custom/`` 下由 extensions loader 自动发现并挂载，无需修改 ``app/main.py``。
路由前缀 ``/api/astock``。取数能力来自 ``app/astock``（由 a-stock-data SKILL.md 移植，
Apache-2.0；来源与偏差记录见 ``backend/app/astock/NOTICE.md``）。

错误语义：代码格式错误 / 指数码 → HTTP 400；上游取数失败 → 200 信封
``state=error``（对齐 dragon_tiger），由前端降级提示。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)
from app.services import astock_service

EXTENSION_ID = "astock.integration"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def _data_dir(request: Request) -> Path:
    return Path(request.app.state.repo.store.data_dir)


def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/astock", tags=["astock"])

    @router.get("/research/reports/{symbol}")
    def reports(
        request: Request,
        symbol: str,
        max_pages: int = Query(default=3, ge=1, le=5),
    ):
        """个股研报列表（东财 reportapi）：标题 / 机构 / 评级 / 三年 EPS 预测。"""
        try:
            return astock_service.get_reports(
                _data_dir(request), symbol, max_pages=max_pages
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))

    @router.get("/margin/{symbol}")
    def margin(
        request: Request,
        symbol: str,
        limit: int = Query(default=30, ge=1, le=120),
    ):
        """融资融券明细（日级，东财 datacenter）：金额单位元。"""
        try:
            return astock_service.get_margin(
                _data_dir(request), symbol, limit=limit
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))

    @router.get("/news/telegraph")
    def telegraph(limit: int = Query(default=50, ge=1, le=100)):
        """财联社电报（全市场实时快讯，北京时间）；45s 短 TTL 内存缓存。"""
        return astock_service.get_telegraph(limit=limit)

    @router.get("/status")
    def status(request: Request) -> dict:
        """集成状态：缓存目录与支持的数据集。"""
        return {
            "extension": EXTENSION_ID,
            "datasets": ["research.reports", "margin", "news.telegraph"],
            "cache_dir": str(_data_dir(request) / "astock"),
        }

    registrar.include_router(router)


def startup(context: ExtensionContext) -> None:
    # 无启动期资源；路由经 request.app.state.repo.store.data_dir 按需取数据目录。
    _ = context
