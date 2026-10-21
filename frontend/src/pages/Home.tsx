import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { catalog, type CategoryInfo, type ProductSummary } from '../api'
import { ErrorBanner, SectionHeading } from '../components/Bits'
import ProductCard, { ProductCardSkeleton } from '../components/ProductCard'

const CATEGORY_BLURBS: Record<string, string> = {
  laptops: 'Thin and light to desktop-replacement',
  smartphones: 'Flagships, compacts and foldables',
  audio: 'Headphones, earbuds and monitors',
  wearables: 'Health tracking and multisport',
  televisions: 'OLED and mini-LED panels',
  cameras: 'Mirrorless bodies, glass and action',
  gaming: 'Consoles, handhelds and controllers',
  'smart-home': 'Hubs, cameras and doorbells',
  accessories: 'Charging, docks and keyboards',
}

export default function Home() {
  const [featured, setFeatured] = useState<ProductSummary[]>([])
  const [categories, setCategories] = useState<CategoryInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)

    Promise.all([
      catalog.list({ featured: true, limit: 8 }, controller.signal),
      catalog.categories(controller.signal),
    ])
      .then(([page, cats]) => {
        setFeatured(page.items)
        setCategories(cats)
      })
      .catch((err) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        setError(err instanceof Error ? err.message : 'Could not load the storefront.')
      })
      .finally(() => setLoading(false))

    return () => controller.abort()
  }, [attempt])

  return (
    <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
      {/* Hero */}
      <section className="relative overflow-hidden rounded-3xl border border-white/5 bg-ink-900/60 px-6 py-14 sm:px-12 sm:py-20 mt-8">
        <div
          aria-hidden="true"
          className="absolute -right-24 -top-24 h-80 w-80 rounded-full bg-volt-500/20 blur-3xl"
        />
        <div
          aria-hidden="true"
          className="absolute -bottom-32 -left-20 h-80 w-80 rounded-full bg-spark-500/10 blur-3xl"
        />
        <div className="relative max-w-2xl">
          <span className="pill bg-volt-500/10 text-volt-300 ring-1 ring-volt-500/20">
            Free shipping over $99
          </span>
          <h1 className="mt-5 text-4xl font-semibold leading-[1.1] tracking-tight text-slate-50 sm:text-6xl">
            Power up
            <br />
            <span className="bg-gradient-to-r from-volt-400 to-spark-400 bg-clip-text text-transparent">
              your tech.
            </span>
          </h1>
          <p className="mt-5 max-w-lg text-base leading-relaxed text-slate-400">
            Laptops, phones, audio, cameras and gaming gear, picked rather than piled up. And an
            assistant that can actually check your order instead of reading you a script.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link to="/catalog" className="btn-primary !px-5 !py-3">
              Shop everything
            </Link>
            <Link to="/catalog?category=laptops" className="btn-ghost !px-5 !py-3">
              See laptops
            </Link>
          </div>
        </div>
      </section>

      {error && (
        <div className="mt-8">
          <ErrorBanner message={error} onRetry={() => setAttempt((n) => n + 1)} />
        </div>
      )}

      {/* Categories */}
      <section className="mt-16">
        <SectionHeading eyebrow="Browse" title="Shop by category" />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {loading
            ? Array.from({ length: 6 }, (_, index) => (
                <div key={index} className="skeleton h-24" />
              ))
            : categories.map((category) => (
                <Link
                  key={category.category}
                  to={`/catalog?category=${category.category}`}
                  className="card group flex items-center justify-between gap-4 p-4 transition-all hover:border-volt-500/30 hover:shadow-glow"
                >
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-slate-100 group-hover:text-white">
                      {category.label}
                    </p>
                    <p className="mt-0.5 truncate text-xs text-slate-500">
                      {CATEGORY_BLURBS[category.category] ?? `${category.product_count} products`}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2 text-xs text-slate-500">
                    <span>{category.product_count}</span>
                    <svg
                      viewBox="0 0 20 20"
                      className="h-4 w-4 fill-slate-600 transition-transform group-hover:translate-x-0.5 group-hover:fill-volt-400"
                      aria-hidden="true"
                    >
                      <path d="M7.5 4.5l6 5.5-6 5.5V4.5z" />
                    </svg>
                  </div>
                </Link>
              ))}
        </div>
      </section>

      {/* Featured */}
      <section className="mt-16">
        <SectionHeading
          eyebrow="Staff picks"
          title="What we would buy"
          action={
            <Link to="/catalog" className="text-sm text-volt-400 hover:text-volt-300">
              All products →
            </Link>
          }
        />
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
          {loading
            ? Array.from({ length: 4 }, (_, index) => <ProductCardSkeleton key={index} />)
            : featured.map((product) => (
                <ProductCard key={product.product_id} product={product} />
              ))}
        </div>
      </section>

      {/* Assistant teaser */}
      <section className="mt-16 overflow-hidden rounded-3xl border border-white/5 bg-gradient-to-br from-ink-900 to-ink-850 p-8 sm:p-12">
        <div className="flex flex-col gap-8 lg:flex-row lg:items-center">
          <div className="flex-1">
            <span className="pill bg-spark-500/10 text-spark-400 ring-1 ring-spark-500/20">
              Built with Strands Agents
            </span>
            <h2 className="mt-4 text-2xl font-semibold tracking-tight text-slate-50 sm:text-3xl">
              Ask Volt anything about your order
            </h2>
            <p className="mt-3 max-w-xl text-sm leading-relaxed text-slate-400">
              Volt has fifteen tools wired to the services behind this store. It can search the
              catalog, check stock, read your cart, track a shipment, cancel an order or hand you
              to a human. Each reply shows which tools it used and the trace id for the turn.
            </p>
          </div>
          <div className="w-full max-w-sm space-y-2.5 lg:w-80">
            {[
              { role: 'user', text: 'Where is my laptop order?' },
              { role: 'tools', text: 'list_my_orders · track_shipment' },
              {
                role: 'assistant',
                text: 'Your Voltaire Pro 16 shipped yesterday and should arrive Thursday.',
              },
            ].map((line) => (
              <div
                key={line.text}
                className={
                  line.role === 'user'
                    ? 'ml-auto w-fit max-w-[90%] rounded-2xl bg-volt-600 px-3.5 py-2.5 text-xs text-white'
                    : line.role === 'tools'
                      ? 'w-fit rounded-full bg-spark-500/10 px-2.5 py-1 font-mono text-[10px] text-spark-400 ring-1 ring-spark-500/20'
                      : 'w-fit max-w-[90%] rounded-2xl border border-white/5 bg-ink-800 px-3.5 py-2.5 text-xs text-slate-200'
                }
              >
                {line.text}
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  )
}
