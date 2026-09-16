/**
 * a-stock-data 集成扩展：个股研究 + 市场快讯。
 *
 * 后端：backend/app/astock + /api/astock/*（研报 / 两融 / 财联社电报），
 * 详见仓库 docs/astock-integration.md。
 *
 * 本文件由 src/extensions/bootstrap.ts 的 import.meta.glob 自动发现，无需其它注册。
 */
import { BookOpen, Radio } from 'lucide-react'

import type { FrontendExtension } from '@/extensions/types'

import ResearchPage from './pages/ResearchPage'
import TelegraphPage from './pages/TelegraphPage'

const extension: FrontendExtension = {
  id: 'astock',
  apiVersion: 1,
  routes: [
    { id: 'astock-research', path: '/astock/research', component: ResearchPage },
    { id: 'astock-telegraph', path: '/astock/telegraph', component: TelegraphPage },
  ],
  navigation: [
    { id: 'astock-research', routeId: 'astock-research', label: '个股研究', icon: BookOpen, order: 200 },
    { id: 'astock-telegraph', routeId: 'astock-telegraph', label: '市场快讯', icon: Radio, order: 210 },
  ],
}

export default extension
