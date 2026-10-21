import { Link } from 'react-router-dom'
import { money } from '../api'
import { EmptyState, ErrorBanner, Skeleton, StockBadge } from '../components/Bits'
import ProductVisual from '../components/ProductVisual'
import { useStore } from '../store'

export default function CartPage() {
  const { cart, loading, error, setQuantity, removeItem, refresh } = useStore()

  if (loading) {
    return (
      <div className="mx-auto max-w-5xl space-y-4 px-4 py-10 sm:px-6">
        <Skeleton className="h-8 w-48" />
        {Array.from({ length: 3 }, (_, index) => (
          <Skeleton key={index} className="h-28 w-full" />
        ))}
      </div>
    )
  }

  const lines = cart?.lines ?? []
  const totals = cart?.totals

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6 lg:px-8">
      <h1 className="mb-8 text-2xl font-semibold tracking-tight text-slate-50 sm:text-3xl">
        Your cart
      </h1>

      {error && (
        <div className="mb-6">
          <ErrorBanner message={error} onRetry={() => void refresh()} />
        </div>
      )}

      {lines.length === 0 ? (
        <EmptyState
          title="Your cart is empty"
          body="Nothing in here yet. Have a look at the shop, or ask Volt for a recommendation."
          action={
            <Link to="/catalog" className="btn-primary mt-1">
              Start shopping
            </Link>
          }
        />
      ) : (
        <div className="grid gap-8 lg:grid-cols-3">
          {/* Lines */}
          <div className="space-y-3 lg:col-span-2">
            {cart?.has_stock_warnings && (
              <div
                role="status"
                className="card border-amber-500/20 bg-amber-500/[0.06] px-4 py-3 text-sm text-amber-100"
              >
                Some items are short on stock. Reduce the quantity before checking out, or
                checkout will be refused.
              </div>
            )}

            {lines.map((line) => (
              <div key={line.product_id} className="card flex gap-4 p-4">
                <Link to={`/product/${line.product_id}`} className="shrink-0">
                  <ProductVisual
                    category={line.category}
                    accent={line.image_accent}
                    name={line.name}
                    className="h-24 w-24 rounded-xl"
                  />
                </Link>

                <div className="flex min-w-0 flex-1 flex-col">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-[11px] uppercase tracking-wider text-slate-500">
                        {line.brand}
                      </p>
                      <Link
                        to={`/product/${line.product_id}`}
                        className="block truncate text-sm font-semibold text-slate-100 hover:text-white"
                      >
                        {line.name}
                      </Link>
                      <p className="mt-1 text-xs text-slate-500">
                        {money(line.unit_price)} each
                      </p>
                    </div>
                    <p className="shrink-0 text-sm font-semibold tabular-nums text-slate-50">
                      {money(line.line_total)}
                    </p>
                  </div>

                  <div className="mt-auto flex flex-wrap items-center gap-3 pt-3">
                    <label className="flex items-center gap-1.5 text-xs text-slate-500">
                      <span className="sr-only">Quantity for {line.name}</span>
                      <select
                        value={line.quantity}
                        onChange={(event) =>
                          void setQuantity(line.product_id, Number(event.target.value))
                        }
                        className="input !w-auto !py-1.5 !text-xs"
                        aria-label={`Quantity for ${line.name}`}
                      >
                        {Array.from({ length: 20 }, (_, index) => index + 1).map((value) => (
                          <option key={value} value={value}>{value}</option>
                        ))}
                      </select>
                    </label>

                    <StockBadge status={line.stock_status} available={line.available} />

                    {line.exceeds_available && (
                      <span className="pill bg-rose-500/10 text-rose-300 ring-1 ring-rose-500/20">
                        Only {line.available} available
                      </span>
                    )}

                    <button
                      type="button"
                      onClick={() => void removeItem(line.product_id)}
                      className="ml-auto text-xs text-slate-500 underline-offset-2 hover:text-rose-300 hover:underline"
                    >
                      Remove
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Summary */}
          <aside className="lg:col-span-1">
            <div className="card p-5 lg:sticky lg:top-24">
              <h2 className="mb-4 text-sm font-semibold text-slate-200">Order summary</h2>

              <dl className="space-y-2.5 text-sm">
                <div className="flex justify-between">
                  <dt className="text-slate-400">Subtotal</dt>
                  <dd className="tabular-nums text-slate-200">{money(totals?.subtotal ?? 0)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-slate-400">Estimated tax</dt>
                  <dd className="tabular-nums text-slate-200">{money(totals?.tax ?? 0)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-slate-400">Shipping</dt>
                  <dd className="tabular-nums text-slate-200">
                    {totals?.free_shipping_applied ? (
                      <span className="text-emerald-300">Free</span>
                    ) : (
                      money(totals?.shipping ?? 0)
                    )}
                  </dd>
                </div>
                <div className="flex justify-between border-t border-white/5 pt-3 text-base font-semibold">
                  <dt className="text-slate-100">Total</dt>
                  <dd className="tabular-nums text-slate-50">{money(totals?.total ?? 0)}</dd>
                </div>
              </dl>

              {totals && !totals.free_shipping_applied && totals.amount_to_free_shipping > 0 && (
                <div className="mt-4 rounded-xl bg-volt-500/[0.08] px-3 py-2.5 text-xs text-volt-200">
                  Add {money(totals.amount_to_free_shipping)} more for free shipping.
                </div>
              )}

              <Link
                to="/checkout"
                className={`btn-primary mt-5 w-full !py-3 ${cart?.has_stock_warnings ? 'pointer-events-none opacity-40' : ''}`}
                aria-disabled={cart?.has_stock_warnings}
              >
                Checkout
              </Link>

              <Link to="/catalog" className="btn-ghost mt-2.5 w-full !py-2.5 !text-xs">
                Keep shopping
              </Link>
            </div>
          </aside>
        </div>
      )}
    </div>
  )
}
