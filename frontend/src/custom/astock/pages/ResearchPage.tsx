/** 个股研究：研报（评级 + EPS 预测）与融资融券明细。数据源 a-stock-data 集成。 */
import { useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'

import { PageHeader } from '@/components/PageHeader'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

import { EmptyCard, ErrorCard, Loading, errText, excerpt, fmtEps, fmtWan, fmtYi, strOf } from '../ui'

const TABS = [
  { key: 'reports', label: '研报' },
  { key: 'margin', label: '融资融券' },
] as const
type TabKey = (typeof TABS)[number]['key']

// 两融明细条数: 查询键与请求必须用同一个常量, 否则改了一处会让缓存与请求错位。
const MARGIN_LIMIT = 100

const MARGIN_COLS = [
  { key: 'rzye', label: '融资余额' },
  { key: 'rzmre', label: '融资买入' },
  { key: 'rzche', label: '融资偿还' },
  { key: 'rqye', label: '融券余额' },
  { key: 'rqmcl', label: '融券卖出量' },
  { key: 'rqchl', label: '融券偿还量' },
  { key: 'rzrqye', label: '两融合计' },
] as const

function ResearchQuery({ symbol }: { symbol: string }) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: QK.astockReports(symbol),
    queryFn: () => api.astockReports(symbol),
    staleTime: 60_000,
  })
  if (isLoading) return <Loading />
  if (isError) return <ErrorCard message={errText(error)} onRetry={() => void refetch()} />
  if (data?.state === 'error') {
    return <ErrorCard message={data.message ?? '上游暂不可用'} onRetry={() => void refetch()} />
  }
  const items = data?.items ?? []
  if (items.length === 0) return <EmptyCard text="该标的本数据源暂无研报记录（返回空不代表无研报，请核验代码后重试）。" />
  return (
    <div className="rounded-card border border-border bg-surface/60">
      <div className="overflow-auto">
        <table className="min-w-full text-left text-xs">
          <thead className="bg-elevated/60 text-[11px] text-muted">
            <tr>
              <th className="px-4 py-2 font-medium">日期</th>
              <th className="px-4 py-2 font-medium">评级</th>
              <th className="px-4 py-2 font-medium">机构</th>
              <th className="px-4 py-2 font-medium">今年EPS预测</th>
              <th className="px-4 py-2 font-medium">标题</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/70">
            {items.map((r, i) => (
              <tr key={i} className="align-top hover:bg-elevated/30">
                <td className="whitespace-nowrap px-4 py-2 font-mono text-muted">{strOf(r.publishDate).slice(0, 10)}</td>
                <td className="px-4 py-2">{strOf(r.emRatingName) || '—'}</td>
                <td className="whitespace-nowrap px-4 py-2">{strOf(r.orgSName) || '—'}</td>
                <td className="px-4 py-2 font-mono tabular-nums">{fmtEps(r.predictThisYearEps)}</td>
                <td className="px-4 py-2 text-secondary">{excerpt(strOf(r.title), 120)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <EnvelopeMeta data={data} />
    </div>
  )
}

function MarginQuery({ symbol }: { symbol: string }) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: QK.astockMargin(symbol, MARGIN_LIMIT),
    queryFn: () => api.astockMargin(symbol, MARGIN_LIMIT),
    staleTime: 60_000,
  })
  if (isLoading) return <Loading />
  if (isError) return <ErrorCard message={errText(error)} onRetry={() => void refetch()} />
  if (data?.state === 'error') {
    return <ErrorCard message={data.message ?? '上游暂不可用'} onRetry={() => void refetch()} />
  }
  const items = data?.items ?? []
  if (items.length === 0) return <EmptyCard text="该标的暂无融资融券记录（可能未在两融标的范围）。" />
  return (
    <div className="rounded-card border border-border bg-surface/60">
      <div className="overflow-auto">
        <table className="min-w-full text-left text-xs">
          <thead className="bg-elevated/60 text-[11px] text-muted">
            <tr>
              <th className="px-4 py-2 font-medium">日期</th>
              {MARGIN_COLS.map(c => (
                <th key={c.key} className="px-4 py-2 font-medium">{c.label}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border/70">
            {items.map((r, i) => (
              <tr key={i} className="hover:bg-elevated/30">
                <td className="whitespace-nowrap px-4 py-2 font-mono text-muted">{strOf(r.date)}</td>
                {MARGIN_COLS.map(c => (
                  <td key={c.key} className="whitespace-nowrap px-4 py-2 font-mono tabular-nums text-secondary">
                    {c.key === 'rqmcl' || c.key === 'rqchl' ? fmtWan(r[c.key]) : fmtYi(r[c.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <EnvelopeMeta data={data} note="金额单位：元（东财 datacenter 口径）" />
    </div>
  )
}

function EnvelopeMeta({ data, note }: { data?: { date?: string; fetched_at?: string; count: number } | null; note?: string }) {
  const parts: string[] = []
  if (data?.date) parts.push(`数据日期 ${data.date}`)
  if (data?.count != null) parts.push(`共 ${data.count} 条`)
  if (data?.fetched_at) parts.push(`拉取 ${strOf(data.fetched_at).replace('T', ' ').slice(0, 19)}`)
  if (note) parts.push(note)
  if (parts.length === 0) return null
  return <div className="border-t border-border/60 px-4 py-1.5 text-[11px] text-muted">{parts.join(' · ')}</div>
}

export default function ResearchPage() {
  const [input, setInput] = useState('')
  const [symbol, setSymbol] = useState('')
  const [tab, setTab] = useState<TabKey>('reports')

  const submit = () => {
    const s = input.trim()
    if (s) setSymbol(s)
  }
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') submit()
  }

  return (
    <div className="min-h-full">
      <PageHeader
        title="个股研究"
        subtitle="研报评级 / 三年 EPS 预测 · 融资融券明细（a-stock-data，按日缓存）"
      />
      <div className="px-4 py-4 sm:px-6">
        <div className="mx-auto max-w-[1280px] space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="输入代码：600519 / SH600519 / 600519.SH / sz000001"
              className="h-8 w-72 rounded-btn border border-border bg-base px-3 text-sm outline-none focus:border-accent sm:w-96"
            />
            <button
              type="button"
              onClick={submit}
              className="inline-flex h-8 items-center rounded-btn bg-accent/90 px-3 text-xs text-foreground hover:bg-accent"
            >
              查询
            </button>
            {symbol && (
              <button
                type="button"
                onClick={() => { setSymbol(''); setInput('') }}
                className="text-xs text-muted hover:text-foreground"
              >
                清除
              </button>
            )}
          </div>

          {symbol ? (
            <>
              <div className="inline-flex items-center gap-0.5 rounded-full border border-border/50 bg-base/70 p-0.5">
                {TABS.map(t => (
                  <button
                    key={t.key}
                    type="button"
                    onClick={() => setTab(t.key)}
                    className={`rounded-full px-3.5 py-1.5 text-xs transition-all ${
                      tab === t.key ? 'bg-accent/15 font-medium text-accent shadow-sm' : 'text-secondary hover:text-foreground'
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              {tab === 'reports' ? <ResearchQuery symbol={symbol} /> : <MarginQuery symbol={symbol} />}
            </>
          ) : (
            <EmptyCard text="输入股票代码后，查看研报（评级与 EPS 预测）与融资融券明细。" />
          )}
        </div>
      </div>
    </div>
  )
}
