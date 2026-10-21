# Observability

Every service emits OpenTelemetry to one collector, which fans out to three backends. The
application knows about none of them.

---

## The pipeline

```
services  ──OTLP/gRPC:4317──▶  ADOT Collector  ──▶  AWS X-Ray        (traces)
                                               ├──▶  Prometheus      (metrics)
                                               ├──▶  CloudWatch EMF  (metrics)
                                               └──▶  CloudWatch Logs (logs)
```

The collector is the only component that knows an AWS observability service exists.
Changing where telemetry goes is a config change and a pod restart — not eight image
rebuilds. That is the entire reason it is there rather than having each service export
directly.

### Why three metric destinations

They answer different questions, and no single one answers all three well.

| | Good at | Used for |
|---|---|---|
| **X-Ray** | Request-scoped causality | "This checkout failed — which of five calls broke?" |
| **Prometheus** | Time series and percentiles, PromQL | "Is p99 checkout latency drifting?" |
| **CloudWatch** | Alarms, dashboards, log correlation | Paging someone. It is the only one that can |

---

## What is instrumented

### Automatically

`instrument_app()` in [`telemetry.py`](../services/shared/voltmart_common/telemetry.py)
attaches:

- **FastAPI** — a span per request, with route, status and latency
- **httpx** — a span per outbound call, and `traceparent` injection. This is what stitches
  a checkout into one trace across five services
- **botocore** — a span per DynamoDB and Bedrock call, with table and operation
- **logging** — `trace_id` and `span_id` stamped into every log line

Health and readiness probes are excluded. Kubelet traffic would otherwise be most of your
trace volume and all of your X-Ray bill.

### By hand

Business metrics, in [`metrics.py`](../services/shared/voltmart_common/metrics.py).
Auto-instrumentation gives you request rate and error rate; these describe what a
storefront operator actually asks about.

| Metric | Dimensions | Why it exists |
|---|---|---|
| `voltmart.checkout.attempts` | `outcome`, `failed_step` | Separates "card declined" (normal) from "we're broken" |
| `voltmart.checkout.duration` | `outcome` | Successful and failed checkouts have very different shapes |
| `voltmart.checkout.compensations` | `step`, `succeeded` | **The important one.** See below |
| `voltmart.payments.attempts` | `outcome`, `decline_reason` | A spike in one reason is a real signal |
| `voltmart.orders.created` / `.value` | `currency` | Order rate and value |
| `voltmart.orders.status_transitions` | `from`, `to` | Orders piling up in one status |
| `voltmart.inventory.reservations` | `product_id`, `outcome` | Which products run out |
| `voltmart.agent.tool_calls` | `tool`, `outcome` | Which tools the model actually reaches for |
| `voltmart.agent.tokens` | `direction`, `model_id` | The cost driver |
| `voltmart.agent.escalations` | `reason` | What the agent cannot handle |

### The one alarm that matters

```
voltmart.checkout.compensations{succeeded="false"}  >  0
```

Most failures here are fine. A declined card is a normal business outcome. A `500` is
retryable. But a **failed compensation** means the saga could not undo itself: stock is
reserved against an order that will never ship, and only a human can reconcile it.

It is the only condition in this system that leaves durable inconsistency behind, so it is
the only one that pages. The threshold is zero.

---

## Trace context

Three propagators are registered, and requests arrive carrying different ones:

- **W3C `traceparent`** — between services, injected by the httpx instrumentation
- **`X-Amzn-Trace-Id`** — from the ALB
- **W3C `baggage`** — for context that should travel with a request

Reading all three keeps one trace intact from the browser through nginx to DynamoDB.

Trace ids use the X-Ray format by default (`OTEL_AWS_XRAY_ID_GENERATOR=true`), so the AWS
console and the OTLP pipeline point at the same trace. Set it to `false` for a pure OTLP
backend — `docker-compose.yml` does exactly that, because Jaeger expects standard random
ids.

### Trace ids are handed to the user

Error responses carry `error.trace_id`, the checkout result carries `trace_id`, and the
chat widget prints one under every reply. That turns "it broke" into "it broke, here is
the trace" without anyone having to correlate timestamps.

---

## Collector configuration

In [`deploy/k8s/base/collector.yaml`](../deploy/k8s/base/collector.yaml). Four parts worth
understanding:

