"""内置扩展数据预设 — 概念/行业启动时只创建配置, 等待用户手动获取 (#199)。

设计原则:
  - 扩展数据通用逻辑零改动 (ExtConfig / fetch_and_ingest / API / 前端均不动)
  - 仅在本模块做「接口结构 → 本地 schema」的转换
  - 「已存在则跳过」: 绝不覆盖用户已有数据, 老用户零影响
  - 拉取失败只记 warning, 不阻断启动 (保持「没数据也能跑」)

种子数据来源 (概念/行业各自独立配置):
  - 概念: https://shy313.com/api/plugins/market_flow/exports/ths-concepts
  - 行业: https://shy313.com/api/plugins/market_flow/exports/ths-industries
作者更新数据只需改接口上的 JSON, 用户下次拉取自动同步, 无需发版。

接入点: app.main.lifespan → ensure_builtin_presets(store.data_dir)
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
    rows_to_parquet,
)

logger = logging.getLogger(__name__)

# 种子数据源 (各自独立配置, 作者维护; 均为「最近交易日快照」)
_CONCEPT_DATA_URL = "https://shy313.com/api/plugins/market_flow/exports/ths-concepts"
_INDUSTRY_DATA_URL = "https://shy313.com/api/plugins/market_flow/exports/ths-industries"
_POPULARITY_DATA_URL = "https://shy313.com/api/plugins/market_flow/exports/popularity"
_MONEY_FLOW_DATA_URL = "https://shy313.com/api/plugins/market_flow/exports/money-flow"

# 预设 id 单一权威: analysis.py 的内置菜单按同一常量指向数据源, 不重复字面量。
POPULARITY_PRESET_ID = "ext_rq_ths"
MONEY_FLOW_PRESET_ID = "ext_zjlx_ths"
_POPULARITY_ID = POPULARITY_PRESET_ID
_MONEY_FLOW_ID = MONEY_FLOW_PRESET_ID


# ---------------------------------------------------------------------------
# 预设定义: 字段结构 + 拉取配方
# ---------------------------------------------------------------------------

def _concept_preset() -> ExtConfig:
    """扩展概念 (ext_gn_ths)。

    接口结构: [{symbol, name, concepts: [概念1, 概念2, ...]}]
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
        description="同花顺概念分类 (启动仅创建配置, 在概念/行业页手动获取)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=_CONCEPT_DATA_URL,
            method="GET",
            schedule_minutes=1440,
            # enabled=False: ensure_builtin_presets 承诺启动不拉取, PullScheduler
            # 只调度 enabled 配置; 手动获取走 fetch_preset 独立路径不受影响 (#199)
            enabled=False,
        ),
    )


