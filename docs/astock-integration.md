# a-stock-data 集成（后端）

本项目集成了开源项目 [a-stock-data](https://github.com/simonlin1212/a-stock-data)
（Apache-2.0）的零鉴权数据端点，作为**扩展数据能力**补足 TickFlow / fuyao / stock-sdk
覆盖不到的高价值数据。取数代码在 `backend/app/astock/`（含出处与偏差记录 NOTICE.md），
TSP 侧接线为 L2 自定义扩展，不修改核心源码。

## 已接入数据集（P0）

| 端点 | 数据 | 源 | 说明 |
| --- | --- | --- | --- |
| `GET /api/astock/research/reports/{symbol}` | 个股研报 + 评级 + 三年 EPS 预测 | 东财 reportapi | 支持 `600519` / `SH600519` / `600519.SH`；格式错/指数码 → 400 |
| `GET /api/astock/margin/{symbol}` | 融资融券明细（日级） | 东财 datacenter | 金额单位元；`?limit=` 控制条数 |
| `GET /api/astock/news/telegraph` | 财联社电报（全市场快讯） | cls.cn v1 + 本地签名 | 45s 短 TTL 内存缓存；`?limit=` 控制条数 |
| `GET /api/astock/status` | 集成状态 | — | 缓存目录 + 数据集清单 |

### 响应信封

```jsonc
{ "state": "ok",              // ok | error（上游失败不伪造数据）
  "dataset": "reports",
  "symbol": "600519.SH",
  "code": "600519",
  "date": "2026-09-08",       // 缓存/数据归属日（北京时间）
  "fetched_at": "...",
  "count": 100,
  "items": [ /* 数据源原始字段映射，见 app/astock 各端点注释 */ ] }
```

### 缓存

- 研报 / 两融：按日落盘 `data/astock/{dataset}/{symbol}/date=YYYY-MM-DD.json`
  （原子写），当日已取直接读缓存，不重复打上游。
- 财联社电报：进程内 45s TTL。

### 限流与风控

东财系接口共用风控面（封 IP 成片失联），本集成全部请求经 `app.astock.common.em_get()`
进程级串行节流（默认 ≥1s + 抖动）。批量使用请调大最小间隔。

## 新增依赖

无。`app/astock` 只依赖本仓库已内置的 `httpx`（+ 标准库）；取数解析不依赖
pandas/requests/baostock。

## 测试

```bash
cd backend
uv run --extra dev python -m pytest tests/test_astock.py -q    # 契约测试，零真实网络
uv run --extra dev python -m ruff check app/astock app/custom/astock_integration.py \
    app/services/astock_service.py tests/test_astock.py
```

真实网络冒烟（可选）：起服务后 `GET /api/astock/margin/600519` 等。

## 升级与维护

- 上游 a-stock-data 更新后，对照其 `SKILL.md`「端点路由速查」逐函数复核
  `backend/app/astock/`（记录见 `NOTICE.md`）。
- 后续可选扩展（研报 PDF 下载、股东户数、互动易、期权、宏观社融/PMI 等）按同一
  模式接入：`app/astock` 加取数函数 → `astock_service` 加缓存服务 → 路由 → 契约测试。

## 前端页面（P1）

前端以 L2 扩展接入（`frontend/src/custom/astock/`），无需改动核心页面代码：

- **个股研究** `/astock/research`（导航「个股研究」）：输入代码 → 「研报」（评级 / EPS 预测）与
  「融资融券」两个 tab，数据按日信封缓存。
- **市场快讯** `/astock/telegraph`（导航「市场快讯」）：财联社电报滚动列表，staleTime 45s /
  60s 自动刷新 + 手动刷新。

接线点：

- `frontend/src/custom/astock/extension.tsx` — 路由与导航注册（由 `src/extensions/bootstrap.ts`
  的 `import.meta.glob` 自动发现，零配置）。
- `frontend/src/lib/api.ts` — 新增 `api.astockReports / astockMargin / astockTelegraph`
  （统一走 `request()` 封装：30s 超时、错误 toast、401 全局处理）。
- `frontend/src/lib/queryKeys.ts` — 新增 `QK.astock*` 工厂（不进 SSE 失效前缀）。
- `frontend/src/custom/astock/pages/*` — 页面实现（研报表格 / 两融表格 / 电报列表），
  错误信封 `state=error` 显示重试卡片。

验证：`pnpm build`（tsc -b && vite build）通过。
