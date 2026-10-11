/**
 * 监控中心页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconMonitor } from '@/components/BrandIcons'
import { Monitor } from './Monitor'

const extension: FrontendExtension = {
  id: 'official.monitor',
  apiVersion: 1,
  routes: [
    { id: 'monitor', path: '/monitor', component: Monitor },
  ],
  navigation: [
    { id: 'monitor', routeId: 'monitor', label: '监控中心', icon: IconMonitor },
  ],
}

export default extension
