/**
 * 概念分析页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconConcept } from '@/components/BrandIcons'
import { ConceptAnalysis } from './ConceptAnalysis'

const extension: FrontendExtension = {
  id: 'official.concept-analysis',
  apiVersion: 1,
  routes: [
    { id: 'concept-analysis', path: '/concept-analysis', component: ConceptAnalysis },
  ],
  navigation: [
    { id: 'concept-analysis', routeId: 'concept-analysis', label: '概念分析', icon: IconConcept },
  ],
}

export default extension
