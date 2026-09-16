# PIT 股本修复：历史市值口径缺陷

> 本文记录一个**已修复**的数据口径缺陷：`close × total_shares` 计算出的历史市值系统性偏小。
> 记录目的是让后续贡献者理解该换算为何存在、边界在哪，避免被当作冗余代码删除。
>
> 相关代码：`backend/app/share_capital.py`、`backend/app/backtest/matrix.py`、
> `backend/app/services/screener.py`、`backend/app/backtest/engine.py`、
> `backend/app/tickflow/repository.py`
>
> 另见第 7 节：一次**同批发生但彼此无关**的启动故障修复（孤儿发布标记探测）。
> 两者没有因果关系，只是同时改动。

> ⚠️ **合并说明（2026-09-15）**：本文 §4–§6 描述的**实现已被取代**。
> 合并 `tick-stock-panel-main` 时采用了该分支的 PIT 股本实现
> （`apply_historical_shares` / `build_share_matrices` / `market_cap_expr`：
> 基于 `data/financials/shares` 的真实公告股本做时点连接，市值改用不复权 `raw_close`），
> 本文基于除权因子推导的 `apply_historical_share_counts` 及相关改动已从代码中移除。
>
> - **§1–§3 的缺陷分析与影响面测量仍然成立**，保留作为口径缺陷的记录。
> - **§4–§6 仅存档**：其中的函数名与实现细节已不对应现存代码。
> - §6 遗留项"`strategy/engine.py` / `monitor.py` 仍为快照口径"在合并后已由
>   `market_cap_expr` + `raw_close` 覆盖。
> - §7 的启动故障修复与本缺陷无关，代码**保留**。
>
> ⚠️ 另需注意：新实现依赖 `data/financials/shares/part.parquet`，
> 该目录当前为空，因此历史股本换算在本地数据同步前不会生效（见文末「合并后状态」）。

## 1. 缺陷现象

`[20,30]亿` 这类市值区间在小市值策略里直接决定选股结果。修复前该区间的历史候选池是错的。

实测：同一段 12 年窗口内，用修复前口径与修复后口径分别取 `[20,30]亿` 的交集：

```
命中集合重叠率（每 20 日采样）：均值 0.697，最低 0.646
2021-01-04 单日：修复前 549 只 → 修复后 610 只，重叠仅 381 只
```

即**候选池约 30% 不一致**，分档越靠近区间边界偏差越大。

## 2. 根因

平台同时存在两种口径的数据，且在**市值计算处被直接相乘**：

| 数据 | 口径 | 来源 |
| --- | --- | --- |
| `close` | **前复权价**（已按最新股本口径折算） | enriched `kline_daily_enriched` |
| `total_shares` / `float_shares` | **最新快照**（单一日期，非历史值） | `instruments.parquet` |

`instruments.parquet` 的 `as_of` 只有一个取值（全表单日快照），因此它提供的是"今天"的股本，
而 `close` 是"把历史价格换算到今天股本口径"的结果。两者相乘时，历史股本被隐式当成与今天相同：

```
市值_t（错误） = 前复权价_t × 最新股本
```

由于前复权价已把历史价格压低，再乘最新股本，**历史市值被系统性算小**。

同一缺陷在平台内有多个落点。**本次修复只覆盖回测/选股的时间序列路径**，其余路径的状态如下
（不要误读为"全部已修"）：

| 位置 | 用途 | 本次状态 |
| --- | --- | --- |
| `backtest/matrix.py` `_build_basic_filter_mask_uncached` | 回测 `market_cap` 边界 | ✅ 已修（逐日股本矩阵） |
| `backtest/matrix.py` `_instrument_axis_values` | 矩阵 `total_shares`/`float_shares` 字段 | ✅ 已修（改为逐日） |
| `services/screener.py` `_load_enriched_history` | 选股历史窗口（时间序列） | ✅ 已修 |
| `services/screener.py` `_compute_enriched_full` | 选股单一目标日 | ⚪ 无需修（当日快照即正确值） |
| `strategy/engine.py` `_basic_filter_expr` | `market_cap_min/max` 选股门槛 | ⚠️ **仍为快照**（见下） |
| `strategy/monitor.py` | 监控规则市值条件 | ⚠️ **仍为快照**（实时监控按当日快照，正确；历史回看会偏小） |

`strategy/engine.py` 这条需要特别注意：选股页走该路径，其中 `market_cap_min/max` 门槛
**仍是快照口径**。因此若某策略依赖 `basic_filter.market_cap_*`，其结果仍受本缺陷影响。
（`custom_small_cap_rotation` 系列不受影响：它们在 META 中把 `market_cap_*` 显式置 `None`，
市值判断全部在 `compute_signals` 内经矩阵完成，走的是已修路径。）

`float_shares` 同病（影响流通市值）。`turnover_rate` 另有一条独立的历史股本覆盖路径
（`apply_historical_float_shares`），但它只在 `data/financials/shares` 存在数据时生效，
而该目录在本地为空，因此修复前它同样退化为快照口径。

