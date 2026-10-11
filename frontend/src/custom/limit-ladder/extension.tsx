/**
 * 连板梯队页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconLadder } from '@/components/BrandIcons'
import { LimitUpLadder } from './LimitUpLadder'

const extension: FrontendExtension = {
  id: 'official.limit-ladder',
  apiVersion: 1,
  routes: [
    { id: 'limit-ladder', path: '/limit-ladder', component: LimitUpLadder },
  ],
  navigation: [
    { id: 'limit-ladder', routeId: 'limit-ladder', label: '连板梯队', icon: IconLadder },
  ],
}

export default extension
