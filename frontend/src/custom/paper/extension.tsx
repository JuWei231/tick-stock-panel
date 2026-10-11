/**
 * 模拟盘页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconPaper } from '@/components/BrandIcons'
import { Paper } from './Paper'

const extension: FrontendExtension = {
  id: 'official.paper',
  apiVersion: 1,
  routes: [
    { id: 'paper', path: '/paper', component: Paper },
  ],
  navigation: [
    { id: 'paper', routeId: 'paper', label: '模拟盘', icon: IconPaper },
  ],
}

export default extension
