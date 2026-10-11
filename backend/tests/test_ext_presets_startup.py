"""内置概念/行业 preset 启动契约 (#199 及后续演进)。

ensure_builtin_presets 的契约: 只创建 config.json, 不拉取数据, 等待用户在
概念/行业页手动获取; 且出厂【不预置 pull 拉取配方】— 手动获取走 fetch_preset
的 fuyao 鉴权 + 板块反转专属路径, 通用定时拉取对 fuyao 既无鉴权也无反转,
预置配方只会误导与周期性失败。

历史配方回收: 换源前的自维护种子源 URL、过渡期的 fuyao 目录标记 URL 均视为
出厂值, 启动时把整个 pull 回收为 None; 用户自定义过 URL 的配置一律不碰。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.services.ext_data import ExtConfigStore
from app.services.ext_presets import (
    _RETIRED_FACTORY_URLS,
    _concept_preset,
    _industry_preset,
    ensure_builtin_presets,
)

_PRESET_IDS = ("ext_gn_ths", "ext_hy_ths")


def test_builtin_presets_ship_without_pull() -> None:
    """出厂 preset 不带 pull 配方: 无 URL、无调度、无 enabled 开关。"""
    for preset in (_concept_preset(), _industry_preset()):
        assert preset.pull is None, (
            f"{preset.id} 出厂预置了 pull 配方, 通用定时拉取对 fuyao 不可用, 违反启动契约"
        )


def test_ensure_builtin_presets_writes_configs_without_pull(tmp_path: Path) -> None:
    """全新数据目录: 启动只落无 pull 的配置, scheduler 扫描后无任务可建。"""
    asyncio.run(ensure_builtin_presets(tmp_path))

    store = ExtConfigStore(tmp_path)
    for cid in _PRESET_IDS:
        config = store.get(cid)
        assert config is not None, f"{cid} 配置未创建"
        assert config.pull is None


def test_ensure_builtin_presets_keeps_custom_user_pull(tmp_path: Path) -> None:
    """用户自定义过 pull (URL 不等于历代出厂值) → 一律不动, 即使 enabled=True。"""
    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)
    config = store.get("ext_gn_ths")
    assert config is not None

    from app.services.ext_data import PullConfig

    config.pull = PullConfig(
        url="https://example.com/my-own-concepts", enabled=True
    )
    store.upsert(config)

    asyncio.run(ensure_builtin_presets(tmp_path))

    refreshed = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert refreshed is not None and refreshed.pull is not None
    assert refreshed.pull.url == "https://example.com/my-own-concepts"
    assert refreshed.pull.enabled is True, "自定义拉取配置被静默改写, 违反「绝不覆盖」原则"


def test_ensure_builtin_presets_reclaims_retired_factory_urls(tmp_path: Path) -> None:
    """pull.url 命中历代废弃出厂值 (shy313 种子 / fuyao 目录标记) → 整体回收为 None。"""
    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)

    from app.services.ext_data import PullConfig

    for cid in _PRESET_IDS:
        config = store.get(cid)
        assert config is not None
        for retired_url in _RETIRED_FACTORY_URLS[cid]:
            config.pull = PullConfig(url=retired_url, enabled=True)  # 含 #199 前出厂默认
            store.upsert(config)

            asyncio.run(ensure_builtin_presets(tmp_path))

            refreshed = ExtConfigStore(tmp_path).get(cid)
            assert refreshed is not None
            assert refreshed.pull is None, (
                f"{cid} 废弃出厂配方 {retired_url} 未回收, 通用定时拉取将持续失败"
            )


def test_ensure_builtin_presets_syncs_retired_factory_description(tmp_path: Path) -> None:
    """description 命中出厂前缀 (历代出厂文案, 如「首次启动自动拉取」) → 同步为当前文案;
    用户改成完全不同描述的 (不以出厂前缀开头) 不动。"""
    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)

    config = store.get("ext_gn_ths")
    assert config is not None
    old_factory_desc = "同花顺概念分类 (首次启动自动拉取, 可在扩展数据页手动更新)"
    config.description = old_factory_desc
    store.upsert(config)

    asyncio.run(ensure_builtin_presets(tmp_path))

    refreshed = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert refreshed is not None
    expected = _concept_preset().description
    assert refreshed.description == expected, "旧出厂文案未同步 (「首次启动自动拉取」与实际行为不符)"

    # 用户自定义描述 (非出厂前缀) → 不动
    refreshed.description = "我自己的概念表, 别动"
    store.upsert(refreshed)
    asyncio.run(ensure_builtin_presets(tmp_path))
    final = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert final is not None
    assert final.description == "我自己的概念表, 别动"
