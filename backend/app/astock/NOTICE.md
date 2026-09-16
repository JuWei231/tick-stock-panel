# a-stock-data 集成核心（app/astock）出处与偏差记录

本目录 `app/astock/*.py` 由开源项目 **[a-stock-data](https://github.com/simonlin1212/a-stock-data)**
（Apache License 2.0，作者 Simon 林）的 `SKILL.md` 代码移植而来，仅用于数据获取工具，
不构成任何投资建议。

- 上游版本：a-stock-data v3.8.0（SKILL.md）
- 移植日期：2026-09-08
- 许可：Apache-2.0（完整许可证文本见上游仓库 `LICENSE`；本项目根目录 LICENSE 为 MIT，
  两许可证并存，本目录代码按 Apache-2.0 条款使用）

## 与本项目既有实现的关系

`app/astock` 是本项目的**数据层移植核心**（纯 httpx + 标准库，零新增依赖），
不含 TSP 业务接线。接线位于：

- `backend/app/services/astock_service.py` — 取数 + 按日 JSON 缓存 / 电报短 TTL
- `backend/app/custom/astock_integration.py` — `/api/astock/*` 路由（L2 扩展自动挂载）
- `backend/tests/test_astock.py` — 契约测试（零真实网络）

## 与 SKILL.md 的有意偏差（移植时记录在案）

1. requests → httpx（对齐本仓库已内置的 httpx>=0.27，零新增依赖）；
   `Session+Retry` 改为 `httpx.Client` + `em_get()` 内显式指数退避重试
   （429/5xx/连接错误重试，403 不重试）。
2. 财联社电报 `ctime` 显式按 Asia/Shanghai 转北京墙钟（naive）；
   SKILL.md 原实现用本机时区，在非中国时区服务器会错开数小时。
3. 两融 `margin_trading` 入口统一过 `norm_ticker()`（SKILL.md 要求调用方先传纯 6 位）。
4. 其余语义原样保留：分页终止、空结果返 `[]`、北交所老号段（43/83/87）抛错、
   金额单位、字段命名、东财统一走 `em_get()` 进程级串行节流。

## 升级同步

上游 SKILL.md 更新后，对照其「端点路由速查」总表逐函数复核本目录对应实现。
