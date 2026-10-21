"""VoltMart support agent service.

Exposes the Strands agent over HTTP. The agent itself lives in
``agent_runtime``; this module handles the things a web service has to handle:
who is calling, how often they may call, where the conversation is stored, and
how a model failure is reported.

Identity
--------
The customer id arrives in the ``X-VoltMart-Customer-Id`` header, which the
frontend sets from its local session.

**This is a workshop stand-in, not authentication.** Anyone who can reach this
endpoint can claim any customer id, which means they can read that customer's
orders through the agent. It is acceptable here because the whole stack is a
disposable sample populated with fabricated data, and it is called out in
``docs/SECURITY.md`` with the two changes a real deployment needs: an
authenticating proxy in front of the service (ALB OIDC or Cognito), and
deriving ``customer_id`` from the verified token instead of a header.

Cost control
------------
Every call to this endpoint is a call to Amazon Bedrock, so it costs money and
is worth abusing. Two defences are in place: a per-identity token bucket, and a
bounded thread pool that caps how many model calls can be in flight at once.
Neither is a substitute for authentication; both limit the damage in a workshop
account.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any

from fastapi import Header, Request
from pydantic import BaseModel, Field

from agent_runtime import (
    MODEL_ID,
    AgentRunner,
    configure_strands_telemetry,
    last_turn_error,
    model_available,
    model_error,
    record_turn_error,
)
from gateway import ServiceGateway
from tools import TOOL_CATALOG, CustomerSession
from voltmart_common.config import downstream_config, storage_config
from voltmart_common.errors import (
    DownstreamUnavailable,
    Forbidden,
    NotFound,
    ValidationFailed,
    VoltMartError,
)
from voltmart_common.ids import is_safe_id, new_conversation_id
from voltmart_common.metrics import record_agent_conversation
from voltmart_common.service import create_app, run
from voltmart_common.storage import Repository
from voltmart_common.telemetry import current_trace_id, tracer

SERVICE_NAME = "voltmart-support-agent"

MAX_MESSAGE_LENGTH = int(os.getenv("AGENT_MAX_MESSAGE_LENGTH", "2000"))
MAX_HISTORY_TURNS = int(os.getenv("AGENT_MAX_HISTORY_TURNS", "20"))
CONVERSATION_TTL_SECONDS = int(os.getenv("AGENT_CONVERSATION_TTL", str(60 * 60 * 24)))

# Rate limit per identity. Generous enough for a demo conversation, tight
# enough that a loop cannot run up a bill unattended.
RATE_LIMIT_REQUESTS = int(os.getenv("AGENT_RATE_LIMIT_REQUESTS", "20"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("AGENT_RATE_LIMIT_WINDOW", "60"))

# Caps concurrent Bedrock calls. The agent loop is synchronous and blocking, so
# it runs here rather than on the event loop.
MAX_CONCURRENT_TURNS = int(os.getenv("AGENT_MAX_CONCURRENT_TURNS", "8"))


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)
    conversation_id: str = Field("", max_length=64)
    cart_id: str = Field("", max_length=64)


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    tools_used: list[str]
    usage: dict[str, int]
    model_id: str
    trace_id: str | None = None
    duration_ms: int


class ConversationSummary(BaseModel):
    conversation_id: str
    customer_id: str
    turn_count: int
    created_at: float
    updated_at: float


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------
class SlidingWindowLimiter:
    """In-process sliding window limiter.

    Per-replica, so the effective cluster limit is this times the replica
    count. Documented rather than solved: a shared limiter needs Redis or
    DynamoDB, which is more moving parts than a workshop needs. The thread pool
    bound below is the backstop that actually protects Bedrock.
    """

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = int(self.window - (now - hits[0])) + 1
                return False, retry_after
            hits.append(now)

            # Opportunistic cleanup so the dict does not grow without bound.
            if len(self._hits) > 10_000:
                stale = [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]
                for k in stale:
                    self._hits.pop(k, None)

            return True, 0


class RateLimited(VoltMartError):
    status_code = 429
    code = "rate_limited"


LIMITER = SlidingWindowLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
STORAGE = storage_config()
REPO = Repository(STORAGE, key_name="conversation_id")
GATEWAY = ServiceGateway(downstream_config())
RUNNER = AgentRunner(GATEWAY)
EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_TURNS, thread_name_prefix="agent-turn"
)


def _readiness() -> bool:
    # Storage must work; the model is checked separately so a Bedrock access
    # problem is visible in /ready rather than as a stream of 500s.
    return REPO.health() and model_available()


async def _startup() -> None:
    configure_strands_telemetry()


async def _shutdown() -> None:
    EXECUTOR.shutdown(wait=True, cancel_futures=False)
    GATEWAY.close()


app, log = create_app(
    SERVICE_NAME,
    title="VoltMart Support Agent",
    description="Strands-powered support assistant with tools for every VoltMart service.",
    readiness_check=_readiness,
    on_startup=_startup,
    on_shutdown=_shutdown,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _identify(customer_id: str | None, request: Request) -> str:
    """Resolve the caller's customer id, or empty string for anonymous.

    An anonymous caller still gets product help; the tools that touch customer
    data refuse on their own.
    """
    if not customer_id:
        return ""
    customer_id = customer_id.strip()
    if not is_safe_id(customer_id):
        raise ValidationFailed("X-VoltMart-Customer-Id is not a valid identifier")
    return customer_id


def _rate_limit_key(customer_id: str, request: Request) -> str:
    if customer_id:
        return f"customer:{customer_id}"
    # Anonymous callers are bucketed by client address. Behind the ALB this is
    # the X-Forwarded-For left-most hop, which uvicorn resolves via
    # proxy_headers.
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}"


def _load_conversation(conversation_id: str, customer_id: str) -> dict[str, Any]:
    """Fetch a conversation, enforcing that it belongs to this caller."""
    if not is_safe_id(conversation_id):
        raise ValidationFailed("conversation_id is not a valid identifier")

    item = REPO.get(conversation_id)
    if item is None:
        raise NotFound("That conversation has expired or does not exist.")

    # Without this check, a conversation id would be enough to read another
    # customer's support history.
    if item.get("customer_id", "") != customer_id:
        log.warning(
            "blocked cross-customer conversation access conversation_id=%s",
            conversation_id,
        )
        raise Forbidden("That conversation does not belong to this customer.")
    return item


def _trim(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the most recent turns so the prompt stays bounded.

    Each turn can be several messages once tool calls and results are counted,
    so the cap is applied generously in message terms.
    """
    cap = MAX_HISTORY_TURNS * 4
    return messages[-cap:] if len(messages) > cap else messages


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.post("/chat", response_model=ChatResponse, tags=["agent"])
async def chat(
    request: ChatRequest,
    http_request: Request,
    x_voltmart_customer_id: str | None = Header(None),
) -> ChatResponse:
    """Send a message to Volt, the support assistant.

    Supply ``conversation_id`` from a previous response to continue a
    conversation; omit it to start a new one.
    """
    started = time.perf_counter()
    customer_id = _identify(x_voltmart_customer_id, http_request)

    allowed, retry_after = LIMITER.check(_rate_limit_key(customer_id, http_request))
    if not allowed:
        record_agent_conversation("rate_limited")
        raise RateLimited(
            "That is a lot of messages at once. Please wait a moment and try again.",
            retry_after_seconds=retry_after,
        )

    with tracer(SERVICE_NAME).start_as_current_span("agent.turn") as span:
        span.set_attribute("voltmart.agent.model_id", MODEL_ID)
        span.set_attribute("voltmart.agent.identified", bool(customer_id))
        span.set_attribute("voltmart.agent.message_length", len(request.message))
        # The customer's message is deliberately not recorded as a span
        # attribute: support conversations contain personal detail, and traces
        # are not the place for it.

        if request.conversation_id:
            conversation = _load_conversation(request.conversation_id, customer_id)
            history = _trim(conversation.get("messages", []))
            conversation_id = request.conversation_id
        else:
            conversation_id = new_conversation_id()
            history = []
            conversation = {
                "conversation_id": conversation_id,
                "customer_id": customer_id,
                "messages": [],
                "created_at": time.time(),
            }

        span.set_attribute("voltmart.agent.conversation_id", conversation_id)
        span.set_attribute("voltmart.agent.history_length", len(history))

        session = CustomerSession(
            customer_id=customer_id,
            cart_id=request.cart_id,
            conversation_id=conversation_id,
        )

        loop = asyncio.get_running_loop()
        try:
            # The Strands agent loop is synchronous and makes blocking network
            # calls, so it runs in the bounded pool rather than on the event
            # loop. run_in_executor propagates the current context, which is
            # what keeps the trace connected across the thread boundary.
            result = await loop.run_in_executor(
                EXECUTOR, RUNNER.run_turn, session, request.message, history
            )
        except Exception as exc:
            span.record_exception(exc)
            record_agent_conversation("error")
            # Classify it once, log the operator-facing diagnosis, and keep it
            # for /agent/diagnostics. The customer still gets a neutral
            # message: Bedrock errors can contain account and model detail that
            # does not belong in a browser response.
            diagnosis = record_turn_error(exc)
            log.error(
                "agent turn failed conversation_id=%s diagnosis=%s",
                conversation_id,
                diagnosis,
                exc_info=True,
            )
            span.set_attribute("voltmart.agent.failure_diagnosis", diagnosis)
            raise DownstreamUnavailable(
                "The assistant is unavailable right now. Please try again in a moment.",
                hint="See GET /agent/diagnostics on this service for the cause.",
            ) from exc

        conversation["messages"] = _trim(result["messages"])
        conversation["updated_at"] = time.time()
        conversation["expires_at"] = int(time.time() + CONVERSATION_TTL_SECONDS)
        REPO.put(conversation)

        tools_used = result["tools_used"]
        span.set_attribute("voltmart.agent.tools_used", ",".join(tools_used) or "none")
        span.set_attribute("voltmart.agent.tool_count", len(tools_used))
        span.set_attribute("voltmart.agent.input_tokens", result["usage"]["input_tokens"])
        span.set_attribute("voltmart.agent.output_tokens", result["usage"]["output_tokens"])

    duration = time.perf_counter() - started
    record_agent_conversation("success")
    log.info(
        "agent turn conversation_id=%s tools=%s duration_ms=%d tokens_in=%d tokens_out=%d",
        conversation_id,
        ",".join(tools_used) or "-",
        int(duration * 1000),
        result["usage"]["input_tokens"],
        result["usage"]["output_tokens"],
    )

    return ChatResponse(
        conversation_id=conversation_id,
        reply=result["reply"],
        tools_used=tools_used,
        usage=result["usage"],
        model_id=result["model_id"],
        trace_id=current_trace_id(),
        duration_ms=int(duration * 1000),
    )


