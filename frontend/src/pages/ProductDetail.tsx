import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  catalog,
  inventory,
  money,
  type Product,
  type ProductSummary,
  type StockRecord,
} from '../api'
import { ErrorBanner, Skeleton, StockBadge, Stars } from '../components/Bits'
import ProductCard from '../components/ProductCard'
import ProductVisual from '../components/ProductVisual'
import { useStore } from '../store'

export default function ProductDetail() {
  const { productId = '' } = useParams()
  const navigate = useNavigate()
  const { addItem } = useStore()

  const [product, setProduct] = useState<Product | null>(null)
  const [stock, setStock] = useState<StockRecord | null>(null)
  const [related, setRelated] = useState<ProductSummary[]>([])
  const [quantity, setQuantity] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [added, setAdded] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    setQuantity(1)
    setAdded(false)
    window.scrollTo({ top: 0 })

    catalog
      .get(productId, controller.signal)
      .then(async (found) => {
        setProduct(found)
        // Stock and related products are nice-to-have: a failure in either
        // should not stop the page rendering the product itself.
        void inventory.get(productId, controller.signal).then(setStock).catch(() => setStock(null))
        void catalog
          .list({ category: found.category, limit: 5 }, controller.signal)
          .then((page) =>
            setRelated(page.items.filter((item) => item.product_id !== productId).slice(0, 4)),
          )
          .catch(() => setRelated([]))
      })
      .catch((err) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        setError(err instanceof Error ? err.message : 'Could not load this product.')
      })
      .finally(() => setLoading(false))

    return () => controller.abort()
  }, [productId])

  async function handleAdd() {
    setAdding(true)
    try {
      await addItem(productId, quantity)
      setAdded(true)
      window.setTimeout(() => setAdded(false), 2200)
    } catch {
      /* the store surfaces the error */
    } finally {
      setAdding(false)
    }
  }

  if (loading) {
    return (
      <div className="mx-auto max-w-7xl px-4 py-10 sm:px-6 lg:px-8">
        <div className="grid gap-10 lg:grid-cols-2">
          <Skeleton className="aspect-[4/3] w-full" />
          <div className="space-y-4">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-9 w-3/4" />
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-12 w-48" />
          </div>
        </div>
      </div>
    )
  }

  if (error || !product) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
        <ErrorBanner message={error ?? 'Product not found.'} />
        <Link to="/catalog" className="btn-ghost mt-6">
          ← Back to the shop
        </Link>
      </div>
    )
  }

  const soldOut = stock?.status === 'out_of_stock'
  const maxQuantity = Math.min(10, Math.max(1, stock?.available ?? 10))

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <nav aria-label="Breadcrumb" className="mb-6 flex items-center gap-2 text-xs text-slate-500">
        <Link to="/catalog" className="hover:text-slate-300">Shop</Link>
        <span aria-hidden="true">/</span>
        <Link to={`/catalog?category=${product.category}`} className="hover:text-slate-300">
          {product.category.replace('-', ' ')}
        </Link>
        <span aria-hidden="true">/</span>
        <span className="truncate text-slate-400">{product.name}</span>
      </nav>

      <div className="grid gap-10 lg:grid-cols-2">
        <ProductVisual
          category={product.category}
          accent={product.image_accent}
          name={product.name}
          size="hero"
          className="aspect-[4/3] w-full rounded-3xl border border-white/5"
        />

        <div>
          <p className="text-xs font-medium uppercase tracking-wider text-volt-400">
            {product.brand}
          </p>
          <h1 className="mt-2 text-3xl font-semibold leading-tight tracking-tight text-slate-50">
            {product.name}
          </h1>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Stars rating={product.rating} reviews={product.review_count} />
            <StockBadge status={stock?.status ?? null} available={stock?.available} />
          </div>

          <p className="mt-6 text-4xl font-semibold tabular-nums text-slate-50">
            {money(product.price)}
          </p>
          <p className="mt-1 text-xs text-slate-500">
            {product.price >= 99
              ? 'Free standard shipping'
              : `Add ${money(99 - product.price)} more for free shipping`}
          </p>

          <p className="mt-6 text-sm leading-relaxed text-slate-300">{product.description}</p>

          <div className="mt-8 flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-slate-500">
              Qty
              <select
                value={quantity}
                onChange={(event) => setQuantity(Number(event.target.value))}
                disabled={soldOut}
                className="input !w-auto !py-2.5"
                aria-label="Quantity"
              >
                {Array.from({ length: maxQuantity }, (_, index) => index + 1).map((value) => (
                  <option key={value} value={value}>{value}</option>
                ))}
              </select>
            </label>

            <button
              type="button"
              onClick={handleAdd}
              disabled={adding || soldOut}
              className={`flex-1 sm:flex-none ${added ? 'btn bg-emerald-600 text-white !px-6 !py-3' : 'btn-primary !px-6 !py-3'}`}
            >
              {soldOut ? 'Out of stock' : adding ? 'Adding…' : added ? 'Added to cart ✓' : 'Add to cart'}
            </button>

            <button
              type="button"
              onClick={() => navigate('/cart')}
              className="btn-ghost !px-5 !py-3"
            >
              View cart
            </button>
          </div>

          {stock?.status === 'low_stock' && (
            <p className="mt-4 text-xs text-amber-300">
              Only {stock.available} left — worth ordering soon.
            </p>
          )}

          <div className="mt-10">
            <h2 className="mb-3 text-sm font-semibold text-slate-200">Specifications</h2>
            <dl className="divide-y divide-white/5 overflow-hidden rounded-xl border border-white/5">
              {Object.entries(product.specs).map(([key, value]) => (
                <div key={key} className="grid grid-cols-3 gap-4 px-4 py-2.5 text-xs">
                  <dt className="text-slate-500">{key.replace(/_/g, ' ')}</dt>
                  <dd className="col-span-2 text-slate-300">{value}</dd>
                </div>
              ))}
            </dl>
          </div>

          {product.tags.length > 0 && (
            <div className="mt-6 flex flex-wrap gap-1.5">
              {product.tags.map((tag) => (
                <span key={tag} className="pill bg-white/5 text-slate-400 ring-1 ring-white/10">
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {related.length > 0 && (
        <section className="mt-20">
          <h2 className="mb-5 text-xl font-semibold tracking-tight text-slate-50">
            More in {product.category.replace('-', ' ')}
          </h2>
          <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
            {related.map((item) => (
              <ProductCard key={item.product_id} product={item} />
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
