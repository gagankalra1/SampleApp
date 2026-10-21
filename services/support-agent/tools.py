"""The tools the VoltMart support agent can call.

Each tool is a thin, well-described wrapper over one microservice endpoint. The
model decides which to call; these functions decide what is allowed.

Authorization, and why the tools are built per request
------------------------------------------------------
The customer's identity is **not** a tool parameter. It is captured in a
closure when the tool set is built for a request, so the model physically
cannot pass a different ``customer_id``.

This matters more than it might look. A tool signed
``get_order_status(order_id, customer_id)`` lets a prompt-injected or merely
confused model read another customer's order by supplying an id it saw
somewhere. Binding identity outside the model's reach makes that class of bug
impossible rather than unlikely. Every tool that touches customer data also
re-checks ownership against the record it fetched, so a guessed order id
returns "not found" instead of someone else's address.

Error handling
--------------
Tools return a dict with ``ok: false`` and a readable message instead of
raising. A raised exception ends the agent turn; a returned error lets the
model say "that order has already shipped, so I can't cancel it, but here is
what I can do" — which is the behaviour a support conversation needs.
"""

from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from strands import tool

from gateway import ServiceError, ServiceGateway
from voltmart_common.metrics import record_agent_escalation, record_agent_tool_call
from voltmart_common.telemetry import tracer

LOG = logging.getLogger(__name__)

# Statuses from which the agent may issue a refund without a human. A refund on
# a delivered order is a returns question, and a refund on a shipped one means
# the goods are in transit; both need a person.
REFUNDABLE_ORDER_STATUSES = {"cancelled", "payment_failed"}


@dataclass
class CustomerSession:
    """Who the agent is acting for, established before the model is involved.

    ``customer_id`` comes from the application's session, never from the
    conversation. ``cart_id`` is optional: the agent can find a cart by
    customer if the frontend did not supply one.
    """

    customer_id: str
    cart_id: str = ""
    email: str = ""
    conversation_id: str = ""

    @property
    def is_identified(self) -> bool:
        return bool(self.customer_id)


