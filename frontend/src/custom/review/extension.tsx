/**
 * 大盘复盘页官方扩展 — 扩展域页面向 custom/ 目录迁移的首个试点。
 *
 * 从 frontend/src/pages/Review.tsx 平移而来: 路由 /review 与侧栏「复盘」入口
 * 均由本清单注册, router.tsx / Layout.tsx 不再持有该页面。复用的核心能力
 * (api / QK / 组件 / 复盘状态库) 均为只读 import, 见 ReviewPage.tsx。
 */
import type { FrontendExtension } from '@/extensions/types'
import { IconReview } from '@/components/BrandIcons'
import { ReviewPage } from './ReviewPage'

const extension: FrontendExtension = {
  id: 'official.review',
  apiVersion: 1,
  routes: [
    { id: 'review', path: '/review', component: ReviewPage },
  ],
  navigation: [
    { id: 'review', routeId: 'review', label: '复盘', icon: IconReview },
  ],
}

export default extension