@app.get(
    "/conversations/{conversation_id}",
    response_model=ConversationSummary,
    tags=["agent"],
)
async def get_conversation(
    conversation_id: str, x_voltmart_customer_id: str | None = Header(None)
) -> ConversationSummary:
    """Metadata about a conversation.

    Returns counts and timestamps, not the transcript. The transcript lives in
    the browser that produced it; serving it back from here would make the
    conversation id a credential for reading a support history.
    """
    customer_id = (x_voltmart_customer_id or "").strip()
    item = _load_conversation(conversation_id, customer_id)
    return ConversationSummary(
        conversation_id=conversation_id,
        customer_id=item.get("customer_id", ""),
        turn_count=sum(
            1 for message in item.get("messages", []) if message.get("role") == "user"
        ),
        created_at=float(item.get("created_at", 0.0)),
        updated_at=float(item.get("updated_at", 0.0)),
    )


@app.delete("/conversations/{conversation_id}", status_code=204, tags=["agent"])
async def delete_conversation(
    conversation_id: str, x_voltmart_customer_id: str | None = Header(None)
) -> None:
    """Delete a conversation and its stored history."""
    customer_id = (x_voltmart_customer_id or "").strip()
    _load_conversation(conversation_id, customer_id)
    REPO.delete(conversation_id)
    log.info("conversation deleted conversation_id=%s", conversation_id)


