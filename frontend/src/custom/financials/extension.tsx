/**
 * 财务分析页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconFinancials } from '@/components/BrandIcons'
import { Financials } from './Financials'

const extension: FrontendExtension = {
  id: 'official.financials',
  apiVersion: 1,
  routes: [
    { id: 'financials', path: '/financials', component: Financials },
  ],
  navigation: [
    { id: 'financials', routeId: 'financials', label: '财务分析', icon: IconFinancials },
  ],
}

export default extension
