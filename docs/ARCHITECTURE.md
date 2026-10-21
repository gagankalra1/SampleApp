# Architecture

Why VoltMart is split the way it is, and what each seam buys.

---

## Service boundaries

The split follows **rate of change and ownership of truth**, not a wish to have many
services. Each one owns exactly one kind of fact:

| Service | Owns | Changes |
|---|---|---|
| catalog | What a product is and costs | On a release cadence |
| inventory | How many units exist | On every order |
| cart | What someone intends to buy | Constantly, and disposably |
| orders | What someone agreed to buy | Once, then by status transition |
| payment | Whether money moved | Once per attempt |
| checkout | Nothing — it orchestrates | Per request |

The most instructive seam is **catalog versus inventory**. Price and description change
when a merchandiser edits them; stock changes on every single purchase. Putting them in
one service means either a hot table for cold data or a cold cache over hot data. Keeping
them apart lets the catalog be a read-only JSON file baked into the image — fast, free,
and impossible to corrupt — while inventory gets a database with the one real consistency
guarantee in the system.

**Checkout owns no data.** It is pure orchestration, which is what lets it scale
independently and be restarted freely. The cost is that it must handle partial failure
explicitly, which is the next section.

---

## The checkout saga

There is no distributed transaction. `POST /checkout` makes seven ordinary HTTP calls,
any of which can fail:

```
1. cart.get              read what they want
2. inventory.reserve     hold the stock          ← compensate: release
3. orders.create         record the intent       ← compensate: fail or cancel
4. payment.authorize     take the money          ← compensate: refund
5. orders.confirm        mark it paid
6. inventory.commit      reserved → sold
7. cart.clear            best effort
```

Each step that changes state registers a compensating action. On failure the stack unwinds
in reverse.

### Four decisions inside that loop

**Reserve before charging.** The alternative — charge, then discover the item is gone — is
a refund and an apology every time. Reserving first means the worst case is a held unit
that gets released seconds later.

**Compensations never raise.** If releasing a reservation fails while we are already
handling a payment decline, raising would replace a precise `402 payment_declined` with a
useless `500`. The failure is recorded on the span and counted in
`voltmart.checkout.compensations{succeeded="false"}`, which is the metric the stack alarms
on — because that is the one condition here that leaves durable inconsistency behind.

**Clearing the cart is best effort.** By step 7 the order exists and is paid for. Failing
the checkout because a cart would not empty would be far worse than a stale cart, so that
step logs and moves on.

**Which compensation depends on how far we got.** `orders.fail` is only legal from
`pending_payment`. Once `confirm_order` has run the order is `confirmed`, and the only
legal exit is `cancelled`. The saga checks what it completed and picks accordingly —
getting this wrong produces a 409 inside a compensation, which is how a failed checkout
turns into a stuck order.

### The bug this design makes easy to hit

`inventory.release` moves `reserved → available`. `inventory.return` moves
`sold → available`. They are not interchangeable, and calling the wrong one is a silent
no-op rather than an error.

Cancelling a *confirmed* order is the trap: checkout already committed, so the units sit
in `sold` and `reserved` is zero. A release finds nothing to give back and quietly
restores nothing — stock vanishes with no error anywhere. `orders.cancel` therefore picks
its endpoint based on the status the order was in before cancellation. The smoke test
asserts on exactly this, because it is invisible without a test that checks the numbers.

---

## Status as a state machine

Orders move through one table of legal transitions:

```
pending_payment ──▶ confirmed ──▶ packed ──▶ shipped ──▶ delivered
       │                │            │
       ├──▶ payment_failed           │
       └──▶ cancelled ◀──────────────┘
```

Anything not in that table is a `409` with the current status in the body, so the support
agent can explain *why* rather than just refusing.

Two properties come from implementing it as a table rather than a pile of `if` statements:

**Transitions are conditional writes.** The update is conditional on the status we just
read, so two concurrent cancel requests cannot both succeed. Without that, both would
trigger a refund.

**Cancellation stops at dispatch.** Once a parcel ships, the goods are physically gone, so
the correct path is a return, not a cancellation. The agent's `request_refund` tool
enforces the same boundary in code and escalates to a human instead.

---

## Data model

One table per service. Not one shared table: each service owns its schema and can be
redeployed or deleted without touching another's data, which is the point of separating
them.