## 3. 影响面

**幅度随回测窗口起点而增大**（这是关键，不要引用单一数字）：

| 回测窗口起点 | 中位低估 | p75 | p90 | 起点前无任何送转的标的 |
| --- | --- | --- | --- | --- |
| 1 年前 | 1% | 3% | 18% | 29.6% |
| 2 年前 | 2% | 7% | 29% | 21.5% |
| 4 年前 | 6% | 21% | 37% | 12.5% |
| 7 年前 | 12% | 34% | 54% | 6.2% |
| 10 年前 | 27% | 49% | 65% | 1.6% |

⚠️ **常见误读**：把"全历史累积"当成普遍幅度。若以 12 年前为界统计，中位放大系数约 1.667、
p90 约 4.478，即"中位低估 40%"。但那是**十余年累积**的结果，不代表 1–2 年回测的实际影响。
估算影响时必须使用对应窗口起点的数字。

数据规模（`data/adj_factor/all.parquet`，本地实测）：43,083 起除权事件，涉及 5,335 只标的，
其中大比例送转（`ex_factor > 1.2`）6,541 起、3,418 只。

**受影响的功能**：主要是直接使用 `total_shares` 的策略与门槛。实际排查结果：

- 内置策略中仅 `trend_breakout` 声明了 `market_cap_min: 20e8` → 走
  `strategy/engine.py:1571`，**该路径本次未修**，故该策略仍受影响
- 因子注册表中的 `log_float_mv` 是**流通**市值口径，不走 `total_shares`
- 自定义策略 `custom_small_cap_rotation` / `_v2` 在 `compute_signals` 内直接使用
  `total_shares` → 走已修的矩阵路径，**已受益**

因此：**本次修复使自定义小市值轮动策略与回测矩阵的市值口径恢复正确；
凡走 `basic_filter.market_cap_*`（`engine.py:1571/1575`）或监控规则
（`monitor.py:1484`）的路径仍为快照口径。**

## 4. 修复方式

### 4.1 换算关系

除权因子表 `data/adj_factor/all.parquet`（`symbol, trade_date, ex_factor`，
`ex_factor` 为每次除权事件的 pre/post 比值）记录了股本变动。由此可还原当日股本：

```
suffix_cumprod_t = cumprod_at_last_event_≤_t / total_cumprod
shares_t         = shares_latest × suffix_cumprod_t
```

其中 `cumprod_at_last_event_≤_t` 是"截至 t（含当日）的累积除权因子"，
`total_cumprod` 是该标的全量累积因子。

该关系与 `indicators/pipeline.py::_apply_adj_factor` 的前复权口径同源：

```
前复权:  adjusted = raw × cumprod_≤t / total_cumprod
恒等式:  前复权价_t × 当日股本_t == 原始价_t × 最新股本
```

**方向容易写反**（本修复过程中确实写反过一次）：减仓方向是 `shares_t = snapshot × suffix`
（suffix ≤ 1，历史股本更小）；写成 `snapshot / suffix` 会在历史日期上放大股本，是错的。

### 4.2 落点：数据边界，而非公式

修复选择在**数据边界**把 `total_shares` / `float_shares` 换算成当日值，
而不是去改四处市值公式。理由：改公式只能修好被改到的那几处，
`total_shares` 在策略、监控、选股页仍会返回快照值，口径继续分叉。

核心函数：`backend/app/share_capital.py::apply_historical_share_counts`
（纯函数，输入行情帧 + 除权因子，输出已换算的股本列；缺因子或非股票资产时原样透传）。

`float_shares` 仍需叠加财务口径的历史公告股本（`apply_historical_float_shares`）：
那是独立的 PIT 来源，因子换算只补足"快照 vs 当日"的差额。

### 4.3 回测矩阵的特殊性

`backtest/matrix.py` 原本把 `total_shares` / `float_shares` 当 **vector field**，
用 `fields[name][:] = values[0]` **按常数广播到整个时间轴** —— 即平台层根本不表示
"历史某日的股本"。因此修复必须让它变成真正的 `(time, asset)` 矩阵：
新增 `_pit_share_axis_values`，把 `(symbol, date)` 展平后调用同一换算函数，
再 reshape 成矩阵轴（注意展平是 symbol-major，需 reshape 成 `(asset, time)` 后转置）。

### 4.4 缓存失效

矩阵磁盘缓存中存有旧逻辑算出的 `total_shares`，因此
`_DIRECT_MATRIX_LOADER_VERSION` 由 **4 → 5**。

**代价：首次回测会重建矩阵缓存**，原有 `data/.backtest_matrix_cache` 目录不再命中。
这是有意为之，否则会继续复用错误口径的缓存。

## 5. 验证

