import { useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import {
  formatIsoDate,
  money,
  ordersApi,
  type CheckoutResult,
  type Order,
} from '../api'
import { ErrorBanner, OrderStatusBadge, Skeleton } from '../components/Bits'

/** Human-readable names for the saga steps the checkout service reports. */
const STEP_LABELS: Record<string, string> = {
  load_cart: 'Read your cart',
  reserve_stock: 'Reserved stock',
  create_order: 'Created the order',
  authorize_payment: 'Authorized payment',
  confirm_order: 'Confirmed the order',
  commit_stock: 'Committed stock',
  clear_cart: 'Emptied your cart',
}

export default function OrderConfirmation() {
  const { orderId = '' } = useParams()
  const location = useLocation()
  // Arriving straight from checkout, the saga detail is handed over in router
  // state. Arriving from a bookmark or a reload, it is simply absent.
  const checkoutResult = (location.state as { result?: CheckoutResult } | null)?.result

  const [order, setOrder] = useState<Order | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    window.scrollTo({ top: 0 })

    ordersApi
      .get(orderId, controller.signal)
      .then(setOrder)
      .catch((err) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        setError(err instanceof Error ? err.message : 'Could not load this order.')
      })
      .finally(() => setLoading(false))

    return () => controller.abort()
  }, [orderId])

  if (loading) {
    return (
      <div className="mx-auto max-w-3xl space-y-4 px-4 py-16 sm:px-6">
        <Skeleton className="h-16 w-16 rounded-full" />
        <Skeleton className="h-9 w-80" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  if (error || !order) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
        <ErrorBanner message={error ?? 'Order not found.'} />
        <Link to="/orders" className="btn-ghost mt-6">
          ← Your orders
        </Link>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-14 sm:px-6">
      <div className="text-center">
        <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-emerald-500/10 ring-1 ring-emerald-500/30">
          <svg viewBox="0 0 24 24" className="h-7 w-7 stroke-emerald-400" fill="none" strokeWidth="2">
            <path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
        <h1 className="mt-5 text-2xl font-semibold tracking-tight text-slate-50 sm:text-3xl">
          Order confirmed
        </h1>
        <p className="mt-2 text-sm text-slate-400">
          We sent a confirmation to {order.email}.
        </p>
        <p className="mt-4 inline-flex items-center gap-2 rounded-xl border border-white/10 bg-white/[0.03] px-3.5 py-2">
          <span className="text-xs text-slate-500">Order</span>
          <span className="font-mono text-sm text-slate-100">{order.order_id}</span>
        </p>
      </div>

      {/* Items */}
      <section className="card mt-10 p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-slate-200">What you bought</h2>
          <OrderStatusBadge status={order.status} />
        </div>

        <ul className="divide-y divide-white/5">
          {order.lines.map((line) => (
            <li key={line.product_id} className="flex items-center justify-between gap-4 py-3">
              <div className="min-w-0">
                <Link
                  to={`/product/${line.product_id}`}
                  className="block truncate text-sm text-slate-100 hover:text-white"
                >
                  {line.name}
                </Link>
                <p className="text-xs text-slate-500">
                  {line.quantity} × {money(line.unit_price)}
                </p>
              </div>
              <p className="shrink-0 text-sm tabular-nums text-slate-200">
                {money(line.line_total)}
              </p>
            </li>
          ))}
        </ul>

        <dl className="mt-4 space-y-2 border-t border-white/5 pt-4 text-sm">
          <div className="flex justify-between">
            <dt className="text-slate-400">Subtotal</dt>
            <dd className="tabular-nums text-slate-200">{money(order.totals.subtotal)}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-400">Tax</dt>
            <dd className="tabular-nums text-slate-200">{money(order.totals.tax)}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-400">Shipping</dt>
            <dd className="tabular-nums text-slate-200">
              {order.totals.shipping === 0 ? 'Free' : money(order.totals.shipping)}
            </dd>
          </div>
          <div className="flex justify-between border-t border-white/5 pt-2.5 text-base font-semibold">
            <dt className="text-slate-100">Total</dt>
            <dd className="tabular-nums text-slate-50">{money(order.totals.total)}</dd>
          </div>
        </dl>
      </section>

      {/* Delivery */}
      <section className="card mt-4 p-5">
        <h2 className="mb-3 text-sm font-semibold text-slate-200">Delivery</h2>
        <div className="grid gap-4 text-xs sm:grid-cols-2">
          <div>
            <p className="text-slate-500">Shipping to</p>
            <address className="mt-1 not-italic leading-relaxed text-slate-300">
              {order.shipping_address.full_name}
              <br />
              {order.shipping_address.line1}
              {order.shipping_address.line2 && (
                <>
                  <br />
                  {order.shipping_address.line2}
                </>
              )}
              <br />
              {order.shipping_address.city}, {order.shipping_address.region}{' '}
              {order.shipping_address.postal_code}
            </address>
          </div>
          <div>
            <p className="text-slate-500">Estimated arrival</p>
            <p className="mt-1 text-sm text-slate-200">
              {formatIsoDate(order.estimated_delivery)}
            </p>
            <p className="mt-3 text-slate-500">Method</p>
            <p className="mt-1 capitalize text-slate-300">{order.shipping_method}</p>
            {order.tracking_number && (
              <>
                <p className="mt-3 text-slate-500">Tracking</p>
                <p className="mt-1 font-mono text-slate-300">{order.tracking_number}</p>
              </>
            )}
          </div>
        </div>
      </section>

      {/* The saga, shown on purpose. */}
      {checkoutResult && (
        <section className="card mt-4 border-volt-500/20 p-5">
          <h2 className="mb-1 text-sm font-semibold text-slate-200">
            What happened behind the scenes
          </h2>
          <p className="mb-4 text-xs text-slate-500">
            This purchase crossed five services in {(checkoutResult.duration_ms / 1000).toFixed(2)}s.
            Each step is a span in one distributed trace.
          </p>

          <ol className="space-y-1.5">
            {checkoutResult.steps_completed.map((step, index) => (
              <li key={step} className="flex items-center gap-2.5 text-xs">
                <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-500/15 text-[10px] text-emerald-300">
                  {index + 1}
                </span>
                <span className="text-slate-300">{STEP_LABELS[step] ?? step}</span>
              </li>
            ))}
          </ol>

          {checkoutResult.trace_id && (
            <p className="mt-4 break-all border-t border-white/5 pt-3 font-mono text-[10px] text-slate-500">
              trace_id {checkoutResult.trace_id}
            </p>
          )}
        </section>
      )}

      <div className="mt-8 flex flex-wrap justify-center gap-3">
        <Link to="/orders" className="btn-ghost">
          All your orders
        </Link>
        <Link to="/catalog" className="btn-primary">
          Keep shopping
        </Link>
      </div>
    </div>
  )
}
