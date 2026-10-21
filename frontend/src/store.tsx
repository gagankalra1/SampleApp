/**
 * Session and cart state for the storefront.
 *
 * There is no sign-in. On first visit the app generates a random customer id
 * and cart id and keeps them in localStorage, which is enough to make orders,
 * carts and agent conversations behave like they belong to a person.
 *
 * This is a demo identity, not authentication. The customer id is sent to the
 * support agent in a header and the backend trusts it. A real deployment puts
 * an authenticating proxy in front and derives the identity from a verified
 * token; see docs/SECURITY.md. Nothing secret is ever stored here, which is
 * why localStorage is acceptable: a random id for fabricated data is not a
 * credential worth protecting.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { ApiError, cartApi, type Cart } from './api'

const CUSTOMER_KEY = 'voltmart.customer_id'
const CART_KEY = 'voltmart.cart_id'

function randomId(prefix: string): string {
  const bytes = new Uint8Array(12)
  crypto.getRandomValues(bytes)
  const body = Array.from(bytes, (b) => b.toString(36).padStart(2, '0')).join('').slice(0, 16)
  return `${prefix}_${body}`
}

function readOrCreateCustomerId(): string {
  try {
    const existing = localStorage.getItem(CUSTOMER_KEY)
    if (existing) return existing
    const created = randomId('cust')
    localStorage.setItem(CUSTOMER_KEY, created)
    return created
  } catch {
    // Private browsing with storage disabled: fall back to a per-tab id so the
    // app still works, it just will not remember orders across a reload.
    return randomId('cust')
  }
}

interface StoreValue {
  customerId: string
  cartId: string
  cart: Cart | null
  itemCount: number
  loading: boolean
  error: string | null
  addItem: (productId: string, quantity?: number) => Promise<void>
  setQuantity: (productId: string, quantity: number) => Promise<void>
  removeItem: (productId: string) => Promise<void>
  refresh: () => Promise<void>
  resetCart: () => Promise<void>
  lastAdded: string | null
}

const StoreContext = createContext<StoreValue | null>(null)

export function StoreProvider({ children }: { children: ReactNode }) {
  const [customerId] = useState(readOrCreateCustomerId)
  const [cartId, setCartId] = useState<string>(() => {
    try {
      return localStorage.getItem(CART_KEY) ?? ''
    } catch {
      return ''
    }
  })
  const [cart, setCart] = useState<Cart | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastAdded, setLastAdded] = useState<string | null>(null)
  const initialised = useRef(false)

  const persistCartId = useCallback((id: string) => {
    setCartId(id)
    try {
      localStorage.setItem(CART_KEY, id)
    } catch {
      /* storage unavailable; the id still lives in memory for this tab */
    }
  }, [])

  /** Resolve a usable cart id, creating a cart if the stored one is gone. */
  const ensureCart = useCallback(async (): Promise<string> => {
    if (cartId) {
      try {
        const existing = await cartApi.get(cartId)
        setCart(existing)
        return cartId
      } catch (err) {
        // A 404 means the cart expired (they have a 30 day TTL) or the
        // workshop stack was redeployed with a fresh table. Making a new one
        // is the right recovery; anything else is a real failure.
        if (!(err instanceof ApiError) || err.status !== 404) throw err
      }
    }
    const created = await cartApi.create(customerId)
    persistCartId(created.cart_id)
    setCart(created)
    return created.cart_id
  }, [cartId, customerId, persistCartId])

  const refresh = useCallback(async () => {
    setError(null)
    try {
      const id = await ensureCart()
      setCart(await cartApi.get(id))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load your cart.')
    }
  }, [ensureCart])

  useEffect(() => {
    if (initialised.current) return
    initialised.current = true
    void (async () => {
      await refresh()
      setLoading(false)
    })()
  }, [refresh])

  const addItem = useCallback(
    async (productId: string, quantity = 1) => {
      setError(null)
      try {
        const id = await ensureCart()
        setCart(await cartApi.addItem(id, productId, quantity))
        setLastAdded(productId)
        window.setTimeout(() => setLastAdded(null), 2200)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not add that to your cart.')
        throw err
      }
    },
    [ensureCart],
  )

  const setQuantity = useCallback(
    async (productId: string, quantity: number) => {
      setError(null)
      if (!cartId) return
      try {
        setCart(await cartApi.setQuantity(cartId, productId, quantity))
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not update that item.')
      }
    },
    [cartId],
  )

  const removeItem = useCallback(
    async (productId: string) => {
      setError(null)
      if (!cartId) return
      try {
        setCart(await cartApi.removeItem(cartId, productId))
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not remove that item.')
      }
    },
    [cartId],
  )

  /** Called after a successful checkout: the server already emptied the cart. */
  const resetCart = useCallback(async () => {
    if (!cartId) return
    try {
      setCart(await cartApi.get(cartId))
    } catch {
      setCart(null)
    }
  }, [cartId])

  const value = useMemo<StoreValue>(
    () => ({
      customerId,
      cartId,
      cart,
      itemCount: cart?.totals.item_count ?? 0,
      loading,
      error,
      addItem,
      setQuantity,
      removeItem,
      refresh,
      resetCart,
      lastAdded,
    }),
    [customerId, cartId, cart, loading, error, addItem, setQuantity, removeItem, refresh, resetCart, lastAdded],
  )

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>
}

export function useStore(): StoreValue {
  const value = useContext(StoreContext)
  if (!value) throw new Error('useStore must be used inside a StoreProvider')
  return value
}
