/**
 * 异动监控页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconAlert } from '@/components/BrandIcons'
import { AbnormalMoves } from './AbnormalMoves'

const extension: FrontendExtension = {
  id: 'official.abnormal',
  apiVersion: 1,
  routes: [
    { id: 'abnormal', path: '/abnormal', component: AbnormalMoves },
  ],
  navigation: [
    { id: 'abnormal', routeId: 'abnormal', label: '异动监控', icon: IconAlert },
  ],
}

export default extension
