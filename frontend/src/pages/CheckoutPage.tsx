import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, checkoutApi, money, type CheckoutPayload } from '../api'
import { ErrorBanner } from '../components/Bits'
import { useStore } from '../store'

/**
 * Checkout. The card fields are plain inputs posting to the simulated payment
 * service, which accepts only the documented fake numbers.
 *
 * A production checkout would not do this. The card number would go straight
 * from the browser to the payment processor's own SDK, which returns a
 * single-use token, and only that token would reach our servers — so a
 * compromise of this application could never expose card data. Tokenisation
 * needs a real processor account, so the sample simulates the gateway instead
 * and says so plainly on screen.
 */

const TEST_CARDS = [
  { number: '4242424242424242', label: '4242 4242 4242 4242', result: 'Approved' },
  { number: '4000000000000002', label: '4000 0000 0000 0002', result: 'Declined — no funds' },
  { number: '4000000000000069', label: '4000 0000 0000 0069', result: 'Declined — expired' },
  { number: '4100000000000019', label: '4100 0000 0000 0019', result: 'Declined — fraud' },
]

const SHIPPING_OPTIONS = [
  { value: 'standard', label: 'Standard', detail: '5 business days', price: 'Free over $99' },
  { value: 'express', label: 'Express', detail: '2 business days', price: '$19.90' },
  { value: 'overnight', label: 'Overnight', detail: 'Next business day', price: '$29.85' },
] as const