def _instrument(name: str) -> Callable:
    """Wrap a tool in a span, a counter and a latency histogram.

    Every tool call becomes a span named ``agent.tool.<name>``, so a trace
    shows the agent's reasoning steps interleaved with the service calls each
    one triggered.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            with tracer("voltmart-support-agent").start_as_current_span(
                f"agent.tool.{name}"
            ) as span:
                span.set_attribute("voltmart.agent.tool", name)
                try:
                    result = func(*args, **kwargs)
                    ok = not (isinstance(result, dict) and result.get("ok") is False)
                    outcome = "success" if ok else "handled_error"
                    span.set_attribute("voltmart.agent.tool_outcome", outcome)
                    return result
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_attribute("voltmart.agent.tool_outcome", "exception")
                    LOG.exception("tool %s raised", name)
                    # Still return rather than propagate, so one broken tool
                    # does not end the customer's conversation.
                    return {
                        "ok": False,
                        "error": "tool_failure",
                        "message": (
                            "Something went wrong on our side running that lookup. "
                            "I can try a different approach or get a human to help."
                        ),
                    }
                finally:
                    duration = time.perf_counter() - started
                    record_agent_tool_call(name, "completed", duration)

        return wrapper

    return decorator


def _service_error(exc: ServiceError) -> dict[str, Any]:
    """Turn a downstream error into something the model can act on."""
    return {
        "ok": False,
        "error": exc.code or "service_error",
        "message": exc.message,
        "details": exc.details,
    }


def build_tools(session: CustomerSession, gateway: ServiceGateway) -> list[Callable]:
    """Build the tool set for one request, bound to one customer.

    Returns the list to hand to ``Agent(tools=...)``.
    """

    # ----------------------------------------------------------------------
    # Catalog
    # ----------------------------------------------------------------------
    @tool
    @_instrument("search_products")
    def search_products(
        query: str = "",
        category: str = "",
        max_price: float = 0.0,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search the VoltMart product catalog.

        Use this whenever the customer asks what VoltMart sells, asks for a
        recommendation, or describes a product without naming it exactly.

        Args:
            query: Free-text search, e.g. "noise cancelling headphones" or "4K TV".
            category: Optional category slug to narrow results. Valid slugs are
                laptops, smartphones, audio, wearables, televisions, cameras,
                gaming, smart-home, accessories.
            max_price: Optional upper price limit in USD. Use 0 for no limit.
            limit: How many products to return, 1 to 10.

        Returns:
            Matching products with prices, ratings and a one-line description.
        """
        params: dict[str, Any] = {"limit": max(1, min(limit, 10))}
        if query:
            params["q"] = query
        if category:
            params["category"] = category
        if max_price and max_price > 0:
            params["max_price"] = max_price

        try:
            page = gateway.search_products(**params)
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "total_matches": page["total"],
            "products": [
                {
                    "product_id": item["product_id"],
                    "name": item["name"],
                    "brand": item["brand"],
                    "category": item["category"],
                    "price_usd": item["price"],
                    "rating": item["rating"],
                    "review_count": item["review_count"],
                    "summary": item["short_description"],
                }
                for item in page["items"]
            ],
        }

    @tool
    @_instrument("get_product_details")
    def get_product_details(product_id: str) -> dict[str, Any]:
        """Get the full specification for one product.

        Use after search_products when the customer asks about specifics such as
        battery life, ports, screen size or materials.

        Args:
            product_id: The product id, e.g. "vm-lap-001".

        Returns:
            Full description, technical specifications, price and rating.
        """
        try:
            product = gateway.get_product(product_id)
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "product_id": product["product_id"],
            "name": product["name"],
            "brand": product["brand"],
            "category": product["category"],
            "price_usd": product["price"],
            "rating": product["rating"],
            "review_count": product["review_count"],
            "description": product["description"],
            "specifications": product["specs"],
            "tags": product["tags"],
        }

    @tool
    @_instrument("list_product_categories")
    def list_product_categories() -> dict[str, Any]:
        """List every product category with its product count and price range.

        Use when the customer asks what kinds of products VoltMart sells, or
        when you need a valid category slug for search_products.

        Returns:
            Categories with labels, counts and price ranges.
        """
        try:
            categories = gateway.list_categories()
        except ServiceError as exc:
            return _service_error(exc)
        return {"ok": True, "categories": categories}

    # ----------------------------------------------------------------------
    # Inventory
    # ----------------------------------------------------------------------
    @tool
    @_instrument("check_stock")
    def check_stock(product_id: str) -> dict[str, Any]:
        """Check whether a product is in stock and how many units are available.

        Use when the customer asks about availability, or before suggesting they
        add something to their cart.

        Args:
            product_id: The product id, e.g. "vm-gam-002".

        Returns:
            Availability status and the number of units that can be ordered.
        """
        try:
            stock = gateway.get_stock(product_id)
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "product_id": stock["product_id"],
            "available_units": stock["available"],
            "status": stock["status"],
            "in_stock": stock["available"] > 0,
            "note": (
                "Low stock, worth mentioning to the customer."
                if stock["status"] == "low_stock"
                else ""
            ),
        }

    # ----------------------------------------------------------------------
    # Cart
    # ----------------------------------------------------------------------
    def _resolve_cart_id() -> str | None:
        """Find this customer's cart, preferring the one the browser supplied."""
        if session.cart_id:
            return session.cart_id
        if not session.is_identified:
            return None
        try:
            cart = gateway.get_cart_for_customer(session.customer_id)
            return cart["cart_id"]
        except ServiceError:
            return None

    @tool
    @_instrument("view_cart")
    def view_cart() -> dict[str, Any]:
        """Show what is currently in this customer's shopping cart.

        Use when the customer asks about their cart, their total, or shipping
        cost. Takes no arguments: the cart is resolved from the signed-in
        customer automatically.

        Returns:
            Cart lines with quantities and prices, plus subtotal, tax, shipping
            and total.
        """
        cart_id = _resolve_cart_id()
        if cart_id is None:
            return {
                "ok": True,
                "cart_found": False,
                "message": "This customer does not have an active cart yet.",
            }

        try:
            cart = gateway.get_cart(cart_id)
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "cart_found": True,
            "items": [
                {
                    "product_id": line["product_id"],
                    "name": line["name"],
                    "quantity": line["quantity"],
                    "unit_price_usd": line["unit_price"],
                    "line_total_usd": line["line_total"],
                    "stock_warning": line.get("exceeds_available", False),
                }
                for line in cart["lines"]
            ],
            "totals": cart["totals"],
            "has_stock_warnings": cart.get("has_stock_warnings", False),
        }

    @tool
    @_instrument("add_to_cart")
    def add_to_cart(product_id: str, quantity: int = 1) -> dict[str, Any]:
        """Add a product to this customer's cart.

        Only call this when the customer has clearly asked for the item to be
        added. Do not add things speculatively while making recommendations.

        Args:
            product_id: The product id to add, e.g. "vm-aud-001".
            quantity: How many units, 1 to 20.

        Returns:
            The updated cart totals, or an explanation of why it could not be added.
        """
        if not session.is_identified:
            return {
                "ok": False,
                "error": "not_identified",
                "message": "I need the customer to be signed in before I can change their cart.",
            }
        if quantity < 1 or quantity > 20:
            return {
                "ok": False,
                "error": "invalid_quantity",
                "message": "Quantity has to be between 1 and 20.",
            }

        cart_id = _resolve_cart_id()
        try:
            if cart_id is None:
                cart_id = gateway.create_cart(session.customer_id)["cart_id"]
            cart = gateway.add_cart_item(cart_id, product_id, quantity)
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "added": {"product_id": product_id, "quantity": quantity},
            "cart_item_count": cart["totals"]["item_count"],
            "cart_total_usd": cart["totals"]["total"],
        }

    @tool
    @_instrument("remove_from_cart")
    def remove_from_cart(product_id: str) -> dict[str, Any]:
        """Remove a product from this customer's cart entirely.

        Args:
            product_id: The product id to remove.

        Returns:
            The updated cart totals, or an explanation of what went wrong.
        """
        if not session.is_identified:
            return {
                "ok": False,
                "error": "not_identified",
                "message": "I need the customer to be signed in before I can change their cart.",
            }

        cart_id = _resolve_cart_id()
        if cart_id is None:
            return {
                "ok": False,
                "error": "no_cart",
                "message": "This customer has no active cart, so there is nothing to remove.",
            }

        try:
            cart = gateway.remove_cart_item(cart_id, product_id)
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "removed": product_id,
            "cart_item_count": cart["totals"]["item_count"],
            "cart_total_usd": cart["totals"]["total"],
        }

    @tool
    @_instrument("check_checkout_readiness")
    def check_checkout_readiness() -> dict[str, Any]:
        """Check whether this customer's cart can be checked out right now.

        Use when the customer says checkout is failing or asks why they cannot
        complete their order. Reserves nothing and charges nothing.

        Returns:
            Whether checkout can proceed, and specific blockers if not.
        """
        cart_id = _resolve_cart_id()
        if cart_id is None:
            return {
                "ok": True,
                "can_checkout": False,
                "reason": "There is no active cart for this customer.",
            }

        try:
            preview = gateway.checkout_preview(cart_id)
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "can_checkout": preview["can_checkout"],
            "reason": preview.get("reason", ""),
            "blockers": preview.get("blockers", []),
            "totals": preview.get("totals", {}),
        }

    # ----------------------------------------------------------------------
    # Orders
    # ----------------------------------------------------------------------
    def _owned_order(order_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Fetch an order, but only if it belongs to this customer.

        An order owned by someone else returns the same "not found" as an id
        that does not exist. Saying "that order exists but is not yours" would
        confirm the id to whoever is probing.
        """
        try:
            order = gateway.get_order(order_id)
        except ServiceError as exc:
            return None, _service_error(exc)

        if order.get("customer_id") != session.customer_id:
            LOG.warning(
                "agent blocked cross-customer order access order_id=%s session_customer=%s",
                order_id,
                session.customer_id,
            )
            return None, {
                "ok": False,
                "error": "not_found",
                "message": (
                    f"I cannot find an order {order_id} on this account. "
                    "Could you double-check the order number?"
                ),
            }
        return order, None

    @tool
    @_instrument("list_my_orders")
    def list_my_orders(limit: int = 5) -> dict[str, Any]:
        """List this customer's recent orders, newest first.

        Use when the customer asks about "my orders", "my last order", or does
        not remember their order number. Takes no customer argument: it is
        always scoped to the signed-in customer.

        Args:
            limit: How many orders to return, 1 to 20.

        Returns:
            Recent orders with ids, statuses, totals and dates.
        """
        if not session.is_identified:
            return {
                "ok": False,
                "error": "not_identified",
                "message": "I need the customer to be signed in to look up their orders.",
            }

        try:
            orders = gateway.list_orders(session.customer_id, max(1, min(limit, 20)))
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "order_count": len(orders),
            "orders": [
                {
                    "order_id": order["order_id"],
                    "status": order["status"],
                    "total_usd": order["totals"]["total"],
                    "item_count": sum(line["quantity"] for line in order["lines"]),
                    "placed_at": order["created_at"],
                    "estimated_delivery": order["estimated_delivery"],
                    "cancellable": order["cancellable"],
                }
                for order in orders
            ],
        }

    @tool
    @_instrument("get_order_status")
    def get_order_status(order_id: str) -> dict[str, Any]:
        """Get the full detail and status history of one order.

        Use when the customer gives you an order number and asks what is
        happening with it.

        Args:
            order_id: The order number, e.g. "VM-260415-A1B2C3D4".

        Returns:
            Status, items, totals, delivery estimate and the status history.
        """
        if not session.is_identified:
            return {
                "ok": False,
                "error": "not_identified",
                "message": "I need the customer to be signed in to look up an order.",
            }

        order, error = _owned_order(order_id)
        if error is not None:
            return error
        assert order is not None

        return {
            "ok": True,
            "order_id": order["order_id"],
            "status": order["status"],
            "items": [
                {
                    "name": line["name"],
                    "quantity": line["quantity"],
                    "unit_price_usd": line["unit_price"],
                }
                for line in order["lines"]
            ],
            "totals": order["totals"],
            "placed_at": order["created_at"],
            "estimated_delivery": order["estimated_delivery"],
            "tracking_number": order["tracking_number"],
            "shipping_method": order["shipping_method"],
            "cancellable": order["cancellable"],
            "history": order["history"],
        }

    @tool
    @_instrument("track_shipment")
    def track_shipment(order_id: str) -> dict[str, Any]:
        """Get shipment tracking for an order.

        Use for "where is my parcel" and "when will it arrive" questions.

        Args:
            order_id: The order number.

        Returns:
            Tracking number, carrier, transit state and delivery estimate.
        """
        order, error = _owned_order(order_id)
        if error is not None:
            return error

        try:
            tracking = gateway.get_tracking(order_id)
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "order_id": order_id,
            "status": tracking["status"],
            "tracking_number": tracking["tracking_number"] or "not yet assigned",
            "carrier": tracking["carrier"] or "not yet assigned",
            "estimated_delivery": tracking["estimated_delivery"],
            "in_transit": tracking["in_transit"],
            "delivered": tracking["delivered"],
        }

    @tool
    @_instrument("cancel_order")
    def cancel_order(order_id: str, reason: str = "customer_request") -> dict[str, Any]:
        """Cancel an order that has not shipped yet.

        This is irreversible, so only call it when the customer has explicitly
        asked to cancel a specific order. Confirm the order number with them
        first if there is any ambiguity. Orders that have already shipped
        cannot be cancelled and need a return instead.

        Args:
            order_id: The order number to cancel.
            reason: Short reason, e.g. "changed mind" or "ordered wrong model".

        Returns:
            The new order status, or an explanation of why it cannot be cancelled.
        """
        if not session.is_identified:
            return {
                "ok": False,
                "error": "not_identified",
                "message": "I need the customer to be signed in before cancelling an order.",
            }

        order, error = _owned_order(order_id)
        if error is not None:
            return error
        assert order is not None

        if not order.get("cancellable", False):
            return {
                "ok": False,
                "error": "not_cancellable",
                "message": (
                    f"Order {order_id} is {order['status']}, so it is past the point "
                    "where it can be cancelled. If it has shipped, the customer can "
                    "return it once it arrives."
                ),
                "current_status": order["status"],
            }

        try:
            cancelled = gateway.cancel_order(order_id, reason[:200])
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "order_id": order_id,
            "new_status": cancelled["status"],
            "refund_note": (
                "Any authorized payment will be refunded to the original card "
                "within 3-5 business days."
            ),
        }

    # ----------------------------------------------------------------------
    # Payments
    # ----------------------------------------------------------------------
    @tool
    @_instrument("get_payment_status")
    def get_payment_status(order_id: str) -> dict[str, Any]:
        """Check the payment attempts made against an order.

        Use when the customer says their payment failed, they were charged
        twice, or they are asking where a refund is.

        Args:
            order_id: The order number.

        Returns:
            Each payment attempt with its status, amount, card last four and
            decline reason where applicable.
        """
        order, error = _owned_order(order_id)
        if error is not None:
            return error

        try:
            payments = gateway.list_payments(order_id)
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "order_id": order_id,
            "attempt_count": len(payments),
            "payments": [
                {
                    "payment_id": payment["payment_id"],
                    "status": payment["status"],
                    "amount_usd": payment["amount"],
                    "method": payment["method"],
                    "card": (
                        f"{payment['card_brand']} ending {payment['card_last4']}"
                        if payment.get("card_last4")
                        else payment["method"]
                    ),
                    "decline_reason": payment.get("decline_reason", ""),
                    "refunded_amount_usd": payment.get("refunded_amount", 0.0),
                }
                for payment in payments
            ],
        }

    @tool
    @_instrument("request_refund")
    def request_refund(order_id: str, reason: str = "customer_request") -> dict[str, Any]:
        """Refund the payment on a cancelled or failed order.

        Only works for orders that are already cancelled or whose payment
        failed. A refund on a shipped or delivered order is a returns case:
        use escalate_to_human for those instead of this tool.

        Args:
            order_id: The order number.
            reason: Short reason for the refund.

        Returns:
            Refund confirmation, or an explanation of why a human is needed.
        """
        if not session.is_identified:
            return {
                "ok": False,
                "error": "not_identified",
                "message": "I need the customer to be signed in before issuing a refund.",
            }

        order, error = _owned_order(order_id)
        if error is not None:
            return error
        assert order is not None

        if order["status"] not in REFUNDABLE_ORDER_STATUSES:
            return {
                "ok": False,
                "error": "requires_human",
                "message": (
                    f"Order {order_id} is {order['status']}. I can only refund orders "
                    "that are cancelled or where payment failed. For a delivered or "
                    "in-transit order this needs a returns agent."
                ),
                "current_status": order["status"],
                "suggested_next_step": "escalate_to_human",
            }

        try:
            payments = gateway.list_payments(order_id)
        except ServiceError as exc:
            return _service_error(exc)

        authorized = [p for p in payments if p["status"] == "authorized"]
        if not authorized:
            already = [p for p in payments if p["status"] == "refunded"]
            return {
                "ok": True,
                "refunded": False,
                "message": (
                    "That order has already been refunded."
                    if already
                    else "No money was ever taken for that order, so there is nothing to refund."
                ),
            }

        try:
            refund = gateway.refund_payment(authorized[0]["payment_id"], reason[:200])
        except ServiceError as exc:
            return _service_error(exc)

        return {
            "ok": True,
            "refunded": True,
            "order_id": order_id,
            "refunded_amount_usd": refund["refunded_amount"],
            "note": "Refunds reach the original card in 3-5 business days.",
        }

    # ----------------------------------------------------------------------
    # Escalation
    # ----------------------------------------------------------------------
    @tool
    @_instrument("escalate_to_human")
    def escalate_to_human(
        summary: str, urgency: str = "normal", category: str = "general"
    ) -> dict[str, Any]:
        """Hand the conversation to a human support agent.

        Call this when the customer asks for a person, when they are upset, when
        the request needs an action you have no tool for (returns, damaged
        goods, address changes on a shipped order, billing disputes), or when
        you have tried and genuinely cannot resolve it.

        Args:
            summary: What the customer needs, in one or two sentences, so the
                human does not have to re-read the whole conversation.
            urgency: "low", "normal" or "high".
            category: One of "returns", "billing", "delivery", "technical",
                "complaint" or "general".

        Returns:
            A ticket reference to give the customer, and the expected response time.
        """
        urgency = urgency if urgency in {"low", "normal", "high"} else "normal"
        record_agent_escalation(category)

        ticket = f"VMS-{int(time.time()) % 1_000_000:06d}"
        response_times = {"high": "within 1 hour", "normal": "within 4 hours", "low": "within 1 business day"}

        LOG.info(
            "escalation created ticket=%s category=%s urgency=%s customer=%s conversation=%s",
            ticket,
            category,
            urgency,
            session.customer_id or "anonymous",
            session.conversation_id or "-",
        )

        return {
            "ok": True,
            "escalated": True,
            "ticket_reference": ticket,
            "category": category,
            "urgency": urgency,
            "expected_response": response_times[urgency],
            "summary_recorded": summary[:500],
            "message_for_customer": (
                f"I have passed this to our support team with reference {ticket}. "
                f"Someone will be in touch {response_times[urgency]}."
            ),
        }

    return [
        search_products,
        get_product_details,
        list_product_categories,
        check_stock,
        view_cart,
        add_to_cart,
        remove_from_cart,
        check_checkout_readiness,
        list_my_orders,
        get_order_status,
        track_shipment,
        cancel_order,
        get_payment_status,
        request_refund,
        escalate_to_human,
    ]


TOOL_CATALOG = [
    {"name": "search_products", "service": "catalog", "writes": False},
    {"name": "get_product_details", "service": "catalog", "writes": False},
    {"name": "list_product_categories", "service": "catalog", "writes": False},
    {"name": "check_stock", "service": "inventory", "writes": False},
    {"name": "view_cart", "service": "cart", "writes": False},
    {"name": "add_to_cart", "service": "cart", "writes": True},
    {"name": "remove_from_cart", "service": "cart", "writes": True},
    {"name": "check_checkout_readiness", "service": "checkout", "writes": False},
    {"name": "list_my_orders", "service": "orders", "writes": False},
    {"name": "get_order_status", "service": "orders", "writes": False},
    {"name": "track_shipment", "service": "orders", "writes": False},
    {"name": "cancel_order", "service": "orders", "writes": True},
    {"name": "get_payment_status", "service": "payment", "writes": False},
    {"name": "request_refund", "service": "payment", "writes": True},
    {"name": "escalate_to_human", "service": "support-agent", "writes": True},
]
