/** 市场快讯：财联社电报滚动列表（60s 自动刷新）。数据源 a-stock-data 集成。 */
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'

import { PageHeader } from '@/components/PageHeader'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

import { EmptyCard, ErrorCard, Loading, errText, excerpt, strOf } from '../ui'

const LIMIT = 50

function TelegraphRow({ item }: { item: Record<string, unknown> }) {
  const title = strOf(item.title)
  const content = strOf(item.content)
  return (
    <div className="rounded-card border border-border bg-surface/60 px-4 py-2.5">
      <div className="flex items-baseline gap-3">
        <span className="shrink-0 font-mono text-[11px] text-muted">{strOf(item.time)}</span>
        <span className="min-w-0 text-xs font-medium text-foreground">{excerpt(title, 160) || '（无标题）'}</span>
      </div>
      {content && content !== title && (
        <p className="mt-1 pl-[92px] text-xs leading-relaxed text-secondary">{excerpt(content)}</p>
      )}
    </div>
  )
}

export default function TelegraphPage() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: QK.astockTelegraph(LIMIT),
    queryFn: () => api.astockTelegraph(LIMIT),
    refetchInterval: 60_000,
    staleTime: 45_000,
  })

  const items = data?.items ?? []

  return (
    <div className="min-h-full">
      <PageHeader
        title="市场快讯"
        subtitle="财联社电报 · 全市场快讯"
        titleExtra={
          data?.date ? (
            <span className="rounded-full bg-elevated px-2 py-0.5 text-[11px] text-muted">{data.date}</span>
          ) : undefined
        }
        right={
          <button
            type="button"
            onClick={() => void refetch()}
            className="inline-flex items-center gap-1.5 rounded-btn border border-border/70 bg-base/70 px-3 py-1.5 text-xs text-secondary hover:bg-elevated hover:text-foreground"
          >
            <RefreshCw className={isFetching ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
            刷新
          </button>
        }
      />
      <div className="px-4 py-4 sm:px-6">
        <div className="mx-auto max-w-[1280px] space-y-3">
          {isLoading ? (
            <Loading label="电报加载中…" />
          ) : isError ? (
            <ErrorCard message={errText(error)} onRetry={() => void refetch()} />
          ) : data?.state === 'error' ? (
            <ErrorCard message={data.message ?? '上游暂不可用'} onRetry={() => void refetch()} />
          ) : items.length === 0 ? (
            <EmptyCard text="当前暂无电报内容。" />
          ) : (
            <div className="space-y-1.5">
              {items.map((it, i) => (
                <TelegraphRow key={i} item={it} />
              ))}
            </div>
          )}
          {data && (
            <div className="text-[11px] text-muted">
              共 {data.count} 条 · 最近拉取{' '}
              {strOf(data.fetched_at).replace('T', ' ').slice(0, 19) || '—'} · 页面每 60s 自动刷新
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