**`memory_limiter` comes first in every pipeline.** Under memory pressure the collector
refuses data rather than being OOM-killed. A telemetry spike should not take out the thing
that would have shown you the spike.

**`k8sattributes` and `resourcedetection`** fill in pod, node, deployment and region —
things the application cannot know about itself.

**`filter/drop_probes`** drops health-check spans centrally, rather than each service
having to.

**`awsxray.indexed_attributes`** makes specific attributes filterable in the X-Ray
console. `voltmart.checkout.failed_step` being indexed is what makes "show me every
checkout that died at payment" a single click.

The collector runs two replicas spread across nodes, with a PodDisruptionBudget, so a node
drain does not create a telemetry gap. It has no CPU limit on purpose: throttling the
collector during a traffic spike loses exactly the data you need most.

---

## Pointing it somewhere else

### Any OTLP backend

Add an exporter and reference it in the pipeline:

```yaml
exporters:
  otlp/vendor:
    endpoint: otlp.example.com:4317
    headers:
      api-key: ${VENDOR_API_KEY}

service:
  pipelines:
    traces:
      exporters: [awsxray, otlp/vendor]   # both, or replace
```

Then `kubectl -n voltmart rollout restart deployment/voltmart-collector`. Put the API key
in a Kubernetes Secret and reference it as an env var — do not inline it in the ConfigMap.

### Grafana on the Prometheus workspace

```bash
aws cloudformation describe-stacks --stack-name voltmart \
  --query 'Stacks[0].Outputs[?OutputKey==`PrometheusQueryUrl`].OutputValue' --output text
```

Add it as a Prometheus data source with SigV4 auth enabled.

Queries to start with:

```promql
# checkout success rate
sum(rate(voltmart_checkout_attempts{outcome="success"}[5m]))
  / sum(rate(voltmart_checkout_attempts[5m]))

# p99 checkout latency
histogram_quantile(0.99, sum(rate(voltmart_checkout_duration_bucket[5m])) by (le))

# declines by reason
sum by (decline_reason) (rate(voltmart_payments_attempts{outcome="declined"}[5m]))

# which tools the agent uses
sum by (tool) (rate(voltmart_agent_tool_calls[5m]))
```

### Locally

`docker compose up` runs Jaeger with an OTLP receiver. Same protocol, same spans — the
application cannot tell the difference. <http://localhost:16686>

---

## Reading what you get

**A successful checkout** is one trace, about 20 spans, roughly 400-700 ms. The payment
span dominates because the simulator sleeps 120-450 ms the way a real gateway would.

**A declined checkout** stops at the payment span and grows a compensation branch:
`checkout.compensate.authorize_payment`, `checkout.compensate.create_order`,
`checkout.compensate.reserve_stock`. The shape of the trace tells you what happened before
you read a single attribute.

**An agent turn** shows the Strands agent loop spans, then `agent.tool.<name>` for each
tool, then the HTTP and DynamoDB calls underneath. You can see the model decide, act, and
answer.

### Logs

Structured JSON on stdout, every line carrying its `trace_id`:

```bash
aws logs tail /aws/voltmart/application --follow --format short
```

```sql
-- errors across all services, newest first
fields @timestamp, service, message, trace_id
| filter level = 'ERROR'
| sort @timestamp desc

-- every log line for one trace, in order
fields @timestamp, service, message
| filter trace_id = 'PASTE_TRACE_ID'
| sort @timestamp asc

-- the alarm condition, in log form
fields @timestamp, message
| filter message like /COMPENSATION FAILED/
```

That second query is the payoff of log-trace correlation: one trace id, every service's
view of it, in time order.

---

## Costs and how to trim them

| | Driver | Lever |
|---|---|---|
| X-Ray | Per trace recorded | Add a `probabilistic_sampler` processor |
| CloudWatch Logs | Per GB ingested | `LogRetentionDays`, raise `LOG_LEVEL` to `warning` |
| Prometheus | Per sample | Raise `OTEL_METRIC_EXPORT_INTERVAL` from 30s |
| EMF metrics | Per custom metric | Narrow `metric_declarations` |

At workshop volume all of these are cents. They matter if a stack is left running for
weeks — which is the real cost risk, not the telemetry.

Tail sampling worth having in production, and left out here deliberately: sampling 10% of
traces would mean the failure a workshop attendee just triggered is 90% likely to be
missing from the console, which defeats the exercise.
