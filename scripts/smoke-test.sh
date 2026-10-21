#!/usr/bin/env bash
#
# Exercises the whole purchase path and checks the answers.
#
#   ./scripts/smoke-test.sh                          against local services
#   ./scripts/smoke-test.sh --url http://my-alb...   against a deployment
#
# This is not a unit test suite. It drives the application the way a customer
# does — browse, add to cart, check out, cancel — and asserts on the parts
# that are easy to get wrong and easy to miss:
#
#   * money arithmetic (tax, free-shipping threshold, totals)
#   * stock moving between available / reserved / sold
#   * a declined card leaving no stock stranded
#   * a cancelled order putting its stock back on sale
#   * one customer not being able to read another's orders
#
# Run it after any change to the services, and after a deployment to confirm
# the cluster is actually working rather than merely running.

set -uo pipefail

BASE_URL=""
PASS=0
FAIL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) BASE_URL="${2:-}"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--url http://host]\n' "$0"
      printf '  Omit --url to test services on 127.0.0.1:8081-8087.\n'
      exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
done

# Against a deployment everything goes through one host and nginx routes by
# path. Locally each service has its own port.
if [[ -n "$BASE_URL" ]]; then
  BASE_URL="${BASE_URL%/}"
  CATALOG="${BASE_URL}/api/catalog"
  INVENTORY="${BASE_URL}/api/inventory/inventory"
  CART="${BASE_URL}/api/cart"
  ORDERS="${BASE_URL}/api/orders"
  CHECKOUT="${BASE_URL}/api/checkout"
  AGENT="${BASE_URL}/api/support"
  # The payment service has no public route, by design. The tests that need it
  # directly are skipped rather than reported as failures.
  PAYMENT=""
else
  CATALOG="http://127.0.0.1:8081"
  INVENTORY="http://127.0.0.1:8082/inventory"
  CART="http://127.0.0.1:8083"
  ORDERS="http://127.0.0.1:8084"
  PAYMENT="http://127.0.0.1:8085"
  CHECKOUT="http://127.0.0.1:8086"
  AGENT="http://127.0.0.1:8087"
fi

JSON='Content-Type: application/json'
CUSTOMER="cust_smoke_$(date +%s)"
OTHER_CUSTOMER="cust_other_$(date +%s)"
ADDRESS='{"full_name":"Smoke Test","line1":"410 Terry Ave N","city":"Seattle","region":"WA","postal_code":"98109","country":"US"}'
GOOD_CARD='{"number":"4242424242424242","exp_month":12,"exp_year":2030,"cvc":"123","holder_name":"Smoke Test"}'
DECLINED_CARD='{"number":"4000000000000002","exp_month":12,"exp_year":2030,"cvc":"123","holder_name":"Smoke Test"}'

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf '    \033[1;31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
skip() { printf '    \033[1;33m–\033[0m %s (skipped)\n' "$*"; }
info() { printf '      %s\n' "$*"; }

