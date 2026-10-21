/**
 * Typed client for the VoltMart services.
 *
 * Every request goes to a relative `/api/...` path on the same origin. nginx
 * (in production) and the Vite dev server (locally) map those paths to the
 * individual services. Same-origin means no CORS configuration, no API base
 * URL baked into the bundle at build time, and no credentials in client code.
 *
 * Note which services are *not* reachable from here: the payment service has
 * no proxy route. The browser never talks to it; only the checkout service
 * does, from inside the cluster.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
export interface ProductSummary {
  product_id: string
  name: string
  brand: string
  category: string
  price: number
  currency: string
  rating: number
  review_count: number
  short_description: string
  tags: string[]
  image_accent: string
  featured: boolean
}

export interface Product extends ProductSummary {
  description: string
  specs: Record<string, string>
}

export interface ProductPage {
  items: ProductSummary[]
  total: number
  limit: number
  offset: number
}

export interface CategoryInfo {
  category: string
  label: string
  product_count: number
  min_price: number
  max_price: number
}

export interface StockRecord {
  product_id: string
  available: number
  reserved: number
  sold: number
  status: 'in_stock' | 'low_stock' | 'out_of_stock'
  low_stock_threshold: number
}

export interface CartLine {
  product_id: string
  name: string
  brand: string
  category: string
  quantity: number
  unit_price: number
  line_total: number
  currency: string
  image_accent: string
  available: number | null
  stock_status: string | null
  exceeds_available: boolean
}

export interface CartTotals {
  subtotal: number
  tax: number
  shipping: number
  total: number
  currency: string
  item_count: number
  free_shipping_applied: boolean
  amount_to_free_shipping: number
}

export interface Cart {
  cart_id: string
  customer_id: string
  lines: CartLine[]
  totals: CartTotals
  created_at: number
  updated_at: number
  has_stock_warnings: boolean
}

export interface OrderLine {
  product_id: string
  name: string
  brand: string
  category: string
  quantity: number
  unit_price: number
  line_total: number
}

export interface StatusEvent {
  status: string
  at: number
  note: string
}

export interface Order {
  order_id: string
  customer_id: string
  email: string
  status: string
  lines: OrderLine[]
  totals: { subtotal: number; tax: number; shipping: number; total: number; currency: string }
  shipping_address: ShippingAddress
  shipping_method: string
  payment_id: string
  created_at: number
  updated_at: number
  estimated_delivery: string
  tracking_number: string
  cancellable: boolean
  history: StatusEvent[]
}

export interface ShippingAddress {
  full_name: string
  line1: string
  line2?: string
  city: string
  region: string
  postal_code: string
  country: string
}

export interface CheckoutResult {
  order_id: string
  payment_id: string
  status: string
  total: number
  currency: string
  estimated_delivery: string
  email: string
  trace_id: string | null
  steps_completed: string[]
  duration_ms: number
}

export interface CheckoutPreview {
  cart_id: string
  line_count: number
  totals: CartTotals
  can_checkout: boolean
  blockers: { product_id: string; name: string; requested: number; available: number; issue: string }[]
  reason: string
}

export interface ChatResponse {
  conversation_id: string
  reply: string
  tools_used: string[]
  usage: { input_tokens: number; output_tokens: number }
  model_id: string
  trace_id: string | null
  duration_ms: number
}

export interface AgentToolInfo {
  name: string
  service: string
  writes: boolean
}

// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

/**
 * An error carrying the structured fields the services return.
 *
 * `traceId` is the useful one: it is shown in the UI on failure so that an
 * attendee can paste it straight into X-Ray and see exactly which service
 * call failed.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string = '',
    readonly traceId: string | null = null,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

const CUSTOMER_HEADER = 'X-VoltMart-Customer-Id'

async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; customerId?: string; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = {}
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (options.customerId) headers[CUSTOMER_HEADER] = options.customerId

  let response: Response
  try {
    response = await fetch(path, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new ApiError('Could not reach VoltMart. Check your connection.', 0, 'network_error')
  }

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`
    let code = ''
    let traceId: string | null = null
    let details: Record<string, unknown> = {}
    try {
      const payload = await response.json()
      const error = payload?.error ?? {}
      message = error.message ?? message
      code = error.code ?? ''
      traceId = error.trace_id ?? null
      details = error.details ?? {}
    } catch {
      // A non-JSON error body means the request did not reach a VoltMart
      // service at all (an nginx or load balancer error page, say).
    }
    throw new ApiError(message, response.status, code, traceId, details)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

// ---------------------------------------------------------------------------
// Catalog
// ---------------------------------------------------------------------------
export interface ProductQuery {
  q?: string
  category?: string
  brand?: string
  minPrice?: number
  maxPrice?: number
  featured?: boolean
  sort?: 'relevance' | 'price_asc' | 'price_desc' | 'rating'
  limit?: number
  offset?: number
}

export const catalog = {
  list(query: ProductQuery = {}, signal?: AbortSignal): Promise<ProductPage> {
    const params = new URLSearchParams()
    if (query.q) params.set('q', query.q)
    if (query.category) params.set('category', query.category)
    if (query.brand) params.set('brand', query.brand)
    if (query.minPrice !== undefined) params.set('min_price', String(query.minPrice))
    if (query.maxPrice !== undefined) params.set('max_price', String(query.maxPrice))
    if (query.featured !== undefined) params.set('featured', String(query.featured))
    if (query.sort) params.set('sort', query.sort)
    params.set('limit', String(query.limit ?? 24))
    params.set('offset', String(query.offset ?? 0))
    return request<ProductPage>(`/api/catalog/products?${params}`, { signal })
  },

  get(productId: string, signal?: AbortSignal): Promise<Product> {
    return request<Product>(`/api/catalog/products/${encodeURIComponent(productId)}`, { signal })
  },

  categories(signal?: AbortSignal): Promise<CategoryInfo[]> {
    return request<CategoryInfo[]>('/api/catalog/categories', { signal })
  },
}

// ---------------------------------------------------------------------------
// Inventory
// ---------------------------------------------------------------------------
export const inventory = {
  get(productId: string, signal?: AbortSignal): Promise<StockRecord> {
    return request<StockRecord>(`/api/inventory/inventory/${encodeURIComponent(productId)}`, { signal })
  },
}

// ---------------------------------------------------------------------------
// Cart
// ---------------------------------------------------------------------------
export const cartApi = {
  create(customerId: string): Promise<Cart> {
    return request<Cart>('/api/cart/carts', { method: 'POST', body: { customer_id: customerId } })
  },

  get(cartId: string, signal?: AbortSignal): Promise<Cart> {
    return request<Cart>(`/api/cart/carts/${encodeURIComponent(cartId)}`, { signal })
  },

  addItem(cartId: string, productId: string, quantity = 1): Promise<Cart> {
    return request<Cart>(`/api/cart/carts/${encodeURIComponent(cartId)}/items`, {
      method: 'POST',
      body: { product_id: productId, quantity },
    })
  },

  setQuantity(cartId: string, productId: string, quantity: number): Promise<Cart> {
    return request<Cart>(
      `/api/cart/carts/${encodeURIComponent(cartId)}/items/${encodeURIComponent(productId)}`,
      { method: 'PUT', body: { quantity } },
    )
  },

  removeItem(cartId: string, productId: string): Promise<Cart> {
    return request<Cart>(
      `/api/cart/carts/${encodeURIComponent(cartId)}/items/${encodeURIComponent(productId)}`,
      { method: 'DELETE' },
    )
  },

  clear(cartId: string): Promise<Cart> {
    return request<Cart>(`/api/cart/carts/${encodeURIComponent(cartId)}`, { method: 'DELETE' })
  },
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------
export const ordersApi = {
  listForCustomer(customerId: string, limit = 25, signal?: AbortSignal): Promise<Order[]> {
    const params = new URLSearchParams({ customer_id: customerId, limit: String(limit) })
    return request<Order[]>(`/api/orders/orders?${params}`, { signal })
  },

  get(orderId: string, signal?: AbortSignal): Promise<Order> {
    return request<Order>(`/api/orders/orders/${encodeURIComponent(orderId)}`, { signal })
  },

  cancel(orderId: string, reason = 'customer_request'): Promise<Order> {
    return request<Order>(`/api/orders/orders/${encodeURIComponent(orderId)}/cancel`, {
      method: 'POST',
      body: { reason },
    })
  },
}

// ---------------------------------------------------------------------------
// Checkout
// ---------------------------------------------------------------------------
export interface CheckoutPayload {
  cart_id: string
  customer_id: string
  email: string
  shipping_address: ShippingAddress
  shipping_method: 'standard' | 'express' | 'overnight'
  payment_method: 'card' | 'wallet' | 'gift_card'
  card?: {
    number: string
    exp_month: number
    exp_year: number
    cvc: string
    holder_name: string
  }
}

export const checkoutApi = {
  preview(cartId: string, signal?: AbortSignal): Promise<CheckoutPreview> {
    return request<CheckoutPreview>(`/api/checkout/checkout/preview/${encodeURIComponent(cartId)}`, { signal })
  },

  submit(payload: CheckoutPayload): Promise<CheckoutResult> {
    return request<CheckoutResult>('/api/checkout/checkout', { method: 'POST', body: payload })
  },
}

// ---------------------------------------------------------------------------
// Support agent
// ---------------------------------------------------------------------------
export const agentApi = {
  chat(
    message: string,
    options: { conversationId?: string; cartId?: string; customerId: string; signal?: AbortSignal },
  ): Promise<ChatResponse> {
    return request<ChatResponse>('/api/support/chat', {
      method: 'POST',
      customerId: options.customerId,
      signal: options.signal,
      body: {
        message,
        conversation_id: options.conversationId ?? '',
        cart_id: options.cartId ?? '',
      },
    })
  },

  tools(signal?: AbortSignal): Promise<{ model_id: string; tool_count: number; tools: AgentToolInfo[] }> {
    return request('/api/support/tools', { signal })
  },
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------
const currencyFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 2,
})

export function money(amount: number): string {
  return currencyFormatter.format(amount)
}

export function formatDate(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

export function formatIsoDate(iso: string): string {
  if (!iso) return ''
  const parsed = new Date(`${iso}T12:00:00Z`)
  if (Number.isNaN(parsed.getTime())) return iso
  return parsed.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
}

export const ORDER_STATUS_LABELS: Record<string, string> = {
  pending_payment: 'Awaiting payment',
  confirmed: 'Confirmed',
  packed: 'Packed',
  shipped: 'Shipped',
  delivered: 'Delivered',
  cancelled: 'Cancelled',
  payment_failed: 'Payment failed',
}