def _industry_preset() -> ExtConfig:
    """扩展行业 (ext_hy_ths)。

    接口结构: [{symbol, name, industries: [一级行业, 二级行业, 三级行业]}]
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
        description="同花顺行业分类 (启动仅创建配置, 在概念/行业页手动获取)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=_INDUSTRY_DATA_URL,
            method="GET",
            schedule_minutes=1440,
            # 同概念 preset: 出厂禁用, 避免启动即网络拉取 (#199)
            enabled=False,
        ),
    )


def _popularity_preset() -> ExtConfig:
    """人气排行 (ext_rq_ths)。

    接口结构: [{rank, symbol, name, heat, change_pct}]  (最近交易日快照)
    本地 schema: 人气排名 / 热度 / 涨跌幅(%) / 股票代码 / 股票简称 / symbol / code

    单位口径: 接口的 change_pct 是【百分数值】(实测与本地 enriched 收盘价
    涨跌幅 corr=1.0, 中位差 0.000000 个百分点), 例如 4.03 表示 +4.03%。
    字段名刻意避开 `change_pct`, 防止被误当成项目的「小数制」实时口径复用。
    """
    return ExtConfig(
        id=_POPULARITY_ID,
        label="人气排行",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("人气排名", "int", "人气排名"),
            ExtField("热度", "float", "热度"),
            ExtField("涨跌幅", "float", "涨跌幅(%)"),
        ],
        description="同花顺人气排行 (启动仅创建配置, 在人气排行页手动获取)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=_POPULARITY_DATA_URL,
            method="GET",
            schedule_minutes=1440,
            # 同概念/行业 preset: 出厂禁用, 避免启动即网络拉取 (#199)
            enabled=False,
        ),
    )


def _money_flow_preset() -> ExtConfig:
    """资金流向 (ext_zjlx_ths)。

    接口结构: [{symbol, name, net, inflow, outflow, rank, change_pct}]
    本地 schema: 资金排名 / 净流入(元) / 流入(元) / 流出(元) / 涨跌幅(%) / …

    单位口径: net/inflow/outflow 单位为元, 且 net == inflow - outflow。
    接口有部分标的 (停牌等) 该三项为 null, 这里保留 null 不补零, 前端显示为「—」,
    避免把「无数据」伪装成「无资金流入」。
    """
    return ExtConfig(
        id=_MONEY_FLOW_ID,
        label="资金流向",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("资金排名", "int", "资金排名"),
            ExtField("净流入", "float", "净流入(元)"),
            ExtField("流入", "float", "流入(元)"),
            ExtField("流出", "float", "流出(元)"),
            ExtField("涨跌幅", "float", "涨跌幅(%)"),
        ],
        description="个股资金流向 (启动仅创建配置, 在资金流向页手动获取)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=_MONEY_FLOW_DATA_URL,
            method="GET",
            schedule_minutes=1440,
            # 同概念/行业 preset: 出厂禁用, 避免启动即网络拉取 (#199)
            enabled=False,
        ),
    )


def _presets() -> list[ExtConfig]:
    return [
        _concept_preset(),
        _industry_preset(),
        _popularity_preset(),
        _money_flow_preset(),
    ]


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


def _num_or_none(value: object) -> float | None:
    """数值字段归一: 保留 None / 非有限值为 None, 不补零。

    接口对停牌等标的会返回 null, 或返回字符串数字。补零会把「无数据」
    伪装成真实的「0 净流入」, 因此这里一律保留空值。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    text = str(value).strip()
    if not text or text.casefold() in {"nan", "none", "null", "-"}:
        return None
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _int_or_none(value: object) -> int | None:
    num = _num_or_none(value)
    return None if num is None else int(num)


def _flatten_popularity_rows(raw_rows: list[dict]) -> list[dict]:
    """人气排行: 展平为本地 schema。

    [{rank, symbol, name, heat, change_pct}] → [{股票代码, 股票简称, 人气排名, 热度, 涨跌幅, symbol, code}]
    注: code 由 symbol 派生 (000001.SZ → 000001), 因 rows_to_parquet 不执行 code_map。
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "人气排名": _int_or_none(r.get("rank")),
            "热度": _num_or_none(r.get("heat")),
            # 接口已是百分数值 (4.03 = +4.03%), 不做二次换算
            "涨跌幅": _num_or_none(r.get("change_pct")),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


def _flatten_money_flow_rows(raw_rows: list[dict]) -> list[dict]:
    """资金流向: 展平为本地 schema。

    [{symbol, name, net, inflow, outflow, rank, change_pct}] →
    [{股票代码, 股票简称, 资金排名, 净流入, 流入, 流出, 涨跌幅, symbol, code}]
    net/inflow/outflow 单位元, 空值保留 (停牌标的接口返回 null)。
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "资金排名": _int_or_none(r.get("rank")),
            "净流入": _num_or_none(r.get("net")),
            "流入": _num_or_none(r.get("inflow")),
            "流出": _num_or_none(r.get("outflow")),
            "涨跌幅": _num_or_none(r.get("change_pct")),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


# 预设 id → 行转换函数。新增预设只在此登记一次, fetch_preset /
# ext_pull._apply_preset_flatten 都从这里取, 不各自维护一份 id 清单。
_FLATTEN_BY_ID = {
    "ext_gn_ths": _flatten_concept_rows,
    "ext_hy_ths": _flatten_industry_rows,
    POPULARITY_PRESET_ID: _flatten_popularity_rows,
    MONEY_FLOW_PRESET_ID: _flatten_money_flow_rows,
}


def preset_flatten(config_id: str):
    """返回预设 id 对应的行转换函数; 非预设返回 None。"""
    return _FLATTEN_BY_ID.get(config_id)


# ---------------------------------------------------------------------------
# 拉取执行 (复用 httpx, 不依赖 fetch_and_ingest 的 PullConfig 路径)
# ---------------------------------------------------------------------------