@app.get("/tools", tags=["agent"])
async def list_tools() -> dict[str, Any]:
    """The tools the agent has, and which service each one calls.

    Exposed so the workshop UI can show the agent's capabilities, and so an
    attendee can see at a glance which tools change state.
    """
    return {
        "model_id": MODEL_ID,
        "tool_count": len(TOOL_CATALOG),
        "tools": TOOL_CATALOG,
        "note": (
            "Identity is bound per request. Tools that read or change customer "
            "data are scoped to the signed-in customer and cannot be redirected "
            "by the model."
        ),
    }


@app.get("/agent/diagnostics", tags=["agent"])
async def diagnostics() -> dict[str, Any]:
    """Why the agent is or is not working. No secrets, no customer data."""
    available = model_available()
    return {
        "model_id": MODEL_ID,
        "model_region": os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION", "unset"),
        "model_client_constructed": available,
        "model_client_error": model_error() if not available else "",
        # The useful field. A Bedrock client constructs fine even when the
        # model id is retired or access is not enabled; the failure only shows
        # up on the first real invocation, which is what this records.
        "last_turn_error": last_turn_error() or "none since start-up",
        "rate_limit": {
            "requests": RATE_LIMIT_REQUESTS,
            "window_seconds": RATE_LIMIT_WINDOW_SECONDS,
            "scope": "per replica",
        },
        "max_concurrent_turns": MAX_CONCURRENT_TURNS,
        "conversation_ttl_seconds": CONVERSATION_TTL_SECONDS,
        "storage_backend": STORAGE.backend,
        "identity_source": "X-VoltMart-Customer-Id header (workshop stand-in, not authentication)",
    }


if __name__ == "__main__":
    run("main:app")