export default function CheckoutPage() {
  const navigate = useNavigate()
  const { cart, cartId, customerId, resetCart } = useStore()

  const [form, setForm] = useState({
    email: 'shopper@example.com',
    full_name: 'Alex Rivera',
    line1: '410 Terry Avenue North',
    line2: '',
    city: 'Seattle',
    region: 'WA',
    postal_code: '98109',
    country: 'US',
    shipping_method: 'standard' as CheckoutPayload['shipping_method'],
    card_number: '4242424242424242',
    exp_month: '12',
    exp_year: '2030',
    cvc: '123',
    holder_name: 'Alex Rivera',
  })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<{ message: string; traceId: string | null } | null>(null)

  // An empty cart cannot be checked out; send them back rather than showing a
  // form that is guaranteed to fail.
  useEffect(() => {
    if (cart && cart.lines.length === 0 && !submitting) navigate('/cart', { replace: true })
  }, [cart, navigate, submitting])

  function set<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((previous) => ({ ...previous, [key]: value }))
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!cartId) return

    setSubmitting(true)
    setError(null)

    try {
      const result = await checkoutApi.submit({
        cart_id: cartId,
        customer_id: customerId,
        email: form.email,
        shipping_address: {
          full_name: form.full_name,
          line1: form.line1,
          line2: form.line2,
          city: form.city,
          region: form.region,
          postal_code: form.postal_code,
          country: form.country,
        },
        shipping_method: form.shipping_method,
        payment_method: 'card',
        card: {
          number: form.card_number.replace(/\s/g, ''),
          exp_month: Number(form.exp_month),
          exp_year: Number(form.exp_year),
          cvc: form.cvc,
          holder_name: form.holder_name,
        },
      })

      await resetCart()
      navigate(`/order/${result.order_id}`, { state: { result }, replace: true })
    } catch (err) {
      const apiError = err instanceof ApiError ? err : null
      setError({
        message: apiError?.message ?? 'Checkout could not be completed.',
        traceId: apiError?.traceId ?? null,
      })
      // Scroll the error into view; on mobile the submit button is well below
      // the top of the form.
      window.scrollTo({ top: 0, behavior: 'smooth' })
    } finally {
      setSubmitting(false)
    }
  }

  const totals = cart?.totals

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6 lg:px-8">
      <h1 className="mb-8 text-2xl font-semibold tracking-tight text-slate-50 sm:text-3xl">
        Checkout
      </h1>

      {error && (
        <div className="mb-6">
          <ErrorBanner message={error.message} traceId={error.traceId} />
        </div>
      )}

      <form onSubmit={submit} className="grid gap-8 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          {/* Contact */}
          <section className="card p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-200">Contact</h2>
            <label>
              <span className="label">Email for order updates</span>
              <input
                type="email"
                required
                autoComplete="email"
                value={form.email}
                onChange={(event) => set('email', event.target.value)}
                className="input"
              />
            </label>
          </section>

          {/* Shipping address */}
          <section className="card p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-200">Shipping address</h2>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="sm:col-span-2">
                <span className="label">Full name</span>
                <input
                  required
                  autoComplete="name"
                  value={form.full_name}
                  onChange={(event) => set('full_name', event.target.value)}
                  className="input"
                />
              </label>
              <label className="sm:col-span-2">
                <span className="label">Address</span>
                <input
                  required
                  autoComplete="address-line1"
                  value={form.line1}
                  onChange={(event) => set('line1', event.target.value)}
                  className="input"
                />
              </label>
              <label className="sm:col-span-2">
                <span className="label">Apartment, suite (optional)</span>
                <input
                  autoComplete="address-line2"
                  value={form.line2}
                  onChange={(event) => set('line2', event.target.value)}
                  className="input"
                />
              </label>
              <label>
                <span className="label">City</span>
                <input
                  required
                  autoComplete="address-level2"
                  value={form.city}
                  onChange={(event) => set('city', event.target.value)}
                  className="input"
                />
              </label>
              <label>
                <span className="label">State</span>
                <input
                  required
                  autoComplete="address-level1"
                  value={form.region}
                  onChange={(event) => set('region', event.target.value)}
                  className="input"
                />
              </label>
              <label>
                <span className="label">ZIP code</span>
                <input
                  required
                  autoComplete="postal-code"
                  inputMode="numeric"
                  value={form.postal_code}
                  onChange={(event) => set('postal_code', event.target.value)}
                  className="input"
                />
              </label>
              <label>
                <span className="label">Country</span>
                <input
                  required
                  maxLength={2}
                  autoComplete="country"
                  value={form.country}
                  onChange={(event) => set('country', event.target.value.toUpperCase())}
                  className="input"
                />
              </label>
            </div>
          </section>

          {/* Shipping method */}
          <section className="card p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-200">Shipping method</h2>
            <div className="space-y-2">
              {SHIPPING_OPTIONS.map((option) => (
                <label
                  key={option.value}
                  className={`flex cursor-pointer items-center gap-3 rounded-xl border px-4 py-3 transition-colors ${
                    form.shipping_method === option.value
                      ? 'border-volt-500/50 bg-volt-500/[0.08]'
                      : 'border-white/10 hover:border-white/20'
                  }`}
                >
                  <input
                    type="radio"
                    name="shipping_method"
                    value={option.value}
                    checked={form.shipping_method === option.value}
                    onChange={() => set('shipping_method', option.value)}
                    className="h-4 w-4 accent-volt-500"
                  />
                  <span className="flex-1">
                    <span className="block text-sm text-slate-100">{option.label}</span>
                    <span className="block text-xs text-slate-500">{option.detail}</span>
                  </span>
                  <span className="text-xs text-slate-400">{option.price}</span>
                </label>
              ))}
            </div>
          </section>

          {/* Payment */}
          <section className="card p-5">
            <div className="mb-4 flex items-center justify-between gap-3">
              <h2 className="text-sm font-semibold text-slate-200">Payment</h2>
              <span className="pill bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/20">
                Simulated
              </span>
            </div>

            <p className="mb-4 rounded-xl bg-white/[0.03] px-3.5 py-3 text-xs leading-relaxed text-slate-400">
              No real card is accepted and no money moves. Only the test numbers below work. The
              card number is never stored or logged — the payment service keeps the last four
              digits and the brand, and nothing else.
            </p>

            <div className="grid gap-4 sm:grid-cols-2">
              <label className="sm:col-span-2">
                <span className="label">Card number</span>
                <input
                  required
                  inputMode="numeric"
                  autoComplete="off"
                  value={form.card_number}
                  onChange={(event) => set('card_number', event.target.value)}
                  className="input font-mono"
                />
              </label>
              <label className="sm:col-span-2">
                <span className="label">Name on card</span>
                <input
                  required
                  autoComplete="off"
                  value={form.holder_name}
                  onChange={(event) => set('holder_name', event.target.value)}
                  className="input"
                />
              </label>
              <label>
                <span className="label">Expiry month</span>
                <input
                  required
                  inputMode="numeric"
                  value={form.exp_month}
                  onChange={(event) => set('exp_month', event.target.value)}
                  className="input"
                />
              </label>
              <label>
                <span className="label">Expiry year</span>
                <input
                  required
                  inputMode="numeric"
                  value={form.exp_year}
                  onChange={(event) => set('exp_year', event.target.value)}
                  className="input"
                />
              </label>
              <label>
                <span className="label">Security code</span>
                <input
                  required
                  inputMode="numeric"
                  autoComplete="off"
                  value={form.cvc}
                  onChange={(event) => set('cvc', event.target.value)}
                  className="input"
                />
              </label>
            </div>

            <div className="mt-4">
              <p className="label">Test cards</p>
              <div className="flex flex-wrap gap-1.5">
                {TEST_CARDS.map((card) => (
                  <button
                    key={card.number}
                    type="button"
                    onClick={() => set('card_number', card.number)}
                    className={`rounded-lg border px-2.5 py-1.5 text-left text-[11px] transition-colors ${
                      form.card_number === card.number
                        ? 'border-volt-500/50 bg-volt-500/10 text-volt-200'
                        : 'border-white/10 text-slate-400 hover:border-white/20 hover:text-slate-200'
                    }`}
                  >
                    <span className="block font-mono">{card.label}</span>
                    <span className="block text-slate-500">{card.result}</span>
                  </button>
                ))}
              </div>
            </div>
          </section>
        </div>

        {/* Summary */}
        <aside className="lg:col-span-1">
          <div className="card p-5 lg:sticky lg:top-24">
            <h2 className="mb-4 text-sm font-semibold text-slate-200">
              {totals?.item_count ?? 0} item{totals?.item_count === 1 ? '' : 's'}
            </h2>

            <ul className="mb-4 max-h-56 space-y-2.5 overflow-y-auto scroll-slim">
              {cart?.lines.map((line) => (
                <li key={line.product_id} className="flex justify-between gap-3 text-xs">
                  <span className="min-w-0 flex-1 truncate text-slate-400">
                    {line.quantity} × {line.name}
                  </span>
                  <span className="shrink-0 tabular-nums text-slate-300">
                    {money(line.line_total)}
                  </span>
                </li>
              ))}
            </ul>

            <dl className="space-y-2.5 border-t border-white/5 pt-4 text-sm">
              <div className="flex justify-between">
                <dt className="text-slate-400">Subtotal</dt>
                <dd className="tabular-nums text-slate-200">{money(totals?.subtotal ?? 0)}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-slate-400">Tax</dt>
                <dd className="tabular-nums text-slate-200">{money(totals?.tax ?? 0)}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-slate-400">Shipping</dt>
                <dd className="tabular-nums text-slate-200">
                  {form.shipping_method === 'standard'
                    ? totals?.free_shipping_applied
                      ? 'Free'
                      : money(totals?.shipping ?? 0)
                    : form.shipping_method === 'express'
                      ? '$19.90'
                      : '$29.85'}
                </dd>
              </div>
              <div className="flex justify-between border-t border-white/5 pt-3 text-base font-semibold">
                <dt className="text-slate-100">Total</dt>
                <dd className="tabular-nums text-slate-50">{money(totals?.total ?? 0)}</dd>
              </div>
            </dl>

            <p className="mt-2 text-[10px] text-slate-600">
              Express and overnight shipping adjust the total server-side when the order is priced.
            </p>

            <button type="submit" disabled={submitting} className="btn-primary mt-5 w-full !py-3">
              {submitting ? 'Placing your order…' : `Pay ${money(totals?.total ?? 0)}`}
            </button>

            <Link to="/cart" className="btn-ghost mt-2.5 w-full !py-2.5 !text-xs">
              Back to cart
            </Link>
          </div>
        </aside>
      </form>
    </div>
  )
}
