import { Suspense, lazy } from 'react'
import { Link, Route, Routes } from 'react-router-dom'
import ChatWidget from './components/ChatWidget'
import Header, { Logo } from './components/Header'
import { Skeleton } from './components/Bits'

// Code-split the routes a visitor does not hit on arrival. The home page and
// catalog are the common entry points; checkout and order history load on
// demand.
const Home = lazy(() => import('./pages/Home'))
const Catalog = lazy(() => import('./pages/Catalog'))
const ProductDetail = lazy(() => import('./pages/ProductDetail'))
const CartPage = lazy(() => import('./pages/CartPage'))
const CheckoutPage = lazy(() => import('./pages/CheckoutPage'))
const OrderConfirmation = lazy(() => import('./pages/OrderConfirmation'))
const OrdersPage = lazy(() => import('./pages/OrdersPage'))

function PageFallback() {
  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-10 sm:px-6 lg:px-8">
      <Skeleton className="h-8 w-64" />
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-64" />
        ))}
      </div>
    </div>
  )
}

function NotFound() {
  return (
    <div className="mx-auto max-w-7xl px-4 py-24 text-center sm:px-6 lg:px-8">
      <p className="text-sm font-semibold uppercase tracking-[0.14em] text-volt-400">404</p>
      <h1 className="mt-3 text-3xl font-semibold tracking-tight text-slate-50">
        That page is not plugged in
      </h1>
      <p className="mx-auto mt-3 max-w-md text-sm text-slate-400">
        The link may be out of date. Try the shop, or ask Volt in the corner.
      </p>
      <Link to="/catalog" className="btn-primary mt-7">
        Browse the shop
      </Link>
    </div>
  )
}

function Footer() {
  return (
    <footer className="mt-20 border-t border-white/5 bg-ink-950/60">
      <div className="mx-auto max-w-7xl px-4 py-10 sm:px-6 lg:px-8">
        <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
          <div className="max-w-sm">
            <Logo />
            <p className="mt-3 text-xs leading-relaxed text-slate-500">
              A sample application: microservices on Amazon EKS, a Strands-powered support
              agent, and OpenTelemetry traces across the whole purchase path.
            </p>
          </div>
          <div className="flex gap-10 text-xs">
            <div>
              <p className="mb-2.5 font-semibold uppercase tracking-wider text-slate-400">Shop</p>
              <ul className="space-y-1.5 text-slate-500">
                <li><Link to="/catalog?category=laptops" className="hover:text-slate-300">Laptops</Link></li>
                <li><Link to="/catalog?category=smartphones" className="hover:text-slate-300">Smartphones</Link></li>
                <li><Link to="/catalog?category=audio" className="hover:text-slate-300">Audio</Link></li>
                <li><Link to="/catalog?category=cameras" className="hover:text-slate-300">Cameras</Link></li>
              </ul>
            </div>
            <div>
              <p className="mb-2.5 font-semibold uppercase tracking-wider text-slate-400">Account</p>
              <ul className="space-y-1.5 text-slate-500">
                <li><Link to="/orders" className="hover:text-slate-300">Your orders</Link></li>
                <li><Link to="/cart" className="hover:text-slate-300">Your cart</Link></li>
              </ul>
            </div>
          </div>
        </div>
        <p className="mt-8 border-t border-white/5 pt-6 text-[11px] text-slate-600">
          VoltMart is a demonstration storefront. Products, prices, stock and payments are
          fabricated, and no real card is ever accepted or charged.
        </p>
      </div>
    </footer>
  )
}

export default function App() {
  return (
    <div className="flex min-h-screen flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-volt-600 focus:px-4 focus:py-2 focus:text-sm focus:text-white"
      >
        Skip to content
      </a>

      <Header />

      <main id="main" className="flex-1">
        <Suspense fallback={<PageFallback />}>
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/catalog" element={<Catalog />} />
            <Route path="/product/:productId" element={<ProductDetail />} />
            <Route path="/cart" element={<CartPage />} />
            <Route path="/checkout" element={<CheckoutPage />} />
            <Route path="/order/:orderId" element={<OrderConfirmation />} />
            <Route path="/orders" element={<OrdersPage />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </Suspense>
      </main>

      <Footer />
      <ChatWidget />
    </div>
  )
}