| 验证项 | 方式 | 结果 |
| --- | --- | --- |
| 换算方向 | 合成 1:2 拆股样本 | 拆股前 5e7、当日 1e8；市值恒等 |
| 多次送转连乘 | 1.5 后 2.0 | 事件前为最新的 `1/3` |
| 早于首个事件 | 窗口起点早于该标的首次除权 | 用全量累积因子 |
| 滚动/行序/边界 | 空输入、缺股本列、异常因子（≤0） | 原样透传，不打乱行序 |
| 矩阵逐日生效 | 真实数据 2015–2021 | 股本随日期变化；无因子时保持常数 |
| 端到端 | `POST /api/backtest/strategy/run`（2020–2021） | 20s 完成，无异常 |
| 业务影响 | 修复前后 `[20,30]亿` 交集 | 重叠率 0.697 |

测试文件：`backend/tests/test_share_capital_pit.py`、`backend/tests/test_matrix_pit_shares.py`

## 6. 已知边界与剩余风险

- **换算精度取决于 `ex_factor` 的语义**。纯现金分红不改变股本，但同样产生 `ex_factor`，
  因此本换算是"与复权价口径一致"的股本，而非会计意义上的总股本。方向上偏保守，
  但存在系统性残差。
- **彻底解决需要真实 PIT 股本表**。本地 `data/financials/shares` 为空，
  且 `share_capital.py` 的历史股本表只有 `float_shares`、没有 `total_shares`。
  接入包含 `total_shares` 的财务股本表后，应以真实值取代本推导值。
- **精度**：股本以 `float32` 存于矩阵（与其余矩阵字段一致），约 7 位有效数字，
  对 10 位数股本有 ~1e-3 相对量化误差；不影响分档判断，但对极高精度需求需注意。
- **选股页仅覆盖时间序列路径**。`_load_enriched_history` 的两条 join 已接入；
  `_compute_enriched_full` 的 join 只作用于**单一目标日**，当日快照即正确值，故不改。
- **仍有未修路径**（本次有意限定范围）：`strategy/engine.py:1571/1575` 的
  `basic_filter.market_cap_*` 门槛、`strategy/monitor.py:1484` 的监控市值条件
  仍使用快照股本。实时监控用当日快照是正确的，但**历史回看/回放会偏小**。
  如需一并修正，应在这些读取点复用 `apply_historical_share_counts`。
- **未复跑历史回测**。候选池变化约 25–30%（长窗口实测），此前基于旧口径得出的回测结论应重跑。
- **此修复改变了所有回测的输入数据**。由于矩阵缓存版本已 bump，
  升级后首次回测结果与升级前不可直接比较。

## 7. 附带修复：孤儿发布标记导致应用无法启动（与本缺陷无关）

同一批改动里还修了一个**启动故障**，与 PIT 股本无因果关系，单独记录以免混淆。

**现象**：后端启动直接失败，栈顶为

```
File "app/enriched_generation.py", line 137, in _process_is_alive
    os.kill(pid, 0)
SystemError: <class 'OSError'> returned a result with an exception set
```

**触发条件**：`data/.matrix_generation_stock.json` 残留 `state=publishing` 标记，
其 `owner_pid` 指向一个**已退出且 pid 已被复用掉的**进程（真实案例：pid 11520）。
启动时 `get_matrix_data_generation` 会探测属主存活，以决定是否恢复该孤儿标记。

**根因**：Windows 上 Python 的 `os.kill(pid, 0)` 对"已不存在的 pid"可能抛
`SystemError`，而真正的 `OSError` 挂在它的 `__context__` 上，且携带
`winerror=87`（ERROR_INVALID_PARAMETER）。原实现只写了 `except OSError`，
**该分支根本不会执行**，于是未捕获异常冒泡到 lifespan，应用启动中止。

本机实测三种返回形态，说明不能只判断一种：

```
pid 已退出(11520)   -> SystemError，__context__ = OSError(winerror=87)
pid 不存在(999999999) -> OSError(winerror=11)     ← 与原代码假设的 87 不同
pid = 自身           -> 正常返回
```

**修复**：新增 `_is_dead_pid_error()`，把 `SystemError.__context__` 一并解包，
并把 `winerror ∈ {87, 11}` 都判为"pid 已死"；其余 `OSError`（如权限不足）仍按存活处理，
保持原有 fail-closed 语义不变。

**影响与验证**：

- 修复后真实孤儿标记被自动恢复，后端正常启动（日志 `Application startup complete`）
- 原先因此失败、被误判为"环境限制"的测试
  `test_data_clear_generation.py::test_clear_data_recovers_stale_publishing_marker_from_dead_process`
  **现已通过** —— 说明它本来就是真 bug，不是环境问题
- 新增两条回归测试：`test_process_is_alive_treats_wrapped_oserror_as_dead`（SystemError 包装）、
  `test_process_is_alive_keeps_true_for_unrelated_errors`（无关 OSError 仍判存活）

**运维提示**：遇到该启动失败时，**不要**手工删除
`data/.matrix_generation_stock.json`。该标记用于防止读取"发布到一半"的 enriched 数据；
修复后应用能自行判定孤儿并恢复，手工删除会绕过这层保护。

