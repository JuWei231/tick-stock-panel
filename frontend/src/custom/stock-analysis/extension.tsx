/**
 * 个股分析页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconStockFocus } from '@/components/BrandIcons'
import { StockAnalysis } from './StockAnalysis'

const extension: FrontendExtension = {
  id: 'official.stock-analysis',
  apiVersion: 1,
  routes: [
    { id: 'stock-analysis', path: '/stock-analysis', component: StockAnalysis },
  ],
  navigation: [
    { id: 'stock-analysis', routeId: 'stock-analysis', label: '个股分析', icon: IconStockFocus },
  ],
}

export default extension