# 部分网络环境 (CDN/WAF/网关) 会把数组包成 {data: [...]}/{list: [...]}/{rows: [...]} 信封。
# 这里做一次兼容解包, 避免误判为「接口返回不是数组」。
_ENVELOPE_KEYS = ("data", "list", "rows", "result", "results")

# 上游是个人维护的低频接口, 实测会连续返回 502 (冷缓存/网关抖动), 稍后重试即成功。
# 只对 5xx 与网络层错误重试; 4xx 是请求本身的问题, 重试无意义且会放大上游压力。
_FETCH_ATTEMPTS = 3
_FETCH_BACKOFF_SECONDS = 1.5


async def _get_with_retry(client, url: str, headers: dict[str, str]):
    """GET + 有限重试, 返回 response。4xx 立即失败, 5xx/网络错误退避重试。"""
    import asyncio

    import httpx

    last_error: Exception | None = None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            resp = await client.get(url, headers=headers)
        except httpx.TransportError as e:
            last_error = e
        else:
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            last_error = httpx.HTTPStatusError(
                f"上游返回 {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        if attempt < _FETCH_ATTEMPTS - 1:
            await asyncio.sleep(_FETCH_BACKOFF_SECONDS * (attempt + 1))

    assert last_error is not None
    raise last_error


async def _fetch_json(url: str) -> list[dict]:
    """请求 JSON 接口, 返回行数组。超时 30s, 失败抛异常由调用方兜底。

    兼容两种上游返回形态:
      - 直接是数组: [{...}, ...]          → 原样返回
      - 信封包裹: {data: [{...}]} 等       → 自动解包

    结构解析放在重试之外: 「返回不是数组」是上游口径变化, 重试不会变好。
    """
    import httpx

    # 延迟导入避免与 ext_pull 循环依赖; 出站请求带 tsp 标识头
    from app.services.ext_pull import outbound_headers

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await _get_with_retry(client, url, outbound_headers())
        data = resp.json()

    if isinstance(data, list):
        return data

    # 信封解包: 在常见键里找第一个值为数组的
    if isinstance(data, dict):
        for key in _ENVELOPE_KEYS:
            inner = data.get(key)
            if isinstance(inner, list):
                return inner
        # 兜底: 遍历所有值, 取第一个数组
        for v in data.values():
            if isinstance(v, list):
                return v

    raise ValueError(
        f"接口返回不是数组 (type={type(data).__name__}), "
        f"响应预览: {str(data)[:200]}"
    )


async def _seed_one(config: ExtConfig, flatten, data_dir: Path) -> int:
    """拉取 + 转换 + 写入单个预设。返回写入行数。"""
    from datetime import date

    raw = await _fetch_json(config.pull.url)
    rows = flatten(raw)
    if not rows:
        raise ValueError(f"接口返回 0 行: {config.pull.url}")
    n = rows_to_parquet(rows, config, data_dir, snapshot_date=date.today())
    return n


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
      - 已存在则完全跳过 (绝不覆盖用户数据)
      - 只写 config.json, 失败只记 warning 不阻断启动
    """
    store = ExtConfigStore(data_dir)

    for config in _presets():
        existing = store.get(config.id)
        if existing is not None:
            # 用户已有此表 (老用户 / 自己重建过) → 一律不动
            continue
        try:
            store.upsert(config)
            logger.info("内置扩展表 %s 配置已就绪 (待用户手动获取数据)", config.id)
        except Exception as e:
            logger.warning("内置扩展表 %s 配置写入失败 (不影响启动): %s", config.id, e)


async def fetch_preset(config_id: str, data_dir: Path) -> int:
    """手动触发某个预设的数据拉取 (供 API 调用)。

    Raises:
        ValueError: config_id 不是内置预设
        Exception: 网络请求/解析/写入失败 (由 API 层转 HTTP 错误)
    """
    config = get_preset(config_id)
    if config is None:
        raise ValueError(f"未知的内置预设: {config_id}")

    flatten = preset_flatten(config_id)
    if flatten is None:
        raise ValueError(f"未知的内置预设: {config_id}")

    # 确保 config.json 存在 (用户可能从未启动过 ensure_builtin_presets)
    store = ExtConfigStore(data_dir)
    if store.get(config_id) is None:
        store.upsert(config)

    n = await _seed_one(config, flatten, data_dir)
    logger.info("内置扩展表 %s 手动拉取成功: %d 行", config_id, n)
    return n