## 8. 合并后状态（2026-09-15）

合并后 `share_capital.py` 的公开接口为：

| 函数 | 作用 | 数据前提 |
| --- | --- | --- |
| `market_cap_expr(df, shares_col)` | 点时市值 = **`raw_close`** × 当日股本 | 需 `raw_close` 列 |
| `derive_snapshot_shares(rows, columns)` | 快照股本 × `close/raw_close` → **推定当日股本** | 需 `close` + `raw_close` |
| `load_share_history(data_dir)` | 读取 `data/financials/shares/part.parquet` | 该表存在 |
| `apply_historical_shares(..., derive_snapshot_fallback=)` | polars 面板按公告日解析历史股本；开关为真时先铺推定基底 | — |
| `build_share_matrices(...)` / `attach_matrix_historical_shares(...)` | 矩阵字段提升为逐日 `(time, asset)` | — |

### 8.1 当日股本从哪来（两段式）

| 数据条件 | 当日股本来源 | 精度 |
| --- | --- | --- |
| 有 `financials/shares` 公告股本表 | 按公告日 asof 取**真实**股本 | 精确 |
| 没有（当前状态） | `快照股本 × close/raw_close` 推定 | 最优估计 |

推定成立的理由：平台前复权口径是 `close = raw_close × ratio`（`ratio = cum(≤t)/total`），
而一次送转把价格缩小 `1/k` 的同时把股本放大 `k` 倍，所以
`当日股本 = 快照股本 × ratio = 快照股本 × close/raw_close`。代入后
`raw_close × 当日股本` 恰好化简为 `close × 快照股本`，**除权日市值连续**。

⚠️ 因此 `close × 快照股本`（上游原口径）在纯送转历史下本来就是对的，
而 §2 的"历史市值被系统性算小"结论方向是反的：真正的偏差是
`raw_close × 快照股本` 会**高估**。§4 那套 `close × 推定股本` 则把 ratio 用了两次、**偏小**。

### 8.2 覆盖面

所有市值落点统一走同一口径：`api/screener.py`、`strategy/engine.py`、`strategy/monitor.py`
（经 `market_cap_expr`）、`backtest/matrix.py` 的 basic_filter、以及
`data/strategies/custom/custom_small_cap_rotation{,_v2}.py`（改为 `raw_close × total_shares`，
并把 `raw_close` 写入 `required_fields()` —— 漏改会双重折算、市值偏小）。

### 8.3 换手率与历史数据

换手率 = `volume / 流通股本`，历史上同样必须用当日流通股本，否则**系统性偏低**。
`indicators/pipeline.py` 的换手率路径已启用 `derive_snapshot_fallback=True`，
并**已全量重建历史 enriched**：

- 重建方式：`run_pipeline()`（默认全量，仅读已有 `kline_daily` + `adj_factor`，不拉数据）
- 结果：3334 个日期分区、11,996,657 行、耗时 158.7s
- 校验：`新换手率 == 旧换手率 / (close/raw_close)`，三个抽样日 99.8–99.9% 标的相对误差 < 0.1%
- 幅度：中位放大 1.30×（2016）、1.08×（2020）、1.02×（2024），个股最高 16.7×

`publication.commit()` 会换新的 enriched generation，矩阵缓存按代失效，无需手工清理。

⚠️ 换手率是策略/因子输入，**历史回测结论需要重跑**。

### 8.4 精确股本已接入（2026-09-15）

TickFlow key 无 FINANCIAL 权限（`data/capabilities.json`：`label: Pro`，probe `✗ financial(无权限)`），
所以改走**通达信**：`TdxProvider.get_financials("shares")` 由股本变迁事件还原当日股本。

- 数据源：通达信行情主站 `get_xdxr_info`，非本地文件（本地 `gbbq` 在该客户端版本里是加密的，
  `vipdoc/cw`、`T0002/cw_cache` 均为空，详见 §9）
- 落盘：`data/financials/shares/part.parquet`，**131,039 行 / 5,219 只标的**，耗时约 4 分钟
- 字段：`symbol, period_end, announce_date, total_shares, float_shares`（单位：股；通达信原生为万股，已 ×1e4）
- 口径：绝对股本事件（类别 2/3/5/6/7/8/9/10…）定锚；`category 1` 的送转/配股只给"每 10 股"比例，
  按 `(1 + 送转/10 + 配股/10)` 链式推算 —— 实测大量送转没有配对的绝对事件
  （如 600519.SH 9 个送转仅 1 个配对），不链式推算会漏掉
- 精度：最新一期与 `instruments` 比对，总股本 99.7%、流通股本 99.2% 落在 ±0.1% 内

**覆盖缺口**：344 只标的不在表内 —— **343 只北交所（`.BJ`）+ 1 只深市**。
通达信 xdxr 对北交所返回空事件。这些标的自动回退到 §8.1 的推定口径（不影响启动，也不报错）。
（2026-09-16 起本地 `gbbq` 路径已补齐这 343 只 —— 见 §10；在线口径本身未改。）