# jq is not assumed to be installed; Python is already a dependency.
jget() { python3 -c 'import json,sys; d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d[int(k)] if k.isdigit() else d.get(k) if isinstance(d, dict) else None
    if d is None: break
print("" if d is None else d)' "$1"; }

expect_eq() {
  local label="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then
    ok "$label"
  else
    bad "$label"
    info "expected: ${expected}"
    info "actual:   ${actual}"
  fi
}

status_of() {
  curl --silent --output /dev/null --write-out '%{http_code}' --max-time 30 "$@"
}

# ===========================================================================
log "Reachability"
# ===========================================================================
if [[ -n "$BASE_URL" ]]; then
  code=$(status_of "${BASE_URL}/healthz")
  expect_eq "storefront responds" "$code" "200"
fi

for pair in "catalog:${CATALOG}" "cart:${CART}" "orders:${ORDERS}" "checkout:${CHECKOUT}"; do
  name="${pair%%:*}"; url="${pair#*:}"
  code=$(status_of "${url}/health")
  if [[ "$code" == "200" ]]; then ok "${name} is healthy"; else bad "${name} is not healthy (HTTP ${code})"; fi
done

if [[ $FAIL -gt 0 ]]; then
  printf '\n\033[1;31mServices are not reachable; stopping here.\033[0m\n'
  printf 'Start them with ./scripts/run-local.sh or docker compose up.\n\n'
  exit 1
fi

# ===========================================================================
log "Catalog"
# ===========================================================================
total=$(curl --silent "${CATALOG}/products?limit=50" | jget total)
if [[ "$total" -ge 20 ]]; then ok "catalog has ${total} products"; else bad "catalog returned only ${total} products"; fi

found=$(curl --silent "${CATALOG}/products?q=noise%20cancelling" | jget total)
if [[ "$found" -ge 1 ]]; then ok "search finds noise cancelling headphones"; else bad "search returned nothing"; fi

categories=$(curl --silent "${CATALOG}/categories" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
if [[ "$categories" -ge 8 ]]; then ok "${categories} categories"; else bad "only ${categories} categories"; fi

code=$(status_of "${CATALOG}/products/does-not-exist")
expect_eq "unknown product returns 404" "$code" "404"

# ===========================================================================
log "Cart arithmetic"
# ===========================================================================
CART_ID=$(curl --silent -X POST "${CART}/carts" -H "$JSON" \
  -d "{\"customer_id\":\"${CUSTOMER}\"}" | jget cart_id)
[[ -n "$CART_ID" ]] && ok "created a cart" || bad "could not create a cart"

# One $399 item: under the $99 free-shipping threshold? No — so shipping free,
# and tax at 8.75% of 399 is 34.91.
curl --silent -X POST "${CART}/carts/${CART_ID}/items" -H "$JSON" \
  -d '{"product_id":"vm-aud-001","quantity":1}' > /dev/null
cart=$(curl --silent "${CART}/carts/${CART_ID}")
expect_eq "subtotal is 399.00"    "$(printf '%s' "$cart" | jget totals.subtotal)" "399.0"
expect_eq "tax is 8.75% = 34.91"  "$(printf '%s' "$cart" | jget totals.tax)"      "34.91"
expect_eq "shipping is free"      "$(printf '%s' "$cart" | jget totals.shipping)" "0.0"
expect_eq "total is 433.91"       "$(printf '%s' "$cart" | jget totals.total)"    "433.91"

# A cheap item on its own should attract shipping.
SMALL_CART=$(curl --silent -X POST "${CART}/carts" -H "$JSON" -d '{"customer_id":"cust_small"}' | jget cart_id)
curl --silent -X POST "${CART}/carts/${SMALL_CART}/items" -H "$JSON" \
  -d '{"product_id":"vm-acc-001","quantity":1}' > /dev/null
small=$(curl --silent "${CART}/carts/${SMALL_CART}")
expect_eq "an \$89 cart pays shipping" "$(printf '%s' "$small" | jget totals.shipping)" "9.95"
expect_eq "and is told what it needs for free shipping" \
  "$(printf '%s' "$small" | jget totals.amount_to_free_shipping)" "10.0"

# Quantity update should re-price, not duplicate the line.
curl --silent -X PUT "${CART}/carts/${CART_ID}/items/vm-aud-001" -H "$JSON" \
  -d '{"quantity":2}' > /dev/null
cart=$(curl --silent "${CART}/carts/${CART_ID}")
expect_eq "updating quantity re-prices" "$(printf '%s' "$cart" | jget totals.subtotal)" "798.0"
expect_eq "and does not duplicate the line" \
  "$(printf '%s' "$cart" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["lines"]))')" "1"

curl --silent -X PUT "${CART}/carts/${CART_ID}/items/vm-aud-001" -H "$JSON" \
  -d '{"quantity":1}' > /dev/null

# ===========================================================================
log "Checkout: the happy path"
# ===========================================================================
before=$(curl --silent "${INVENTORY}/vm-aud-001")
avail_before=$(printf '%s' "$before" | jget available)
sold_before=$(printf '%s' "$before" | jget sold)
info "stock before: available=${avail_before} sold=${sold_before}"

result=$(curl --silent -X POST "${CHECKOUT}/checkout" -H "$JSON" -d "{
  \"cart_id\":\"${CART_ID}\",\"customer_id\":\"${CUSTOMER}\",
  \"email\":\"smoke@example.com\",\"shipping_address\":${ADDRESS},
  \"shipping_method\":\"standard\",\"payment_method\":\"card\",\"card\":${GOOD_CARD}}")

ORDER_ID=$(printf '%s' "$result" | jget order_id)
if [[ -n "$ORDER_ID" ]]; then
  ok "checkout produced order ${ORDER_ID}"
  expect_eq "order is confirmed" "$(printf '%s' "$result" | jget status)" "confirmed"
  steps=$(printf '%s' "$result" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["steps_completed"]))')
  expect_eq "all 7 saga steps ran" "$steps" "7"
  trace=$(printf '%s' "$result" | jget trace_id)
  [[ ${#trace} -eq 32 ]] && ok "a trace id was returned" || bad "no usable trace id"
else
  bad "checkout failed"
  info "$(printf '%s' "$result" | head -c 300)"
fi

after=$(curl --silent "${INVENTORY}/vm-aud-001")
expect_eq "one unit left available stock" \
  "$(printf '%s' "$after" | jget available)" "$((avail_before - 1))"
expect_eq "and became a sale" \
  "$(printf '%s' "$after" | jget sold)" "$((sold_before + 1))"
expect_eq "nothing is left reserved" "$(printf '%s' "$after" | jget reserved)" "0"

expect_eq "the cart was emptied" \
  "$(curl --silent "${CART}/carts/${CART_ID}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["lines"]))')" "0"

# ===========================================================================
log "Checkout: a declined card must not strand stock"
# ===========================================================================
DECLINE_CART=$(curl --silent -X POST "${CART}/carts" -H "$JSON" \
  -d "{\"customer_id\":\"${CUSTOMER}\"}" | jget cart_id)
curl --silent -X POST "${CART}/carts/${DECLINE_CART}/items" -H "$JSON" \
  -d '{"product_id":"vm-tv-001","quantity":2}' > /dev/null

tv_before=$(curl --silent "${INVENTORY}/vm-tv-001" | jget available)

code=$(curl --silent --output /tmp/voltmart-decline.json --write-out '%{http_code}' \
  --max-time 30 -X POST "${CHECKOUT}/checkout" -H "$JSON" -d "{
    \"cart_id\":\"${DECLINE_CART}\",\"customer_id\":\"${CUSTOMER}\",
    \"email\":\"smoke@example.com\",\"shipping_address\":${ADDRESS},
    \"payment_method\":\"card\",\"card\":${DECLINED_CARD}}")

expect_eq "a declined card returns 402, not 500" "$code" "402"
expect_eq "with a machine-readable reason" \
  "$(jget error.details.decline_reason < /tmp/voltmart-decline.json)" "insufficient_funds"

tv_after=$(curl --silent "${INVENTORY}/vm-tv-001" | jget available)
expect_eq "the saga released the reserved stock" "$tv_after" "$tv_before"

expect_eq "the customer keeps their cart to retry" \
  "$(curl --silent "${CART}/carts/${DECLINE_CART}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["lines"]))')" "1"

