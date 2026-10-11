/**
 * 行业分析页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconIndustry } from '@/components/BrandIcons'
import { IndustryAnalysis } from './IndustryAnalysis'

const extension: FrontendExtension = {
  id: 'official.industry-analysis',
  apiVersion: 1,
  routes: [
    { id: 'industry-analysis', path: '/industry-analysis', component: IndustryAnalysis },
  ],
  navigation: [
    { id: 'industry-analysis', routeId: 'industry-analysis', label: '行业分析', icon: IconIndustry },
  ],
}

export default extension
