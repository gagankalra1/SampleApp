import { useState } from 'react'
import { Link } from 'react-router-dom'
import { money, type ProductSummary } from '../api'
import { useStore } from '../store'
import { Stars } from './Bits'
import ProductVisual from './ProductVisual'

export default function ProductCard({ product }: { product: ProductSummary }) {
  const { addItem, lastAdded } = useStore()
  const [busy, setBusy] = useState(false)
  const justAdded = lastAdded === product.product_id

  async function handleAdd(event: React.MouseEvent) {
    // The whole card is a link to the detail page; the button inside it must
    // not navigate.
    event.preventDefault()
    event.stopPropagation()
    setBusy(true)
    try {
      await addItem(product.product_id, 1)
    } catch {
      /* the store surfaces the error; the card just stops spinning */
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className="group card flex flex-col overflow-hidden transition-all duration-200 hover:border-volt-500/30 hover:shadow-glow">
      <Link
        to={`/product/${product.product_id}`}
        className="flex flex-1 flex-col focus-visible:outline-none"
        aria-label={`${product.name}, ${money(product.price)}`}
      >
        <div className="relative">
          <ProductVisual
            category={product.category}
            accent={product.image_accent}
            name={product.name}
            className="h-44 w-full transition-transform duration-300 group-hover:scale-[1.03]"
          />
          {product.featured && (
            <span className="pill absolute left-3 top-3 bg-ink-950/70 text-volt-300 ring-1 ring-volt-500/30 backdrop-blur">
              Staff pick
            </span>
          )}
        </div>

        <div className="flex flex-1 flex-col gap-2 p-4">
          <p className="text-[11px] font-medium uppercase tracking-wider text-slate-500">
            {product.brand}
          </p>
          <h3 className="text-sm font-semibold leading-snug text-slate-100 group-hover:text-white">
            {product.name}
          </h3>
          <p className="line-clamp-2 flex-1 text-xs leading-relaxed text-slate-400">
            {product.short_description}
          </p>
          <Stars rating={product.rating} reviews={product.review_count} />
        </div>
      </Link>

      <div className="flex items-center justify-between gap-3 border-t border-white/5 px-4 py-3">
        <span className="text-base font-semibold tabular-nums text-slate-50">
          {money(product.price)}
        </span>
        <button
          type="button"
          onClick={handleAdd}
          disabled={busy}
          className={justAdded ? 'btn bg-emerald-600 text-white !py-2 !text-xs' : 'btn-primary !py-2 !text-xs'}
          aria-label={`Add ${product.name} to cart`}
        >
          {busy ? 'Adding…' : justAdded ? 'Added ✓' : 'Add to cart'}
        </button>
      </div>
    </article>
  )
}

export function ProductCardSkeleton() {
  return (
    <div className="card overflow-hidden" aria-hidden="true">
      <div className="skeleton h-44 w-full rounded-none" />
      <div className="space-y-2.5 p-4">
        <div className="skeleton h-2.5 w-16" />
        <div className="skeleton h-3.5 w-4/5" />
        <div className="skeleton h-2.5 w-full" />
        <div className="skeleton h-2.5 w-2/3" />
      </div>
      <div className="flex items-center justify-between border-t border-white/5 px-4 py-3">
        <div className="skeleton h-4 w-20" />
        <div className="skeleton h-8 w-24" />
      </div>
    </div>
  )
}
