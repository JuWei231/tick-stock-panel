/** a-stock-data 扩展页共享 UI：加载 / 错误 / 空态与取值助手。 */
import { AlertTriangle, Loader2 } from 'lucide-react'

export function Loading({ label = '加载中…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-xs text-muted">
      <Loader2 className="h-4 w-4 animate-spin" />
      {label}
    </div>
  )
}

export function ErrorCard({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex items-center gap-2 rounded-card border border-border bg-surface/80 px-4 py-3 text-xs text-muted">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-bear/80" />
      <span className="min-w-0 flex-1">拉取失败：{message}</span>
      {onRetry && (
        <button type="button" onClick={onRetry} className="shrink-0 text-accent hover:underline">
          重试
        </button>
      )}
    </div>
  )
}

export function EmptyCard({ text }: { text: string }) {
  return (
    <div className="rounded-card border border-dashed border-border/70 bg-surface/40 px-4 py-10 text-center text-xs text-muted">
      {text}
    </div>
  )
}

export function strOf(v: unknown): string {
  return v === null || v === undefined ? '' : String(v)
}

export function numOf(v: unknown): number {
  if (typeof v === 'number') return Number.isFinite(v) ? v : 0
  const n = Number.parseFloat(String(v ?? ''))
  return Number.isFinite(n) ? n : 0
}

export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export function fmtEps(v: unknown): string {
  const n = numOf(v)
  return n ? n.toFixed(2) : '—'
}

export function fmtYi(v: unknown): string {
  return `${(numOf(v) / 1e8).toFixed(2)} 亿`
}

export function fmtWan(v: unknown): string {
  return `${(numOf(v) / 1e4).toFixed(0)} 万`
}

export function excerpt(s: string, max = 240): string {
  const t = s.trim()
  return t.length > max ? `${t.slice(0, max)}…` : t
}