| Table | Key | Index | TTL |
|---|---|---|---|
| `inventory` | `product_id` | — | — |
| `carts` | `cart_id` | `customer-index` on `customer_id` | 30 days |
| `orders` | `order_id` | `customer-index` on `customer_id` | never |
| `payments` | `payment_id` | `order-index` on `order_id` | never |
| `conversations` | `conversation_id` | — | 24 hours |

On-demand billing throughout: workshop traffic is spiky and mostly zero, so provisioned
capacity would mean paying for idle or throttling during the demo.

TTL where state is short-lived. Agent conversations are the most personal data the stack
holds and are worthless once the conversation ends, so they delete themselves rather than
depending on anyone remembering. Orders have no TTL — expiring them would make the order
history lie.

### Where stock lives

```
available   free to promise
reserved    held by an in-flight checkout
sold        committed to a completed order
```

`reserve` is a **conditional atomic update**: subtract, but only while the result stays at
or above zero. When several checkouts race for the last unit, DynamoDB arbitrates and the
application never locks. The loser gets a `409 out_of_stock` with the real available count
in the body.

### Prices are captured once

The cart stores only product ids and quantities and re-prices from the catalog on every
read, so a shopper always sees the current price.

The order does the opposite: it prices its lines at creation and stores those prices
permanently. What the customer agreed to pay is what the record says, forever. Storing a
price in the cart and billing from it is how sample apps end up selling a $2,399 laptop
for last month's price.

---

## Request path

```
browser
  → ALB                     the only public endpoint
  → nginx (frontend pod)    serves the bundle, proxies /api/* by allowlist
  → service (ClusterIP)
  → DynamoDB / Bedrock
```

nginx proxying the API from the same pod that serves the page buys three things: no CORS
configuration, no API hostname baked into the JavaScript bundle at build time, and one
public endpoint to secure instead of two.

The allowlist is the security boundary. There is **no route to the payment service** — the
browser has no business talking to it — and no route to the OTLP collector. A
NetworkPolicy enforces the same thing at the network layer, so adding a proxy route by
mistake still would not open it.

### nginx resolves upstreams at request time

Upstreams are assigned to variables before `proxy_pass`, which forces per-request DNS
resolution. With a static upstream name, nginx refuses to start if a backend Service does
not exist yet — turning a first deploy into a `CrashLoopBackOff` race. The resolver
address is the cluster's actual kube-dns ClusterIP, read at deploy time rather than
assumed.

---

## Failure behaviour

| What breaks | What happens |
|---|---|
| catalog | Cart and order pages fail — nothing can be priced without it |
| inventory | Carts still render, without stock badges. Checkout refuses: it will not sell what it cannot reserve |
| cart | Browsing works, buying does not |
| orders | Checkout fails at step 3; stock is released |
| payment | Checkout fails at step 4 with `503`, not `402`. The distinction matters: `402` is a message to the shopper, `503` is a page for an engineer |
| checkout | Browsing and carts work; purchases do not |
| support-agent | The chat widget reports itself unavailable. Nothing else notices |
| collector | Everything works, telemetry is dropped. Never let observability be a hard dependency |

Liveness probes never touch a dependency; readiness probes do. A liveness probe that
checked DynamoDB would restart every pod during a dependency blip, turning a partial
outage into a total one.

---

## Why these technology choices

**Python/FastAPI for all seven backends.** A polyglot stack would show off
language-agnostic tracing, but it triples the surface area for a reader. One language, one
shared library, one Dockerfile — and the shared library is where every service gets
identical telemetry and error shapes for free.

**React with no state library.** Two pieces of state matter: the cart and the session.
A context (`store.tsx`) is enough; Redux here would be ceremony.

**DynamoDB over an RDBMS.** No connection pools to tune, no instance to patch, no password
to rotate, and the conditional-update primitive is exactly what inventory needs. The cost
is no joins — which is why the catalog is a file and the agent does the joining.

**EKS Pod Identity over IRSA.** No OIDC provider to register, no trust policy with a
hand-built `sub` condition to get subtly wrong, and the association is a plain
CloudFormation resource.

**CodeBuild inside the stack.** CloudFormation cannot build a container image. Rather than
asking a workshop attendee to run eight `docker build` commands with the right
credentials, the stack builds its own images in the account. That is what makes it
launchable by someone who has not read the README.
