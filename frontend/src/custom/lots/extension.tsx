/**
 * 持仓提醒页官方扩展 — 扩展域消费页 (docs/open-platform-plan.md §2)。
 * 路由与侧栏入口由本清单注册, 核心侧 router/Layout 不持有本页面;
 * 对核心能力均为只读 import。删除本目录即整体卸载。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconLots } from '@/components/BrandIcons'
import { Lots } from './Lots'

const extension: FrontendExtension = {
  id: 'official.lots',
  apiVersion: 1,
  routes: [
    { id: 'lots', path: '/lots', component: Lots },
  ],
  navigation: [
    { id: 'lots', routeId: 'lots', label: '持仓提醒', icon: IconLots },
  ],
}

export default extension
