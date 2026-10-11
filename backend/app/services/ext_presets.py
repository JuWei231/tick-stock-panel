"""内置扩展数据预设 — 概念/行业启动时只创建配置, 等待用户手动获取 (#199)。

设计原则:
  - 扩展数据通用逻辑零改动 (ExtConfig / fetch_and_ingest / API / 前端均不动)
  - 仅在本模块做「接口结构 → 本地 schema」的转换
  - 「已存在则跳过」: 绝不覆盖用户已有数据, 老用户零影响
  - 拉取失败只记 warning, 不阻断启动 (保持「没数据也能跑」)

种子数据来源 (概念/行业共用 fuyao 同花顺指数接口, 2026-10 起替代自维护导出):
  - 板块清单: /api/a-share-index/catalog/ths-index-list (tag=cn_concept / industry)
  - 板块成分: /api/a-share-index/constituents/ths-stock-list (单板块一次)
  接口方向为「板块→股票」, 此处反转为「股票→板块名清单」, 行结构与旧种子源
  同构 ([{symbol, name, concepts|industries: [...]}]) → flatten 与下游零改动。
  API Key 复用 fuyao 插件同一凭据; 全量约 390+320 次成分股调用, 8 并发可承受。

接入点: app.main.lifespan → ensure_builtin_presets(store.data_dir)
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date
from pathlib import Path

import httpx

from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
    rows_to_parquet,
)

logger = logging.getLogger(__name__)

# fuyao 同花顺指数接口 (鉴权 X-api-key, 信封 {code, message, data:{item:[...]}})
_FUYAO_BASE = "https://fuyao.aicubes.cn"
_FUYAO_CATALOG_PATH = "/api/a-share-index/catalog/ths-index-list"
_FUYAO_CONSTITUENTS_PATH = "/api/a-share-index/constituents/ths-stock-list"
# 成分股逐板块拉取: 全量 710 次调用, 8 并发 + 单板块失败重试 1 次 (实测无 429)
_FUYAO_CONCURRENCY = 8
_FUYAO_TIMEOUT_S = 20.0
_FUYAO_BOARD_RETRIES = 1

# 2026-10 换源前的出厂种子 URL (自维护导出接口, 已下线)。仅当现有配置的
# pull.url 精确等于旧出厂值 (未被用户改过) 时, 启动迁移为 fuyao 配方。
_LEGACY_PULL_URLS = {
    "ext_gn_ths": "https://shy313.com/api/plugins/market_flow/exports/ths-concepts",
    "ext_hy_ths": "https://shy313.com/api/plugins/market_flow/exports/ths-industries",
}


# ---------------------------------------------------------------------------
# 预设定义: 字段结构 + 拉取配方
# ---------------------------------------------------------------------------

def _concept_preset() -> ExtConfig:
    """扩展概念 (ext_gn_ths)。

    接口结构 (fuyao 反转后): [{symbol, name, concepts: [概念1, 概念2, ...]}]
    本地 schema: 股票代码 / 股票简称 / 所属概念(分号拼接) / symbol / code
    """
    return ExtConfig(
        id="ext_gn_ths",
        label="扩展概念",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("所属概念", "string", "所属概念"),
        ],
        description="同花顺概念分类 (fuyao 同花顺指数接口; 启动仅创建配置, 在概念/行业页手动获取)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            # url 仅作配方标识: 实际拉取走 fetch_preset 的 fuyao 反转路径
            url=f"{_FUYAO_BASE}{_FUYAO_CATALOG_PATH}?tag=cn_concept",
            method="GET",
            schedule_minutes=1440,
            # enabled=False: ensure_builtin_presets 承诺启动不拉取, PullScheduler
            # 只调度 enabled 配置; 手动获取走 fetch_preset 独立路径不受影响 (#199)
            enabled=False,
        ),
    )


def _industry_preset() -> ExtConfig:
    """扩展行业 (ext_hy_ths)。

    接口结构 (fuyao 反转后): [{symbol, name, industries: [行业板块名, ...]}]
    — fuyao 为扁平板块清单, 无一/二/三级层级路径, 单级粒度。
    本地 schema: 股票代码 / 股票简称 / 所属同花顺行业(横杠拼接) / symbol / code
    """
    return ExtConfig(
        id="ext_hy_ths",
        label="扩展行业",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("所属同花顺行业", "string", "所属同花顺行业"),
        ],
        description="同花顺行业分类 (fuyao 同花顺指数接口, 扁平板块无层级; 启动仅创建配置, 在概念/行业页手动获取)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=f"{_FUYAO_BASE}{_FUYAO_CATALOG_PATH}?tag=industry",
            method="GET",
            schedule_minutes=1440,
            # 同概念 preset: 出厂禁用, 避免启动即网络拉取 (#199)
            enabled=False,
        ),
    )


def _presets() -> list[ExtConfig]:
    return [_concept_preset(), _industry_preset()]


# ---------------------------------------------------------------------------
# 接口结构 → 本地 schema 转换 (仅预设使用)
# ---------------------------------------------------------------------------

def _symbol_to_code(symbol: str) -> str:
    """symbol (000001.SZ) → code (000001)。"""
    return symbol.split(".", 1)[0] if "." in symbol else symbol


def _dimension_label(value: object) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def _flatten_concept_rows(raw_rows: list[dict]) -> list[dict]:
    """概念: concepts 数组 → 分号拼接成「所属概念」字符串。

    [{symbol, name, concepts:[...]}] → [{股票代码, 股票简称, 所属概念, symbol, code}]
    注: code 由 symbol 派生 (000001.SZ → 000001), 因 rows_to_parquet 不执行 code_map。
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        concepts = r.get("concepts") or []
        labels = [label for c in concepts if (label := _dimension_label(c))]
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "所属概念": ";".join(labels),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