**开关**：`preferences.json` 的 `financial_data_provider` 已设为 `tdx`。
财务数据的 5 张表共用这一个开关，tdx 只提供 `shares`，其余 4 张返回空表
（它们本来就是空的，TickFlow 无权限也拿不到）。
若要改回，在设置页把「财务数据」切回 TickFlow 即可；切回后 `sync_shares` 拉不到数据会
`_write_table` 提前返回，**不会覆盖已有的 shares 表**。

**刷新**：设置页「财务数据」同步按钮，或 `POST /api/financials/sync/shares`
（`_financial_allowed` 是 custom 感知的，配合该开关后不再被 `Cap.FINANCIAL` 拦截）。
`get_financials` 忽略 `latest_only`：xdxr 一次请求即返回全部事件，只取最新会漏掉两次同步之间的中间送转。

缺失/损坏时仍会打印一次 WARNING（`_warn_share_history_unusable`），不静默退化。

### 8.5 历史数据已按精确股本重建

换手率依赖当日流通股本，因此股本精确化后**重建了两次** enriched：

| 次序 | 口径 | 中位换手率（2016-06-15）|
| --- | --- | --- |
| 原始（合并前） | 快照股本 | 0.9636 |
| 第一次重建 | 推定当日股本 | 1.4449 |
| 第二次重建 | **精确当日股本** | 见下方校验 |

第二次重建后校验（`300908.SZ` @ 2026-09-11）：

```
enriched turnover : 1.0314448430155734
volume*1e4/float  : 1.0314448430155734   (float = 122,992,520, 取自 shares 表)
```

**逐位吻合**。每次重建都会换新的 enriched generation，矩阵缓存按代自动失效。

> 2026-09-16 又重建了**第三次**：本地 gbbq 补齐北交所 343 只精确股本后，让北交所历史
> 换手率/流通市值也走精确值（此前该批标的走推定口径，抽样最大偏差 102.7%）。见 §11.7。

## 9. 本地通达信文件：早期结论与更正（2026-09-15 排查 / 2026-09-16 更正）

> ⚠️ **结论更正**：本节最初的结论是「本地拿不到逐日股本历史」，**该结论不成立**。
> `gbbq` 确实被加密，但用的是社区公开的**白盒密钥表 + 16 轮 Feistel** 方案，不是单字节
> XOR；当时只试了 zlib/gzip/bz2/lzma 与 256 个单字节 key，因此把"解不开"误判成"没有
> 这份数据"。现已实现本地读取（见 §10）。
>
> 表格里 `base.dbf`、`vipdoc\cw`、`T0002\cw_cache` 三行的判断**仍然成立**。

原始排查记录（保留）：本机装有两个通达信（`D:\TradeTool` 为活跃安装，`D:\通达信` 为 2023
旧装），逐个排查后的结论曾是不能离线取到逐日股本：

