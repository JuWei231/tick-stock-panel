/**
 * 信号库页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconSignals } from '@/components/BrandIcons'
import { Signals } from './Signals'

const extension: FrontendExtension = {
  id: 'official.signals',
  apiVersion: 1,
  routes: [
    { id: 'signals', path: '/signals', component: Signals },
  ],
  navigation: [
    { id: 'signals', routeId: 'signals', label: '信号库', icon: IconSignals },
  ],
}

export default extension