def _flatten_industry_rows(raw_rows: list[dict]) -> list[dict]:
    """行业: industries 数组 → 横杠拼接成「所属同花顺行业」字符串。

    [{symbol, name, industries:[...]}] → [{股票代码, 股票简称, 所属同花顺行业, symbol, code}]
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        inds = r.get("industries") or []
        labels = [label for i in inds if (label := _dimension_label(i))]
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "所属同花顺行业": "-".join(labels),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


# ---------------------------------------------------------------------------
# fuyao 拉取: 板块清单 + 逐板块成分股 → 反转成「股票→板块名」行
# (复用 httpx, 不依赖 fetch_and_ingest 的 PullConfig 路径)
# ---------------------------------------------------------------------------


def _fuyao_api_key() -> str:
    """取 fuyao API Key — 与 app/plugins/fuyao/provider.py 共用同一凭据存储
    (secrets.json 的 fuyao_api_key 优先, 后备 FUYAO_API_KEY 环境变量)。
    不 import 插件模块, 保持核心服务不依赖插件; 两处字段名需同步维护。
    """
    from app import secrets_store

    return secrets_store.get_env_backed_secret("fuyao_api_key", "FUYAO_API_KEY")


async def _fuyao_get_items(
    client: httpx.AsyncClient, path: str, params: dict
) -> list[dict]:
    """GET + fuyao 信封解包 ({code, message, data:{item:[...]}}), 返回 item 数组。"""
    resp = await client.get(path, params=params)
    if resp.status_code != 200:
        raise ValueError(f"fuyao HTTP {resp.status_code}: {path}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise ValueError(f"fuyao 响应不是 JSON: {path}") from e
    if payload.get("code") != 0:
        raise ValueError(
            f"fuyao code={payload.get('code')}: {payload.get('message') or path}"
        )
    items = (payload.get("data") or {}).get("item")
    if not isinstance(items, list):
        raise ValueError(f"fuyao 响应缺少 data.item: {path}")
    return items


async def _fetch_dimension_rows(
    tag: str,
    list_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict]:
    """fuyao 板块清单 + 逐板块成分股 → 反转为「股票→板块名清单」行。

    返回与旧种子源同构: [{symbol, name, list_key: [板块名, ...]}];
    每只股票的板块名顺序跟随目录顺序, 保证拼接结果稳定。

    容错: 单板块成分股失败重试 1 次仍失败则跳过 (记 warning, 缺一两个板块
    远好于整次拉取失败); 目录失败或全部板块失败才抛错。

    transport 仅供测试注入 MockTransport, 生产传 None。
    """
    api_key = _fuyao_api_key()
    if not api_key:
        raise ValueError(
            "未配置 fuyao API Key — 在 设置 → 数据源 填写, 或配置环境变量 FUYAO_API_KEY"
        )

    # 延迟导入避免与 ext_pull 循环依赖; 出站请求带 tsp 标识头
    from app.services.ext_pull import outbound_headers

    sem = asyncio.Semaphore(_FUYAO_CONCURRENCY)

    async with httpx.AsyncClient(
        base_url=_FUYAO_BASE,
        headers={"X-api-key": api_key, **outbound_headers()},
        timeout=_FUYAO_TIMEOUT_S,
        transport=transport,
    ) as client:
        boards = await _fuyao_get_items(client, _FUYAO_CATALOG_PATH, {"tag": tag})
        boards = [
            b
            for b in boards
            if isinstance(b, dict) and (b.get("thscode") or "").strip()
            and (b.get("name") or "").strip()
        ]
        if not boards:
            raise ValueError(f"fuyao 板块目录为空: tag={tag}")

        async def _members(board: dict) -> list[dict] | None:
            """单板块成分股; 失败重试后仍失败返回 None (由聚合方跳过)。"""
            params = {"thscode": board["thscode"]}
            async with sem:
                for attempt in range(_FUYAO_BOARD_RETRIES + 1):
                    try:
                        return await _fuyao_get_items(
                            client, _FUYAO_CONSTITUENTS_PATH, params
                        )
                    except Exception:  # noqa: BLE001 — 单板块失败容忍: 重试后跳过
                        if attempt == _FUYAO_BOARD_RETRIES:
                            return None
                        await asyncio.sleep(0.5)

        members_list = await asyncio.gather(*(_members(b) for b in boards))

    stocks: dict[str, dict] = {}
    failed: list[str] = []
    for board, members in zip(boards, members_list, strict=True):
        if members is None:
            failed.append(board["name"])
            continue
        for m in members:
            sym = (m.get("thscode") or "").strip()
            if not sym:
                continue
            entry = stocks.get(sym)
            if entry is None:
                entry = {"symbol": sym, "name": m.get("name") or "", list_key: []}
                stocks[sym] = entry
            # 目录内板块名去重 (同名板块不叠加)
            if board["name"] not in entry[list_key]:
                entry[list_key].append(board["name"])

    if failed:
        logger.warning(
            "fuyao 板块成分股拉取失败 %d/%d (已跳过): %s",
            len(failed),
            len(boards),
            "、".join(failed[:5]),
        )
    if not stocks:
        raise ValueError(f"fuyao 全部板块成分股拉取失败: tag={tag}")
    return list(stocks.values())


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def get_preset(config_id: str) -> ExtConfig | None:
    """按 id 取预设定义 (供 API 层校验 id 合法性)。"""
    for c in _presets():
        if c.id == config_id:
            return c
    return None


async def ensure_builtin_presets(data_dir: Path) -> None:
    """启动时: 为缺失的预设创建 config.json (含 pull 配置), 但【不拉取数据】。

    设计: 数据获取改为用户在概念/行业页手动点「获取数据」触发, 避免启动时
    网络请求阻塞, 也避免「自动拉取」与「用户自主控制」的预期冲突。

    安全保证:
      - 已存在则跳过创建 (绝不覆盖用户数据); 唯一例外: pull.url 仍是旧出厂
        种子值时同步为 fuyao 配方并回收 enabled (用户自定义过 URL 的配置不碰)
      - 只写 config.json, 失败只记 warning 不阻断启动
    """
    store = ExtConfigStore(data_dir)

    for config in _presets():
        existing = store.get(config.id)
        if existing is not None:
            # 用户已有此表 (老用户 / 自己重建过) → 不重建; 但若拉取配方还停在
            # 换源前的出厂种子 URL, 定向迁移到 fuyao (避免 UI 显示已下线的旧源)。
            legacy = _LEGACY_PULL_URLS.get(config.id)
            factory = config.pull.url if config.pull is not None else None
            if (
                existing.pull is not None
                and factory is not None
                and existing.pull.url in (legacy, factory)
            ):
                # url 等于旧出厂种子值或当前出厂值 → 视为出厂配置 (非用户自定义):
                # 同步为 fuyao 配方, 并回收 enabled (#199 之前出厂遗留 True; 换源后
                # 通用定时拉取既不带 fuyao 鉴权也不做板块反转, 只会每周期失败报错;
                # fetch_preset 手动获取路径不受 enabled 影响)。用户自定义 URL 不碰。
                existing.pull.url = factory
                existing.pull.enabled = False
                store.upsert(existing)
                logger.info(
                    "内置扩展表 %s 拉取配方已同步为 fuyao (出厂态回收为手动获取)",
                    config.id,
                )
            continue
        try:
            store.upsert(config)
            logger.info("内置扩展表 %s 配置已就绪 (待用户手动获取数据)", config.id)
        except Exception as e:  # noqa: BLE001 — 启动契约: 预设写入失败不阻断
            logger.warning("内置扩展表 %s 配置写入失败 (不影响启动): %s", config.id, e)


async def fetch_preset(config_id: str, data_dir: Path) -> int:
    """手动触发某个预设的数据拉取 (供 API 调用)。

    数据源为 fuyao 同花顺指数接口 (板块清单 + 成分股反转), 行结构与旧种子源
    同构, flatten / parquet 落盘路径不变。

    Raises:
        ValueError: config_id 不是内置预设
        Exception: 网络请求/解析/写入失败 (由 API 层转 HTTP 错误)
    """
    if config_id == "ext_gn_ths":
        tag, list_key, flatten = "cn_concept", "concepts", _flatten_concept_rows
    elif config_id == "ext_hy_ths":
        tag, list_key, flatten = "industry", "industries", _flatten_industry_rows
    else:
        raise ValueError(f"未知的内置预设: {config_id}")
    config = get_preset(config_id)
    assert config is not None  # 上面分支已保证

    # 确保 config.json 存在 (用户可能从未启动过 ensure_builtin_presets)
    store = ExtConfigStore(data_dir)
    if store.get(config_id) is None:
        store.upsert(config)

    raw = await _fetch_dimension_rows(tag, list_key)
    rows = flatten(raw)
    if not rows:
        raise ValueError(f"接口返回 0 行: {config_id}")
    n = rows_to_parquet(rows, config, data_dir, snapshot_date=date.today())
    logger.info("内置扩展表 %s 手动拉取成功: %d 行", config_id, n)
    return n