| 文件 | 内容 | 实测结果 |
| --- | --- | --- |
| `T0002\hq_cache\gbbq` | 股本变迁（带日期） | ✅ **可解密（更正）**：192,540 条事件 / 6,318 只标的 / 1990-03-01 ~ 2026-09-23 |
| `T0002\hq_cache\base.dbf` | 明文 DBF，7932 条：`ZGB` 总股本、`LTAG` 流通A股、`GXRQ`、`SSDATE`、`HY` | 可读，但与 `instruments` 重复：总股本 99.5%、流通股本 99.0% 落在 ±0.1%（**每标的仅 1 行 = 快照**） |
| `vipdoc\cw\` | gpcw 财务历史 | ❌ 两个安装都是**空目录** |
| `T0002\cw_cache\` | 财务缓存 | ❌ 两个安装都是**空目录** |

`base.dbf` 的字段（供将来参考）：`SC` 市场、`GPDM` 代码、`GXRQ` 数据日期、`ZGB` 总股本(万股)、
`LTAG` 流通A股(万股)、`SSDATE` 上市日期、`HY` 行业，另有 `ZZC/JZC/ZYSY/JLY` 等财务字段。

**更正后的结论**：`gbbq` 与主站 `get_xdxr_info` 是**同一份**股本变迁数据（记录结构、字段
槽位、单位全部一致），本地这份可离线解密 → 逐日股本不必联网获取；`base.dbf` 只有最新快照，
与 `instruments` 等价，对 PIT 目标无增量；`vipdoc/cw`、`T0002/cw_cache` 为空，仍不可用。

## 10. 本地 gbbq 读取（2026-09-16 实现）

### 10.1 位置与行为

| 项 | 说明 |
| --- | --- |
| 读取器 | `backend/app/plugins/tdx/gbbq.py`（解密 + 记录解析 + 本地市场号映射） |
| 密钥表 | `backend/app/plugins/tdx/gbbq_keys.py`（机器生成；文件头注明来源与版本相关性） |
| 接线点 | `TdxProvider.get_financials("shares", ...)`：**本地优先 + 在线补齐** |
| 定位顺序 | 环境变量 `TDX_HOME` / `TDX_DATA_DIR`（可指向安装目录、`T0002` 或 `gbbq` 文件本身；命中即用，未命中继续探测）→ 各盘根目录下常见安装目录名；多个候选取 `gbbq` 最新的那个 |
| 过期判定 | 文件里**最新事件日**早于今天 5 天即视为过期 → 整体回退在线（gbbq 每个交易日都有新公告落进来，最新事件日≈快照日；宁可慢，不用过期股本） |
| 失败路径 | 文件缺失 / 损坏 / 无记录 → fail-closed 回退在线，行为与只有在线时完全一致 |
| 北交所 | 本地文件用**市场号 2** 承载 920xxx；在线路径把 `.BJ` 映射到市场 0，取不到这批 |

"每日股本"的来源没有变：本地文件与在线 xdxr 都是**事件**（生效日 → 当日总股本/流通股本），
逐日是平台侧按生效日 asof 展开的阶梯值。

### 10.2 实测（2026-09-16，客户端当日刷新的文件）

| 验证项 | 结果 |
| --- | --- |
| 文件自洽 | `4 + 29 × 192,540 = 5,583,664` 字节（29 字节/条 + 4 字节头）|
| 解密 + 解析耗时 | **5.7 s**（纯 Python，输出事件索引；pytdx 参考实现同任务含 pandas 构造为 22.7 s，原在线逐标的同步约 4 分钟）|
| 与平台股本表 as-of 比对 | 5,219 只平台标的全部有本地阶梯，可比对 5,201 只 **0 处不一致**（±0.1%）|
| 本地领先平台表 | 18 只（本地有更新事件，平台上次在线同步尚未取到）—— 合并按 `(symbol, period_end)` 取并集，下次同步落表 |
| 额外覆盖 | 本地可推出阶梯的标的多出 423 只（其中 343 只北交所 + B 股等），平台股本表不含 |
| 北交所 | `instruments` 的 343 只 `.BJ`：343 只有阶梯、**343/343 与快照一致（±0.1%）** |
| 逐日校验 | 6 只（含 8–9 次送转的标的）× 7 个交易日：本地阶梯的当日流通股本 == 当日 `volume×1e4/turnover_rate`，相对差 0 ~ 9e-5 |
| 密钥表长度自检 | 8,352 个十六进制字符 = 1044 × uint32 = 4176 字节 |

### 10.3 边界与风险

- **新鲜度跟着客户端走**：`gbbq` 只在通达信客户端运行时刷新。实测本地可能比平台表新
  （2026-09-16：`300545.SZ` 本地已有 09-16 事件，平台表停在 06-30 后未同步），也可能旧；
  过期由 §10.1 的判定兜底回退在线。
- **精度**：股本以 IEEE float32 存于文件，10 位数股本带 ~1e-7 相对量化误差（与在线压缩浮点同量级）。
- **密钥表是客户端版本相关的常量**：客户端换表后读取器解不出合法记录，会 fail-closed 回退在线，
  不会静默产生错误股本；届时应从对应版本重新提取该表。
- **含未来已公告的除权日**（实测 17+ 条晚于当日），下游必须继续严格按生效日 asof 使用。
- **平台股本表仍是权威落盘**：本地路径只改变 `get_financials("shares")` 的取数来源，
  `data/financials/shares/part.parquet` 的 schema、写入口径与合并逻辑不变。
- **测试**：`backend/tests/test_tdx_gbbq.py`（合成文件加解密往返、类别槽位口径、市场号映射、
  离线路径不联网、在线补齐、过期与损坏回退、快照缓存失效、真实文件结构 + 与平台表 parity）；
  本机无通达信安装时真实文件用例自动跳过。

## 11. 总市值口径审计与修复（2026-09-16）

对全仓总市值落点做了一次审计（口径 = `raw_close × 当日股本`，见 §8.2），结论与修复如下。

### 11.1 审计结论：服务端口径正确

- 实测 19,925 个「标的 × 日期」样本（2016/2019/2022/2024/2026 五个日期，真值 = 不复权价 ×
  股本表 asof）：**服务端路径相对误差 0.0000%**；矩阵路径（float32 存储）最大残差 4.2e-08。
- 逐条核对：选股 polars 路径、回测矩阵路径、选股 matrix_native 路径、实时监控、`/market-snapshot`、
  分钟回放面板、非股票资产中和 —— 全部走 `market_cap_expr` + 当日股本解析（§8.2）。
- 反例量化（说明这些口径为什么必须统一）：
  - `close × 当日股本`（复权比计入两次）：中位 +2.2%、p90 +35.7%、最大 +94%（2016 年中位 22.9%）；
  - `快照股本 × raw_close`：中位 +0.4%、p90 +71%、最大 +3223%。
  - 最新交易日 `close == raw_close`（实测 5,548/5,548 行），因此"当日"用前复权价无差异。

### 11.2 已修的三处偏离

| 位置 | 原实现 | 修复 |
| --- | --- | --- |
| `frontend/.../screener/ScreenerFilter.tsx` | 前端用 `close × total_shares` 重算市值区间 | 优先用后端 `market_cap`/`float_market_cap`，否则 `raw_close × 股本` |
| `frontend/.../StockInfoBar.tsx`、`EChartsCandlestick.tsx` | 市值/换手率用"现价 × 快照股本"，历史区间（回测命中弹窗）错 | 后端 `/daily` 逐行下发 `market_cap`/`float_market_cap`（`share_capital.attach_market_cap`）、`turnover_rate`；前端优先用行内值，仅分时/实时行才回退 |
| `log_float_mv` 因子（`strategy/scoring.py`、`backtest/matrix.py`） | 用前复权 `close` 反推流通市值 | 改用 `raw_close`；注册表依赖改为 `{raw_close, volume, turnover_rate}`，因子回测面板按依赖加载该列 |

### 11.3 顺带修掉的静默路径

- 配置了 `market_cap_*`/`float_cap_*` 却拿不到股本列时，原先返回 None 被当作"未配置"直接跳过约束。
  现在三处落点（策略 basic_filter、监控 volume-delta、矩阵 basic_filter）都会打**一次性 WARNING**
  （`share_capital.warn_market_cap_unavailable`），不再静默丢弃约束。
- 缺不复权价的告警统一到 `share_capital.warn_missing_valuation_price`（一次/上下文），
  `log_float_mv` 在缺 `raw_close` 时 fail-closed（不产出因子列，不退回前复权价）。

### 11.4 验证与剩余风险

- 新增/更新测试：`tests/test_kline_daily_market_cap.py`（历史行 PIT 市值、禁止二次换算、ETF 不注入）、
  `tests/backtest/test_matrix_valuation_factors.py`（矩阵因子用 raw_close + 缺列 fail-closed）、
  `tests/test_basic_filter_market_cap.py`（两处"约束被跳过"告警）、
  `tests/test_factor_expansion.py` / `test_factor_registry.py` / `tests/backtest/test_matrix_strategy.py`
  / `tests/backtest/test_factor_batch.py`（口径与依赖回归）。
- 剩余风险：前端两处改动做了类型检查与 `pnpm build`（2026-09-16 已跑通：`tsc -b` 零错误 +
  vite 构建成功；此前失败是因为 `src/custom/astock/` 页面引用了当时不存在的 `QK.astockReports/
  astockMargin/astockTelegraph`，以及 `vitest` 未安装 —— 两者已补齐），但**仍需在页面上人工联调一次**
  （回测命中弹窗的历史市值、选股页历史日期的市值筛选）。
- `log_float_mv` 数值已变，依赖它的因子策略/挖掘结论需重跑。

### 11.5 首次全量同步暴露的两处缺陷与修复（2026-09-16 晚）

用本地 gbbq 跑通第一次全市场同步（6.8 s；5,219 → 5,562 只，北交所补 343 只）后，把落表值与
本地阶梯**逐键**比对（137,167 键）：136,818 键一致，**349 键保留了同步前的旧值**。分类后定位到
两处代码缺陷：

**① `_merge_report_history` 的"新行覆盖旧行"在同日同键时不成立**（`services/financial_sync.py`）

股本/解禁事件的 `announce_date` 就等于生效日，同一 `(symbol, period_end)` 反复同步时排序键
完全相同，胜负取决于 `group_by().agg(last())` 的偶然行序 → 旧口径的错行永远改不掉。
修复：显式加输入序号 `_merge_order` 参与排序（越后写优先级越高）+ `maintain_order=True`；
`_merge_order` 不落表（写入前从列集合里排除）。

**② 与绝对事件配对的送转被多反除一次**（`plugins/tdx/provider.py`）

"锚点之前逐项反除"没有区分两种送转：

| 形态 | 例子 | 正确做法 |
| --- | --- | --- |
| 送转与紧随其后的绝对事件配对（除权除息日 → 次日送股上市，且 后总/前总 恰好等于送转倍数） | 600519：2002-07-25 10送1 → 07-26 前 25000 / 后 27500 万股 | **不反除**：锚点 `qian` 已是送转前股本，再除一次会让"上市 → 首次变动"的基准偏小一个倍数（2.5 亿 → 2.2727 亿） |
| 历史上没有绝对事件记录的送转 | 1995 年送转 + 1998 年才有绝对事件 | 仍要反除，否则该段基准偏大 |

修复：`_is_paired_with_absolute()` —— 7 天窗口内存在绝对事件且 后/前 比值与送转倍数吻合
（2% 容差）即视为配对，跳过反除。

⚠️ **口径澄清（本文 §8.1 的除权日连续性在此同样适用）**：除权日（category 1）当天的股本按
**送转后**取值，与后续各次除权的处理一致，这样 `raw_close × 当日股本` 在除权日连续。首次比对时
一度把表里 67 个"除权日 = 送转后值"的行当成错误，实际**表是对的、阶梯错了一天**（同一个基准
缺陷的另一种表现）；修复后两者一致，这 67 个键不再变动。

### 11.6 修复后的重跑结果

| 项 | 结果 |
| --- | --- |
| 重跑同步 | 6.4 s，`{'shares': 137174}` |
| 逐键比对（落表 vs 修正后阶梯） | **137,174 键全部一致，0 处不一致** |
| 旧键丢失 | 0 |
| 相较上一次同步被改写的键 | **595 个**（例：`600295.SH` 基准行 2.58 亿 → 5.16 亿；`600602.SH` 1990-12-19 的 `float 3,360,900 > total 2,000,000` 不可能值 → 修正为 510,900） |
| 反例行（`float_shares > total_shares`） | 5 → **4**，剩余 4 行是 **通达信源数据自身矛盾**：`000850.SZ` 2001 年两条记录给的是流通 6500 万 > 总 1850 万；`601918.SH` 两条为 float32 独立存储的舍入（比值 1.0000，差 ~160 股 / 18.5 亿股） |
| 关键抽样 | 600519 基准行 = 2.5 亿（IPO 真实总股本）✓；600519 2002-07-25 = 2.75 亿 ✓；000157 除权日/上市日 = 3 亿 ✓；920002.BJ 最新 = 6,370.4 万 ✓ |

新增回归测试：`tests/test_tdx_provider.py` 4 条（配对不反除 / 未配对反除 / 两者并存只除未配对 /
窗口内倍数不符不算配对）、`tests/test_fundamental_factors.py` 1 条（同日同键以后写帧为准，且逐列
仍能由旧行补齐）。受影响模块回归 293 passed。

**仍待处理**：北交所 343 只有了精确股本，但 enriched 的 `turnover_rate` 仍是按推定股本算的
（§8.5 机制）——要让历史换手/市值用上精确值需重建 enriched（全市场约 160 s，会换 generation
并使矩阵缓存失效）。`000850.SZ` / `601918.SH` 的源数据矛盾若要纠正，应在数据边界加显式告警而非
静默改写（尚未实现）。（前半句已于同日执行，见 §11.7。）

### 11.7 enriched 全量重建（2026-09-16 晚，含北交所精确股本）

入口与 API 的 `POST /api/kline/rebuild_enriched` 相同：`run_pipeline(on_batch_done=...)`，
只读已有 `kline_daily` + `adj_factor`，**不拉任何数据**。

| 项 | 结果 |
| --- | --- |
| 耗时 / 规模 | **160.5 s**、12,002,207 行、3,335 个日期分区、5,564 只标的（enriched 体量 0.61 GB） |
| 复权因子 | 43,127 行 |
| generation | `ab697dfbf47a4766957a2122d2fd2681` → **`de12c4fd81224782891eed1438c2b1f0`**（state=ready） |
| 分区写入时间 | 2016-06-15 = 21:49:23、2026-09-15 = 21:50:45（均晚于重建开始，确认整体换新） |
| staging 残留 | 0 文件（无冗余副本） |
| schema | 仍 15 列，未变 |

**换手率校验（证明用上了精确股本）**：`turnover_rate == volume × 1e4 / 当日流通股本`（股本取
`financials/shares` asof），股票 + 北交所共 **70 个 (标的, 日期) 组合，最大相对差 0.00e+00**。

**北交所的历史偏差量化**（重建前无股本行 → 走 `快照 × close/raw_close` 推定；现用精确值）：

| 标的 | 日期 | 推定流通股本 | 精确流通股本 | 偏差 |
| --- | --- | --- | --- | --- |
| `920593.BJ` | 2024-06-17 | 115,071,809 | 56,783,052 | **+102.7%** |
| `920834.BJ` | 2024-06-17 | 59,290,402 | 32,964,004 | +79.9% |
| `920392.BJ` | 2024-06-17 | 27,112,790 | 15,132,400 | +79.2% |
| `920346.BJ` | 2022-06-15 | 39,801,835 | 24,707,473 | +61.1% |

抽样中 18 个组合偏差 >0.1%。原因是推定式只能反映"送转引起的价格比例变化"，**无法反映解禁/发行
这类不改变价格比例的流通股本变动**，因此北交所历史换手率与流通市值此前系统性偏离（近期日期
偏差已收敛到 1~6%）。

**缓存与失效链**（改动后需要知道的行为）：

- 矩阵磁盘缓存的 `manifest.json` 同时记录 `source_generation`（旧代 `ab697d…`）与每个分区的
  内容哈希；换代后 `_prune_matrix_disk_cache(current_source_generation=...)` 会淘汰旧条目 ——
  **重建后的第一次回测会重建矩阵缓存**（首次较慢，符合 §4.4/§6.3 的既有约定）。
- 进程内 enriched 缓存按代失效；本次重建时无后端进程在跑，故无需重启。
- 前端需要在页面上刷新一次以丢弃旧的 TanStack Query 缓存（矩阵/选股结果均为盘后静态数据）。