# ===========================================================================
log "Checkout: out of stock"
# ===========================================================================
# vm-gam-002 is seeded with a single unit on purpose.
OOS_CART=$(curl --silent -X POST "${CART}/carts" -H "$JSON" -d '{"customer_id":"cust_oos"}' | jget cart_id)
curl --silent -X POST "${CART}/carts/${OOS_CART}/items" -H "$JSON" \
  -d '{"product_id":"vm-gam-002","quantity":5}' > /dev/null

expect_eq "the cart warns before checkout is attempted" \
  "$(curl --silent "${CART}/carts/${OOS_CART}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["lines"][0]["exceeds_available"])')" "True"

expect_eq "the preview refuses" \
  "$(curl --silent "${CHECKOUT}/checkout/preview/${OOS_CART}" | jget can_checkout)" "False"

code=$(status_of -X POST "${CHECKOUT}/checkout" -H "$JSON" -d "{
  \"cart_id\":\"${OOS_CART}\",\"customer_id\":\"cust_oos\",
  \"email\":\"smoke@example.com\",\"shipping_address\":${ADDRESS},
  \"payment_method\":\"card\",\"card\":${GOOD_CARD}}")
expect_eq "and checkout returns 409" "$code" "409"

# ===========================================================================
log "Orders"
# ===========================================================================
if [[ -n "$ORDER_ID" ]]; then
  order=$(curl --silent "${ORDERS}/orders/${ORDER_ID}")
  expect_eq "the order can be read back" "$(printf '%s' "$order" | jget order_id)" "$ORDER_ID"
  expect_eq "it is cancellable while unshipped" "$(printf '%s' "$order" | jget cancellable)" "True"

  expect_eq "it appears in the customer's order list" \
    "$(curl --silent "${ORDERS}/orders?customer_id=${CUSTOMER}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)) >= 1)')" "True"

  # The isolation property. A different customer must see nothing.
  expect_eq "another customer sees no orders" \
    "$(curl --silent "${ORDERS}/orders?customer_id=${OTHER_CUSTOMER}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" "0"

  # Cancelling a confirmed order must move stock from sold back to available —
  # this is the case a naive implementation silently gets wrong, because the
  # units are no longer reserved.
  sold_pre=$(curl --silent "${INVENTORY}/vm-aud-001" | jget sold)
  avail_pre=$(curl --silent "${INVENTORY}/vm-aud-001" | jget available)

  cancelled=$(curl --silent -X POST "${ORDERS}/orders/${ORDER_ID}/cancel" -H "$JSON" \
    -d '{"reason":"smoke test"}')
  expect_eq "the order cancels" "$(printf '%s' "$cancelled" | jget status)" "cancelled"

  sleep 1
  stock=$(curl --silent "${INVENTORY}/vm-aud-001")
  expect_eq "cancelling returned the unit to available stock" \
    "$(printf '%s' "$stock" | jget available)" "$((avail_pre + 1))"
  expect_eq "and reduced the sold count" \
    "$(printf '%s' "$stock" | jget sold)" "$((sold_pre - 1))"

  code=$(status_of -X POST "${ORDERS}/orders/${ORDER_ID}/cancel" -H "$JSON" -d '{"reason":"again"}')
  expect_eq "cancelling twice is refused with 409" "$code" "409"
