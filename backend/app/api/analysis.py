"""自定义分析菜单 API。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.fs_utils import atomic_write_text

from app.services.ext_presets import MONEY_FLOW_PRESET_ID, POPULARITY_PRESET_ID

router = APIRouter(prefix="/api/analysis-menus", tags=["analysis-menus"])


class AnalysisColumn(BaseModel):
    field: str
    label: str = ""
    type: Literal["string", "number", "percent", "amount", "date"] = "string"
    width: int | None = None
    sortable: bool = False
    precision: int | None = None
    format: str | None = None
    aggregate: Literal["count", "avg", "sum", "min", "max"] | None = None
    visible: bool = True


class DefaultSort(BaseModel):
    field: str
    order: Literal["asc", "desc"] = "desc"


class AnalysisMenu(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=64)
    icon: str = "chart"
    data_source: str = Field(..., min_length=1)
    template: Literal["dimension_rank", "ranking", "table"] = "dimension_rank"
    dimension_field: str | None = None
    rank_field: str | None = None
    group_columns: list[AnalysisColumn] = Field(default_factory=list)
    detail_columns: list[AnalysisColumn] = Field(default_factory=list)
    default_sort: DefaultSort | None = None
    visible: bool = True
    order: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    builtin: bool = False


class UpsertAnalysisMenu(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    icon: str = "chart"
    data_source: str = Field(..., min_length=1)
    template: Literal["dimension_rank", "ranking", "table"] = "dimension_rank"
    dimension_field: str | None = None
    rank_field: str | None = None
    group_columns: list[AnalysisColumn] = Field(default_factory=list)
    detail_columns: list[AnalysisColumn] = Field(default_factory=list)
    default_sort: DefaultSort | None = None
    visible: bool = True
    order: int = 0


class ReorderMenusReq(BaseModel):
    ids: list[str] = Field(..., min_length=1)


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _base_dir(request: Request) -> Path:
    return _data_dir(request) / "analysis_menus"


def _path(request: Request, menu_id: str) -> Path:
    return _base_dir(request) / f"{menu_id}.json"


def _load_saved(request: Request) -> list[AnalysisMenu]:
    base = _base_dir(request)
    if not base.exists():
        return []
    items: list[AnalysisMenu] = []
    for p in sorted(base.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            items.append(AnalysisMenu(**raw))
        except Exception:
            continue
    return items


def _ordered(items: list[AnalysisMenu]) -> list[AnalysisMenu]:
    return sorted(items, key=lambda m: (m.order, m.label, m.id))


def _save(request: Request, menu: AnalysisMenu) -> AnalysisMenu:
    now = datetime.now().isoformat()
    if not menu.created_at:
        menu.created_at = now
    menu.updated_at = now
    menu.builtin = False
    base = _base_dir(request)
    base.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        _path(request, menu.id),
        json.dumps(menu.model_dump(), ensure_ascii=False, indent=2),
    )
    return menu


def _ranking_menu(
    menu_id: str,
    label: str,
    data_source: str,
    rank_field: str,
    detail_columns: list[AnalysisColumn],
    order: int,
) -> AnalysisMenu:
    """内置榜单菜单: template=ranking 走扁平明细表, 不做分组。"""
    return AnalysisMenu(
        id=menu_id,
        label=label,
        icon="chart",
        data_source=data_source,
        template="ranking",
        rank_field=rank_field,
        group_columns=[],
        detail_columns=detail_columns,
        default_sort=DefaultSort(field=rank_field, order="asc"),
        visible=True,
        order=order,
        builtin=True,
    )


def _default_menus(request: Request) -> list[AnalysisMenu]:
    """自动生成的默认分析菜单 —— 内置榜单菜单 (不再扫描扩展数据配置)。

    历史上会扫描扩展数据配置, 对含「概念」字段的表自动生成一个「概念分析」菜单。
    该自动生成已关闭: 内置的概念分析页(/concept-analysis)已覆盖该场景, 自动菜单会造成
    导航重复。需要时用户可在「设置 → 扩展页面」手动创建。

    现在只返回「内置预设 ↔ 内置榜单」的固定配对 —— 人气排行和资金流向没有对应的
    内置页面, 因此由本函数直接声明菜单, 不写用户数据(不可误删、随代码升级), 也不与
    任何内置页面重复。用户若要用同 id 覆盖, 创建一个同名 saved 菜单即可 (list_menus
    以 saved 优先)。
    """
    return [
        _ranking_menu(
            "hot_rank",
            "人气排行",
            POPULARITY_PRESET_ID,
            "人气排名",
            [
                AnalysisColumn(field="人气排名", label="排名", type="number", precision=0, sortable=True),
                AnalysisColumn(field="股票简称", label="名称", type="string"),
                AnalysisColumn(field="股票代码", label="代码", type="string"),
                AnalysisColumn(field="热度", label="热度", type="number", precision=1, sortable=True),
                AnalysisColumn(field="涨跌幅", label="涨跌幅", type="percent", precision=2, sortable=True),
            ],
            order=300,
        ),
        _ranking_menu(
            "money_flow",
            "资金流向",
            MONEY_FLOW_PRESET_ID,
            "资金排名",
            [
                AnalysisColumn(field="资金排名", label="排名", type="number", precision=0, sortable=True),
                AnalysisColumn(field="股票简称", label="名称", type="string"),
                AnalysisColumn(field="股票代码", label="代码", type="string"),
                AnalysisColumn(field="净流入", label="净流入(元)", type="amount", precision=2, sortable=True),
                AnalysisColumn(field="流入", label="流入(元)", type="amount", precision=2),
                AnalysisColumn(field="流出", label="流出(元)", type="amount", precision=2),
                AnalysisColumn(field="涨跌幅", label="涨跌幅", type="percent", precision=2, sortable=True),
            ],
            order=310,
        ),
    ]


@router.get("")
def list_menus(request: Request):
    saved = _load_saved(request)
    saved_ids = {m.id for m in saved}
    defaults = [m for m in _default_menus(request) if m.id not in saved_ids]
    return {"items": _ordered(saved + defaults)}


@router.get("/{menu_id}")
def get_menu(request: Request, menu_id: str):
    for menu in _ordered(_load_saved(request) + _default_menus(request)):
        if menu.id == menu_id:
            return menu
    raise HTTPException(404, f"分析菜单 '{menu_id}' 不存在")


@router.post("/reorder")
def reorder_menus(request: Request, body: ReorderMenusReq):
    saved = {m.id: m for m in _load_saved(request)}
    defaults = {m.id: m for m in _default_menus(request)}
    for idx, menu_id in enumerate(body.ids):
        menu = saved.get(menu_id) or defaults.get(menu_id)
        if not menu:
            continue
        menu.order = idx
        _save(request, menu)
    return {"items": _ordered(_load_saved(request))}


@router.post("/{menu_id}")
def upsert_menu(request: Request, menu_id: str, body: UpsertAnalysisMenu):
    if not menu_id.replace("_", "").isalnum():
        raise HTTPException(400, "菜单标识只能包含字母、数字和下划线")
    existing = next((m for m in _load_saved(request) if m.id == menu_id), None)
    menu = AnalysisMenu(
        id=menu_id,
        created_at=existing.created_at if existing else None,
        **body.model_dump(),
    )
    return _save(request, menu)


@router.delete("/{menu_id}")
def delete_menu(request: Request, menu_id: str):
    p = _path(request, menu_id)
    if not p.exists():
        raise HTTPException(404, f"分析菜单 '{menu_id}' 不存在或为默认菜单")
    p.unlink()
    return {"status": "deleted"}
