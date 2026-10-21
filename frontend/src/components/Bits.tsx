/** Small shared presentational pieces. */

import type { ReactNode } from 'react'
import { ORDER_STATUS_LABELS } from '../api'

export function Stars({ rating, reviews }: { rating: number; reviews?: number }) {
  const rounded = Math.round(rating * 2) / 2
  return (
    <div className="flex items-center gap-1.5">
      <div className="flex" aria-hidden="true">
        {[1, 2, 3, 4, 5].map((index) => {
          const fill = rounded >= index ? 1 : rounded >= index - 0.5 ? 0.5 : 0
          return (
            <svg key={index} viewBox="0 0 20 20" className="h-3.5 w-3.5">
              <defs>
                <linearGradient id={`star-${index}-${fill}`}>
                  <stop offset={`${fill * 100}%`} stopColor="#fbbf24" />
                  <stop offset={`${fill * 100}%`} stopColor="rgb(51 58 92)" />
                </linearGradient>
              </defs>
              <path
                fill={`url(#star-${index}-${fill})`}
                d="M10 1.5l2.6 5.3 5.9.85-4.25 4.15 1 5.85L10 14.9l-5.25 2.75 1-5.85L1.5 7.65l5.9-.85L10 1.5z"
              />
            </svg>
          )
        })}
      </div>
      <span className="text-xs text-slate-400">
        {rating.toFixed(1)}
        {reviews !== undefined && (
          <span className="sr-only"> out of 5 from {reviews.toLocaleString()} reviews</span>
        )}
      </span>
      {reviews !== undefined && (
        <span aria-hidden="true" className="text-xs text-slate-500">
          ({reviews.toLocaleString()})
        </span>
      )}
    </div>
  )
}

export function StockBadge({ status, available }: { status: string | null; available?: number | null }) {
  if (!status) return null
  const config: Record<string, { label: string; className: string }> = {
    in_stock: { label: 'In stock', className: 'bg-emerald-500/10 text-emerald-300 ring-1 ring-emerald-500/20' },
    low_stock: {
      label: available ? `Only ${available} left` : 'Low stock',
      className: 'bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/20',
    },
    out_of_stock: { label: 'Out of stock', className: 'bg-rose-500/10 text-rose-300 ring-1 ring-rose-500/20' },
  }
  const { label, className } = config[status] ?? config.in_stock
  return <span className={`pill ${className}`}>{label}</span>
}

const STATUS_STYLES: Record<string, string> = {
  pending_payment: 'bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/20',
  confirmed: 'bg-volt-500/10 text-volt-300 ring-1 ring-volt-500/20',
  packed: 'bg-spark-500/10 text-spark-400 ring-1 ring-spark-500/20',
  shipped: 'bg-sky-500/10 text-sky-300 ring-1 ring-sky-500/20',
  delivered: 'bg-emerald-500/10 text-emerald-300 ring-1 ring-emerald-500/20',
  cancelled: 'bg-slate-500/10 text-slate-400 ring-1 ring-slate-500/20',
  payment_failed: 'bg-rose-500/10 text-rose-300 ring-1 ring-rose-500/20',
}

export function OrderStatusBadge({ status }: { status: string }) {
  return (
    <span className={`pill ${STATUS_STYLES[status] ?? STATUS_STYLES.cancelled}`}>
      {ORDER_STATUS_LABELS[status] ?? status}
    </span>
  )
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />
}

export function ErrorBanner({
  message,
  traceId,
  onRetry,
}: {
  message: string
  traceId?: string | null
  onRetry?: () => void
}) {
  return (
    <div role="alert" className="card border-rose-500/20 bg-rose-500/[0.06] p-4">
      <div className="flex items-start gap-3">
        <svg viewBox="0 0 20 20" className="mt-0.5 h-5 w-5 shrink-0 fill-rose-400">
          <path d="M10 1.8l8.5 15.4H1.5L10 1.8zm0 5.2a.9.9 0 00-.9.9v3.6a.9.9 0 001.8 0V7.9a.9.9 0 00-.9-.9zm0 7a1 1 0 100 2 1 1 0 000-2z" />
        </svg>
        <div className="min-w-0 flex-1">
          <p className="text-sm text-rose-100">{message}</p>
          {/* Surfacing the trace id turns a dead end into a starting point:
              paste it into X-Ray and the failing span is right there. */}
          {traceId && (
            <p className="mt-1.5 font-mono text-[11px] text-rose-300/70">trace {traceId}</p>
          )}
        </div>
        {onRetry && (
          <button type="button" onClick={onRetry} className="btn-ghost shrink-0 !py-1.5 !text-xs">
            Retry
          </button>
        )}
      </div>
    </div>
  )
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string
  body: string
  action?: ReactNode
}) {
  return (
    <div className="card flex flex-col items-center gap-3 px-6 py-16 text-center">
      <div className="rounded-2xl bg-volt-500/10 p-3.5 ring-1 ring-volt-500/20">
        <svg viewBox="0 0 24 24" className="h-7 w-7 stroke-volt-400" fill="none" strokeWidth="1.6">
          <path d="M3 6h18M6 6l1.2 13.2A2 2 0 009.2 21h5.6a2 2 0 002-1.8L18 6M9 6V4a2 2 0 012-2h2a2 2 0 012 2v2" />
        </svg>
      </div>
      <h3 className="text-base font-semibold text-slate-100">{title}</h3>
      <p className="max-w-sm text-sm text-slate-400">{body}</p>
      {action}
    </div>
  )
}

export function SectionHeading({
  eyebrow,
  title,
  action,
}: {
  eyebrow?: string
  title: string
  action?: ReactNode
}) {
  return (
    <div className="mb-5 flex items-end justify-between gap-4">
      <div>
        {eyebrow && (
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-volt-400">
            {eyebrow}
          </p>
        )}
        <h2 className="text-xl font-semibold tracking-tight text-slate-50 sm:text-2xl">{title}</h2>
      </div>
      {action}
    </div>
  )
}
