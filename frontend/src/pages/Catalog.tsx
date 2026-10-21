import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { catalog, type CategoryInfo, type ProductPage, type ProductQuery } from '../api'
import { EmptyState, ErrorBanner } from '../components/Bits'
import ProductCard, { ProductCardSkeleton } from '../components/ProductCard'

const PAGE_SIZE = 12

const SORTS: { value: NonNullable<ProductQuery['sort']>; label: string }[] = [
  { value: 'relevance', label: 'Most relevant' },
  { value: 'price_asc', label: 'Price: low to high' },
  { value: 'price_desc', label: 'Price: high to low' },
  { value: 'rating', label: 'Best rated' },
]

const PRICE_BANDS = [
  { label: 'Under $250', max: 250 },
  { label: '$250 – $750', min: 250, max: 750 },
  { label: '$750 – $1,500', min: 750, max: 1500 },
  { label: 'Over $1,500', min: 1500 },
]

export default function Catalog() {
  // The URL is the source of truth for filters, so a filtered view can be
  // shared, bookmarked and survives a reload.
  const [params, setParams] = useSearchParams()
  const [page, setPage] = useState<ProductPage | null>(null)
  const [categories, setCategories] = useState<CategoryInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  const query = useMemo<ProductQuery>(() => {
    const min = params.get('min_price')
    const max = params.get('max_price')
    return {
      q: params.get('q') ?? undefined,
      category: params.get('category') ?? undefined,
      minPrice: min ? Number(min) : undefined,
      maxPrice: max ? Number(max) : undefined,
      sort: (params.get('sort') as ProductQuery['sort']) ?? 'relevance',
      limit: PAGE_SIZE,
      offset: Number(params.get('offset') ?? 0),
    }
  }, [params])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)

    catalog
      .list(query, controller.signal)
      .then(setPage)
      .catch((err) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        setError(err instanceof Error ? err.message : 'Could not load products.')
      })
      .finally(() => setLoading(false))

    return () => controller.abort()
  }, [query, attempt])

  useEffect(() => {
    const controller = new AbortController()
    catalog.categories(controller.signal).then(setCategories).catch(() => setCategories([]))
    return () => controller.abort()
  }, [])

  /** Merge changes into the query string, resetting pagination. */
  const update = useCallback(
    (changes: Record<string, string | null>) => {
      const next = new URLSearchParams(params)
      for (const [key, value] of Object.entries(changes)) {
        if (value === null || value === '') next.delete(key)
        else next.set(key, value)
      }
      if (!('offset' in changes)) next.delete('offset')
      setParams(next, { replace: true })
    },
    [params, setParams],
  )

  const activeBand = PRICE_BANDS.findIndex(
    (band) =>
      (band.min ?? undefined) === query.minPrice && (band.max ?? undefined) === query.maxPrice,
  )
  const hasFilters = Boolean(query.q || query.category || query.minPrice || query.maxPrice)
  const offset = query.offset ?? 0
  const total = page?.total ?? 0

  return (
    <div className="mx-auto max-w-7xl px-4 py-10 sm:px-6 lg:px-8">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-50 sm:text-3xl">
          {query.q
            ? `Results for "${query.q}"`
            : categories.find((c) => c.category === query.category)?.label ?? 'All products'}
        </h1>
        <p className="mt-1.5 text-sm text-slate-500">
          {loading ? 'Loading…' : `${total} product${total === 1 ? '' : 's'}`}
        </p>
      </div>

      <div className="flex flex-col gap-8 lg:flex-row">
        {/* Filters */}
        <aside className="lg:w-60 lg:shrink-0" aria-label="Filters">
          <div className="space-y-6 lg:sticky lg:top-24">
            {hasFilters && (
              <button
                type="button"
                onClick={() => setParams(new URLSearchParams(), { replace: true })}
                className="btn-ghost w-full !py-2 !text-xs"
              >
                Clear all filters
              </button>
            )}

            <fieldset>
              <legend className="label">Category</legend>
              <div className="space-y-0.5">
                <button
                  type="button"
                  onClick={() => update({ category: null })}
                  className={`block w-full rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors ${
                    !query.category ? 'bg-volt-500/10 text-volt-300' : 'text-slate-400 hover:bg-white/5'
                  }`}
                >
                  All categories
                </button>
                {categories.map((category) => (
                  <button
                    key={category.category}
                    type="button"
                    onClick={() => update({ category: category.category })}
                    className={`flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors ${
                      query.category === category.category
                        ? 'bg-volt-500/10 text-volt-300'
                        : 'text-slate-400 hover:bg-white/5'
                    }`}
                  >
                    <span>{category.label}</span>
                    <span className="text-xs text-slate-600">{category.product_count}</span>
                  </button>
                ))}
              </div>
            </fieldset>

            <fieldset>
              <legend className="label">Price</legend>
              <div className="space-y-0.5">
                {PRICE_BANDS.map((band, index) => (
                  <button
                    key={band.label}
                    type="button"
                    onClick={() =>
                      update(
                        activeBand === index
                          ? { min_price: null, max_price: null }
                          : {
                              min_price: band.min ? String(band.min) : null,
                              max_price: band.max ? String(band.max) : null,
                            },
                      )
                    }
                    className={`block w-full rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors ${
                      activeBand === index
                        ? 'bg-volt-500/10 text-volt-300'
                        : 'text-slate-400 hover:bg-white/5'
                    }`}
                  >
                    {band.label}
                  </button>
                ))}
              </div>
            </fieldset>
          </div>
        </aside>

        {/* Results */}
        <div className="min-w-0 flex-1">
          <div className="mb-5 flex items-center justify-end">
            <label className="flex items-center gap-2 text-xs text-slate-500">
              Sort
              <select
                value={query.sort}
                onChange={(event) => update({ sort: event.target.value })}
                className="input !w-auto !py-1.5 !text-xs"
              >
                {SORTS.map((sort) => (
                  <option key={sort.value} value={sort.value}>
                    {sort.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {error ? (
            <ErrorBanner message={error} onRetry={() => setAttempt((n) => n + 1)} />
          ) : loading ? (
            <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 6 }, (_, index) => (
                <ProductCardSkeleton key={index} />
              ))}
            </div>
          ) : total === 0 ? (
            <EmptyState
              title="Nothing matched"
              body="Try a broader search or clear a filter. Volt can help you find something too — the button is in the corner."
              action={
                <button
                  type="button"
                  onClick={() => setParams(new URLSearchParams(), { replace: true })}
                  className="btn-primary mt-1"
                >
                  Clear filters
                </button>
              }
            />
          ) : (
            <>
              <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
                {page?.items.map((product) => (
                  <ProductCard key={product.product_id} product={product} />
                ))}
              </div>

              {total > PAGE_SIZE && (
                <nav
                  className="mt-10 flex items-center justify-between gap-4"
                  aria-label="Pagination"
                >
                  <button
                    type="button"
                    disabled={offset === 0}
                    onClick={() => update({ offset: String(Math.max(0, offset - PAGE_SIZE)) })}
                    className="btn-ghost !text-xs"
                  >
                    ← Previous
                  </button>
                  <span className="text-xs text-slate-500">
                    {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
                  </span>
                  <button
                    type="button"
                    disabled={offset + PAGE_SIZE >= total}
                    onClick={() => update({ offset: String(offset + PAGE_SIZE) })}
                    className="btn-ghost !text-xs"
                  >
                    Next →
                  </button>
                </nav>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
