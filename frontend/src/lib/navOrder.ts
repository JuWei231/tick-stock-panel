/**
 * 侧栏默认排序基准 — 扩展域页面迁移 (custom/) 的老用户升级兼容。
 *
 * 迁移前这 19 个路径按此顺序构成内置侧栏 (Layout 核心导航数组)。
 * 页面迁往 custom/<ns>/extension.tsx 后, 无保存排序的环境 (新装或
 * 从未调整过菜单的用户) 若按「核心在前、扩展追加」拼接, 11 个页面会
 * 整体沉底, 老用户升级即感知错位。因此默认顺序统一按本基准排序,
 * 升级前后侧栏一致; 已保存过 nav_order 的用户按保存值展示, 不受影响。
 *
 * 未知路径 (声明式分析页、二开扩展) 排在已知路径之后, 保持相对顺序。
 * 修改本清单需同步回归 navOrder.test.ts。
 */
export const DEFAULT_NAV_PATHS = [
  '/', '/watchlist', '/screener', '/factors', '/backtest',
  '/stock-analysis', '/limit-ladder', '/concept-analysis', '/industry-analysis', '/financials',
  '/monitor', '/regime', '/abnormal', '/lots', '/paper', '/signals', '/review',
  '/indices', '/data',
] as const

const INDEX = new Map<string, number>(DEFAULT_NAV_PATHS.map((p, i) => [p, i]))

/** 路径的默认序号; 未知路径返回 Infinity (排在全部已知路径之后) */
export function defaultNavIndex(path: string): number {
  return INDEX.get(path) ?? Number.POSITIVE_INFINITY
}

/**
 * 按默认基准排序的比较器。两个未知路径相减会产生 NaN 破坏排序,
 * 必须先判等再相减 (相等时依赖 sort 的稳定性保持相对顺序)。
 */
export function compareByDefaultNav(a: string, b: string): number {
  const ia = defaultNavIndex(a)
  const ib = defaultNavIndex(b)
  if (ia === ib) return 0
  return ia - ib
}