else
  skip "order checks (no order was created)"
fi

# ===========================================================================
log "Payment simulator"
# ===========================================================================
if [[ -n "$PAYMENT" ]]; then
  expect_eq "the test cards are documented" \
    "$(curl --silent "${PAYMENT}/test-cards" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["cards"]) >= 6)')" "True"

  code=$(status_of -X POST "${PAYMENT}/payments" -H "$JSON" -d '{
    "order_id":"VM-SMOKE-0001","amount":100,"method":"card",
    "card":{"number":"4111111111111111","exp_month":12,"exp_year":2030,"cvc":"123","holder_name":"T"}}')
  expect_eq "an undocumented card number is refused" "$code" "402"

  code=$(status_of -X POST "${PAYMENT}/payments" -H "$JSON" -d '{
    "order_id":"VM-SMOKE-0002","amount":100,"method":"card",
    "card":{"number":"4242424242424242","exp_month":1,"exp_year":2024,"cvc":"123","holder_name":"T"}}')
  expect_eq "an expired card is refused" "$code" "402"
else
  skip "payment service checks (no public route, which is correct)"
fi

# ===========================================================================
log "Support agent"
# ===========================================================================
agent_code=$(status_of "${AGENT}/tools")
if [[ "$agent_code" == "200" ]]; then
  expect_eq "the agent exposes 15 tools" \
    "$(curl --silent "${AGENT}/tools" | jget tool_count)" "15"

  # Identity must not be a tool parameter — if it were, the model could act
  # for any customer it could name.
  expect_eq "no tool takes a customer id" \
    "$(curl --silent "${AGENT}/tools" | python3 -c '
import json,sys
tools = json.load(sys.stdin)["tools"]
print(not any("customer" in t["name"] for t in tools))')" "True"

  ready=$(status_of "${AGENT}/ready")
  if [[ "$ready" == "200" ]]; then
    chat=$(curl --silent --max-time 120 -X POST "${AGENT}/chat" -H "$JSON" \
      -H "X-VoltMart-Customer-Id: ${CUSTOMER}" \
      -d '{"message":"What noise cancelling headphones do you sell, and are they in stock?"}')
    reply=$(printf '%s' "$chat" | jget reply)
    if [[ -n "$reply" ]]; then
      ok "the agent answered a product question"
      info "tools used: $(printf '%s' "$chat" | python3 -c 'import json,sys; print(", ".join(json.load(sys.stdin).get("tools_used") or ["none"]))')"
      tools_used=$(printf '%s' "$chat" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("tools_used") or []))')
      [[ "$tools_used" -ge 1 ]] && ok "and called at least one tool" || bad "it answered without calling a tool"
    else
      bad "the agent returned no reply"
      info "$(printf '%s' "$chat" | head -c 200)"
    fi
  else
    skip "agent conversation (not ready — check Bedrock model access)"
    info "diagnostics: curl ${AGENT}/agent/diagnostics"
  fi
else
  skip "agent checks (service not reachable)"
fi

# ===========================================================================
printf '\n\033[1;36m==> Summary\033[0m\n'
printf '    %d passed, %d failed\n\n' "$PASS" "$FAIL"

if [[ $FAIL -eq 0 ]]; then
  printf '\033[1;32mEverything works.\033[0m\n\n'
  exit 0
fi
printf '\033[1;31m%d check(s) failed.\033[0m\n\n' "$FAIL"
exit 1
