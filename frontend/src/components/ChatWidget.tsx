/**
 * The Volt support assistant panel.
 *
 * Deliberately shows its work: every reply lists which tools the agent called
 * and the trace id for the turn. In a workshop that is the point — you ask
 * "where is my order", watch the badge say `list_my_orders, track_shipment`,
 * then open that trace and see the agent's own spans sitting above the HTTP
 * calls those tools made.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { agentApi, ApiError, type ChatResponse } from '../api'
import { useStore } from '../store'

interface Turn {
  id: string
  role: 'user' | 'assistant'
  text: string
  tools?: string[]
  traceId?: string | null
  durationMs?: number
  failed?: boolean
}

/**
 * Renders `**bold**` as real emphasis and leaves everything else as text.
 *
 * The system prompt asks the model for plain prose, but models drift and a
 * reply full of literal asterisks looks broken. This handles the one piece of
 * markup that actually shows up in practice.
 *
 * It builds React elements rather than setting innerHTML, so there is no path
 * from model output to executed HTML — which is the whole reason not to reach
 * for a markdown renderer here.
 */
function renderMessage(text: string) {
  // Strip markup that has no meaning in a chat bubble before splitting.
  const cleaned = text
    .replace(/^#{1,6}\s+/gm, '')     // headings
    .replace(/^\s*[-*]\s+/gm, '• ')  // list bullets
    .replace(/`{1,3}/g, '')          // code fences and inline ticks

  return cleaned.split(/(\*\*[^*]+\*\*)/g).map((part, index) =>
    part.startsWith('**') && part.endsWith('**') && part.length > 4 ? (
      <strong key={index} className="font-semibold text-white">
        {part.slice(2, -2)}
      </strong>
    ) : (
      part
    ),
  )
}

const SUGGESTIONS = [
  'What are your best noise cancelling headphones?',
  'Where is my most recent order?',
  "What's in my cart?",
  'I need a laptop under $1500 for video editing',
]

const GREETING: Turn = {
  id: 'greeting',
  role: 'assistant',
  text:
    "Hi, I'm Volt. I can look up products, check stock, see what's in your cart, " +
    'and track or cancel an order. What do you need?',
}

export default function ChatWidget() {
  const { customerId, cartId } = useStore()
  const [open, setOpen] = useState(false)
  const [turns, setTurns] = useState<Turn[]>([GREETING])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [conversationId, setConversationId] = useState('')

  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Keep the newest message in view as the conversation grows.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns, sending])

  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  // Escape closes the panel and returns focus to the button that opened it.
  useEffect(() => {
    if (!open) return
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open])

  useEffect(() => () => abortRef.current?.abort(), [])

  const send = useCallback(
    async (message: string) => {
      const text = message.trim()
      if (!text || sending) return

      const userTurn: Turn = { id: `u-${Date.now()}`, role: 'user', text }
      setTurns((previous) => [...previous, userTurn])
      setDraft('')
      setSending(true)

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      try {
        const response: ChatResponse = await agentApi.chat(text, {
          conversationId: conversationId || undefined,
          cartId: cartId || undefined,
          customerId,
          signal: controller.signal,
        })
        setConversationId(response.conversation_id)
        setTurns((previous) => [
          ...previous,
          {
            id: `a-${Date.now()}`,
            role: 'assistant',
            text: response.reply,
            tools: response.tools_used,
            traceId: response.trace_id,
            durationMs: response.duration_ms,
          },
        ])
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        const apiError = error instanceof ApiError ? error : null
        setTurns((previous) => [
          ...previous,
          {
            id: `e-${Date.now()}`,
            role: 'assistant',
            failed: true,
            traceId: apiError?.traceId ?? null,
            text:
              apiError?.status === 429
                ? "That's a lot of questions at once. Give me a few seconds and try again."
                : apiError?.message ??
                  'I could not reach the assistant. It may still be starting up.',
          },
        ])
      } finally {
        setSending(false)
        inputRef.current?.focus()
      }
    },
    [cartId, conversationId, customerId, sending],
  )

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter makes a newline.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void send(draft)
    }
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="fixed bottom-5 right-5 z-50 flex items-center gap-2.5 rounded-2xl bg-volt-600 px-4 py-3.5 text-sm font-medium text-white shadow-glow transition-all hover:bg-volt-500 active:scale-95"
        aria-expanded={open}
        aria-controls="volt-assistant"
      >
        <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8">
          {open ? (
            <path d="M6 6l12 12M18 6L6 18" />
          ) : (
            <path d="M21 11.5a8.4 8.4 0 01-8.5 8.4 8.8 8.8 0 01-3.8-.85L3 21l1.95-5.1A8.4 8.4 0 0112.5 3 8.4 8.4 0 0121 11.5z" />
          )}
        </svg>
        <span className="hidden sm:inline">{open ? 'Close' : 'Ask Volt'}</span>
      </button>

      {open && (
        <div
          id="volt-assistant"
          ref={panelRef}
          role="dialog"
          aria-modal="false"
          aria-label="Volt support assistant"
          className="fixed bottom-24 right-5 z-50 flex h-[min(34rem,calc(100vh-8rem))] w-[min(26rem,calc(100vw-2.5rem))] animate-slide-up flex-col overflow-hidden rounded-2xl border border-white/10 bg-ink-900/95 shadow-2xl backdrop-blur-xl"
        >
          <div className="flex items-center gap-3 border-b border-white/5 px-4 py-3">
            <span className="relative flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-volt-500 to-spark-500">
              <svg viewBox="0 0 32 32" className="h-5 w-5 fill-white" aria-hidden="true">
                <path d="M17.5 4 9 18h5.5L13 28l9-14.5h-5.5L17.5 4z" />
              </svg>
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-slate-50">Volt Assistant</p>
              <p className="flex items-center gap-1.5 text-[11px] text-slate-400">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                Connected to your orders and cart
              </p>
            </div>
          </div>

          <div ref={scrollRef} className="scroll-slim flex-1 space-y-3 overflow-y-auto px-4 py-4">
            {turns.map((turn) => (
              <div
                key={turn.id}
                className={`flex ${turn.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div className={`max-w-[85%] space-y-1.5 ${turn.role === 'user' ? 'items-end' : ''}`}>
                  <div
                    className={`rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed ${
                      turn.role === 'user'
                        ? 'bg-volt-600 text-white'
                        : turn.failed
                          ? 'border border-rose-500/20 bg-rose-500/10 text-rose-100'
                          : 'border border-white/5 bg-ink-800 text-slate-200'
                    }`}
                  >
                    {/* renderMessage builds React elements, never HTML, so
                        model output can never become executable markup. */}
                    <p className="whitespace-pre-wrap">
                      {turn.role === 'assistant' ? renderMessage(turn.text) : turn.text}
                    </p>
                  </div>

                  {turn.tools && turn.tools.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1">
                      {turn.tools.map((tool) => (
                        <span
                          key={tool}
                          className="pill bg-spark-500/10 font-mono text-[10px] text-spark-400 ring-1 ring-spark-500/20"
                          title={`The agent called the ${tool} tool`}
                        >
                          {tool}
                        </span>
                      ))}
                      {turn.durationMs !== undefined && (
                        <span className="text-[10px] text-slate-500">
                          {(turn.durationMs / 1000).toFixed(1)}s
                        </span>
                      )}
                    </div>
                  )}

                  {turn.traceId && (
                    <p className="font-mono text-[10px] text-slate-600" title="OpenTelemetry trace id">
                      trace {turn.traceId.slice(0, 16)}…
                    </p>
                  )}
                </div>
              </div>
            ))}

            {sending && (
              <div className="flex justify-start">
                <div className="flex items-center gap-1.5 rounded-2xl border border-white/5 bg-ink-800 px-4 py-3">
                  {[0, 150, 300].map((delay) => (
                    <span
                      key={delay}
                      className="h-1.5 w-1.5 animate-bounce rounded-full bg-volt-400"
                      style={{ animationDelay: `${delay}ms` }}
                    />
                  ))}
                  <span className="sr-only">Volt is thinking</span>
                </div>
              </div>
            )}
          </div>

          {turns.length === 1 && (
            <div className="flex flex-wrap gap-1.5 border-t border-white/5 px-4 py-3">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => void send(suggestion)}
                  className="rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-1.5 text-left text-[11px] text-slate-300 transition-colors hover:border-volt-500/40 hover:text-white"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          )}

          <form
            onSubmit={(event) => {
              event.preventDefault()
              void send(draft)
            }}
            className="border-t border-white/5 p-3"
          >
            <div className="flex items-end gap-2">
              <textarea
                ref={inputRef}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={onKeyDown}
                rows={1}
                maxLength={2000}
                placeholder="Ask about products or your order…"
                aria-label="Message to the Volt assistant"
                className="input max-h-28 min-h-[2.75rem] flex-1 resize-none !py-2.5"
              />
              <button
                type="submit"
                disabled={sending || !draft.trim()}
                className="btn-primary shrink-0 !px-3 !py-2.5"
                aria-label="Send message"
              >
                <svg viewBox="0 0 24 24" className="h-4.5 w-4.5" fill="none" stroke="currentColor" strokeWidth="1.8">
                  <path d="M4 12l16-8-6 8 6 8-16-8z" />
                </svg>
              </button>
            </div>
            <p className="mt-2 text-[10px] leading-relaxed text-slate-600">
              Volt can read and change your cart and orders. Demo data only.
            </p>
          </form>
        </div>
      )}
    </>
  )
}
