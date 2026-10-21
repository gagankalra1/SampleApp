import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ApiError,
  formatDate,
  formatIsoDate,
  money,
  ordersApi,
  type Order,
} from '../api'
import { EmptyState, ErrorBanner, OrderStatusBadge, Skeleton } from '../components/Bits'
import { useStore } from '../store'

export default function OrdersPage() {
  const { customerId } = useStore()
  const [orders, setOrders] = useState<Order[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ message: string; traceId: string | null } | null>(null)

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setError(null)
      try {
        setOrders(await ordersApi.listForCustomer(customerId, 25, signal))
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return
        setError(err instanceof Error ? err.message : 'Could not load your orders.')
      } finally {
        setLoading(false)
      }
    },
    [customerId],
  )

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function cancel(orderId: string) {
    setCancelling(orderId)
    setNotice(null)
    try {
      const updated = await ordersApi.cancel(orderId, 'customer_request')
      setOrders((previous) =>
        previous.map((order) => (order.order_id === orderId ? updated : order)),
      )
      setNotice({ message: `Order ${orderId} was cancelled and the stock released.`, traceId: null })
    } catch (err) {
      const apiError = err instanceof ApiError ? err : null
      setNotice({
        message: apiError?.message ?? 'Could not cancel that order.',
        traceId: apiError?.traceId ?? null,
      })
    } finally {
      setCancelling(null)
    }
  }

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl space-y-4 px-4 py-10 sm:px-6">
        <Skeleton className="h-8 w-48" />
        {Array.from({ length: 3 }, (_, index) => (
          <Skeleton key={index} className="h-36 w-full" />
        ))}
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6 lg:px-8">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-50 sm:text-3xl">
          Your orders
        </h1>
        <p className="mt-1.5 text-xs text-slate-500">
          Signed in as <span className="font-mono text-slate-400">{customerId}</span> — a demo
          identity stored in this browser.
        </p>
      </div>

      {error && (
        <div className="mb-6">
          <ErrorBanner message={error} onRetry={() => void load()} />
        </div>
      )}

      {notice && (
        <div className="mb-6">
          <ErrorBanner message={notice.message} traceId={notice.traceId} />
        </div>
      )}

      {orders.length === 0 && !error ? (
        <EmptyState
          title="No orders yet"
          body="Once you buy something it will show up here, and Volt will be able to track it for you."
          action={
            <Link to="/catalog" className="btn-primary mt-1">
              Start shopping
            </Link>
          }
        />
      ) : (
        <div className="space-y-4">
          {orders.map((order) => (
            <article key={order.order_id} className="card p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <Link
                    to={`/order/${order.order_id}`}
                    className="font-mono text-sm text-slate-100 hover:text-white"
                  >
                    {order.order_id}
                  </Link>
                  <p className="mt-1 text-xs text-slate-500">
                    Placed {formatDate(order.created_at)} · {order.lines.length} item
                    {order.lines.length === 1 ? '' : 's'}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <OrderStatusBadge status={order.status} />
                  <span className="text-sm font-semibold tabular-nums text-slate-50">
                    {money(order.totals.total)}
                  </span>
                </div>
              </div>

              <ul className="mt-4 space-y-1.5">
                {order.lines.slice(0, 3).map((line) => (
                  <li key={line.product_id} className="truncate text-xs text-slate-400">
                    {line.quantity} × {line.name}
                  </li>
                ))}
                {order.lines.length > 3 && (
                  <li className="text-xs text-slate-600">
                    and {order.lines.length - 3} more
                  </li>
                )}
              </ul>

              <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-white/5 pt-4 text-xs">
                {order.status === 'shipped' && order.tracking_number ? (
                  <span className="text-slate-400">
                    Tracking <span className="font-mono text-slate-300">{order.tracking_number}</span>
                  </span>
                ) : order.status === 'delivered' ? (
                  <span className="text-emerald-300">Delivered</span>
                ) : order.status === 'cancelled' ? (
                  <span className="text-slate-500">Cancelled</span>
                ) : (
                  <span className="text-slate-400">
                    Arriving {formatIsoDate(order.estimated_delivery)}
                  </span>
                )}

                <Link
                  to={`/order/${order.order_id}`}
                  className="ml-auto text-volt-400 hover:text-volt-300"
                >
                  View details →
                </Link>

                {order.cancellable && (
                  <button
                    type="button"
                    onClick={() => void cancel(order.order_id)}
                    disabled={cancelling === order.order_id}
                    className="text-slate-500 underline-offset-2 hover:text-rose-300 hover:underline disabled:opacity-50"
                  >
                    {cancelling === order.order_id ? 'Cancelling…' : 'Cancel order'}
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
