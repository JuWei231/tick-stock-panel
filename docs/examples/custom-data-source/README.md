# 自定义数据源 mock 联调示例

这个目录提供一个本地 mock HTTP 数据源,用于验证项目的自定义数据源接入链路。

## 运行 mock 服务

```bash
cd docs/examples/custom-data-source
python mock_server.py
```

服务默认监听:

```text
http://127.0.0.1:3021
```

端点:

| 端点 | 数据 |
| --- | --- |
| `/daily` | 日K |
| `/adj_factor` | 除权因子 |
| `/realtime` | 全市场实时快照 |

## 接入项目

复制示例 YAML 到运行数据目录:

```bash
mkdir -p data/data_sources
cp docs/examples/custom-data-source/mock_source.yaml data/data_sources/mock_source.yaml
```

然后在项目里打开:

```text
设置 -> 数据源 -> 重新加载
```

选择 `mock_source` 后,可用「试拉测试」验证 `daily`、`adj_factor` 和 `realtime`。

完整说明见 [../../custom-data-source.md](../../custom-data-source.md)。

## a-stock-data 桥接示例（真实数据源）

本目录还提供 `a_stock_data_source.yaml`，对接
[a-stock-data](https://github.com/shy3130/a-stock-data) 的 `bridge_server.py`
（默认 `http://127.0.0.1:3022`）。

先启动桥接服务（a-stock-data 仓库内，独立 venv）:

```powershell
cd <a-stock-data>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-bridge.txt
.\start_bridge.ps1
```

再复制配置并重新加载:

```powershell
Copy-Item docs\examples\custom-data-source\a_stock_data_source.yaml data\data_sources\a_stock_data_source.yaml -Force
curl -X POST http://127.0.0.1:3018/api/settings/data-sources/reload
```

该配置覆盖 `daily` / `adj_factor` / `realtime` / `minute` / `full_minute` / `financial`
六个数据集；深度盘口（depth5）暂无数据集契约，仍需 TickFlow。
字段映射与参数说明见 a-stock-data 仓库的 `BRIDGE_FOR_TICK_STOCK_PANEL.md`。
