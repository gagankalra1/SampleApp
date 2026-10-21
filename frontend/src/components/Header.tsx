import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, useNavigate } from 'react-router-dom'
import { useStore } from '../store'

const NAV = [
  { to: '/catalog', label: 'Shop' },
  { to: '/catalog?category=laptops', label: 'Laptops' },
  { to: '/catalog?category=audio', label: 'Audio' },
  { to: '/catalog?category=gaming', label: 'Gaming' },
  { to: '/orders', label: 'Orders' },
]

export function Logo({ className = '' }: { className?: string }) {
  return (
    <span className={`flex items-center gap-2 ${className}`}>
      <svg viewBox="0 0 32 32" className="h-7 w-7" aria-hidden="true">
        <defs>
          <linearGradient id="logo-grad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#818cf8" />
            <stop offset="100%" stopColor="#22d3ee" />
          </linearGradient>
        </defs>
        <rect width="32" height="32" rx="8" fill="#111320" />
        <path d="M17.5 4 9 18h5.5L13 28l9-14.5h-5.5L17.5 4z" fill="url(#logo-grad)" />
      </svg>
      <span className="text-[17px] font-semibold tracking-tight text-slate-50">
        Volt<span className="text-volt-400">Mart</span>
      </span>
    </span>
  )
}

export default function Header() {
  const { itemCount } = useStore()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [mobileOpen, setMobileOpen] = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)

  // "/" focuses search, the way most storefronts and docs sites behave.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null
      const typing = target && /^(INPUT|TEXTAREA)$/.test(target.tagName)
      if (event.key === '/' && !typing) {
        event.preventDefault()
        searchRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  function submitSearch(event: React.FormEvent) {
    event.preventDefault()
    const trimmed = query.trim()
    navigate(trimmed ? `/catalog?q=${encodeURIComponent(trimmed)}` : '/catalog')
    setMobileOpen(false)
  }

  return (
    <header className="sticky top-0 z-40 border-b border-white/5 bg-ink-950/80 backdrop-blur-xl">
      <div className="mx-auto flex h-16 max-w-7xl items-center gap-4 px-4 sm:px-6 lg:px-8">
        <Link to="/" className="shrink-0" aria-label="VoltMart home">
          <Logo />
        </Link>

        <nav className="hidden items-center gap-1 lg:flex" aria-label="Main">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/catalog'}
              className={({ isActive }) =>
                `rounded-lg px-3 py-2 text-sm transition-colors ${
                  isActive ? 'text-white' : 'text-slate-400 hover:text-slate-100'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <form onSubmit={submitSearch} className="ml-auto hidden flex-1 max-w-sm sm:block" role="search">
          <div className="relative">
            <svg
              viewBox="0 0 20 20"
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 fill-slate-500"
              aria-hidden="true"
            >
              <path d="M8.5 2a6.5 6.5 0 014.98 10.68l3.92 3.92-1.42 1.42-3.92-3.92A6.5 6.5 0 118.5 2zm0 2a4.5 4.5 0 100 9 4.5 4.5 0 000-9z" />
            </svg>
            <input
              ref={searchRef}
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search products…"
              aria-label="Search products"
              className="input !py-2 pl-9 pr-10"
            />
            <kbd className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[10px] text-slate-500">
              /
            </kbd>
          </div>
        </form>

        <Link
          to="/cart"
          className="relative ml-auto shrink-0 rounded-xl border border-white/10 bg-white/[0.03] p-2.5 text-slate-300 transition-colors hover:border-white/20 hover:text-white sm:ml-0"
          aria-label={`Cart, ${itemCount} item${itemCount === 1 ? '' : 's'}`}
        >
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.7">
            <path d="M3 4h2l2.4 12.3a2 2 0 002 1.7h8.2a2 2 0 002-1.6L21 8H6" />
            <circle cx="10" cy="20" r="1.4" fill="currentColor" stroke="none" />
            <circle cx="18" cy="20" r="1.4" fill="currentColor" stroke="none" />
          </svg>
          {itemCount > 0 && (
            <span className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-volt-500 px-1 text-[11px] font-semibold text-white">
              {itemCount > 99 ? '99+' : itemCount}
            </span>
          )}
        </Link>

        <button
          type="button"
          onClick={() => setMobileOpen((open) => !open)}
          className="shrink-0 rounded-xl border border-white/10 bg-white/[0.03] p-2.5 text-slate-300 lg:hidden"
          aria-expanded={mobileOpen}
          aria-controls="mobile-nav"
          aria-label="Toggle navigation menu"
        >
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8">
            {mobileOpen ? <path d="M6 6l12 12M18 6L6 18" /> : <path d="M4 7h16M4 12h16M4 17h16" />}
          </svg>
        </button>
      </div>

      {mobileOpen && (
        <nav
          id="mobile-nav"
          className="animate-fade-up border-t border-white/5 px-4 py-3 lg:hidden"
          aria-label="Mobile"
        >
          <form onSubmit={submitSearch} className="mb-3 sm:hidden" role="search">
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search products…"
              aria-label="Search products"
              className="input"
            />
          </form>
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              onClick={() => setMobileOpen(false)}
              className="block rounded-lg px-3 py-2.5 text-sm text-slate-300 hover:bg-white/5 hover:text-white"
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      )}
    </header>
  )
}
