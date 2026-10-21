# Security

What this stack does properly, what it cuts short because it is a workshop sample, and
exactly what the gap is.

The honest summary: **the infrastructure posture is production-grade; the authentication
story is not.** There is no authentication at all. Everything else — IAM scoping,
credential handling, network isolation, container hardening, card data handling — is done
the way it should be done, so the code is safe to learn from and safe to copy. The parts
that are not are listed in [Shortcuts](#shortcuts), each with its replacement.

---

## What this is

A disposable sample on a workshop account, populated entirely with fabricated data:
invented products, invented customers, published fake card numbers, and a payment service
that contacts no card network. There is nothing here worth stealing.

That shapes the threat model. The risks that actually matter are:

1. **Cost abuse.** The agent endpoint invokes Amazon Bedrock on every request. An exposed,
   unauthenticated, unlimited endpoint is a bill.
2. **Habits.** Sample code gets copied. A sample that logs card numbers or puts
   application permissions on the node role teaches people to do that.
3. **Lateral movement.** If one pod is compromised, how much does the attacker get?
4. **Leaving it running.** The single most likely real incident is a forgotten stack.

What is explicitly *not* in scope: protecting the fabricated data from disclosure, and
withstanding a determined attacker. One customer cannot read another's orders — that is
enforced, because the enforcement is the lesson — but anyone who can reach the endpoint can
claim to be any customer.

---

## Enforced

### IAM: one role per service, scoped to its own data

Seven roles, in [`identity.yaml`](../deploy/cloudformation/nested/identity.yaml):

| Service | DynamoDB | Other |
|---|---|---|
| catalog | none | none |
| inventory | `inventory` read/write | — |
| cart | `carts` + its index | — |
| orders | `orders` + its index, **no DeleteItem** | — |
| payment | `payments` + its index, **no Scan, no DeleteItem** | — |
| checkout | none | — |
| support-agent | `conversations` | `bedrock:InvokeModel` on the configured model |
| collector | none | X-Ray, CloudWatch, Prometheus — all write-only |

Three details are deliberate:

- **catalog and checkout have no AWS role at all.** Catalog serves a file; checkout only
  calls other services. Neither needs credentials, so neither gets any.
- **orders cannot delete.** The application cancels orders by status transition and never
  removes a row, so the role cannot either. Permissions match behaviour.
- **payment cannot Scan.** Payment records are read by id or by order, never enumerated.

The alternative — application permissions on the node role — is shorter and much worse:
every pod on the node inherits every permission, so a bug in the catalog service grants
the ability to write orders and invoke Bedrock.

### No static credentials anywhere

EKS Pod Identity binds each role to one Kubernetes ServiceAccount. Credentials are
short-lived and delivered by the pod identity agent.

There is no access key in the repository, the images, the manifests or the ConfigMaps.
`git grep` for one and you will not find it, because there is nothing to find.

The IMDS endpoint (`169.254.169.254/32`) is blocked by NetworkPolicy, so a compromised pod
cannot reach around Pod Identity to borrow the node role.

### Containers

Every container, enforced by the namespace's `restricted` Pod Security Standard and
checked by `scripts/validate.sh`:

```yaml
runAsNonRoot: true
runAsUser: 10001                  # 101 for nginx
readOnlyRootFilesystem: true
allowPrivilegeEscalation: false
capabilities: { drop: ["ALL"] }
seccompProfile: { type: RuntimeDefault }
```

The image build also `chmod -R a-w /app`, so a compromised process cannot modify the code
it is executing. Writable paths are explicit `emptyDir` mounts with size limits.

Images are scanned on push, and ECR tags are immutable — a tag cannot be repointed at
different content after the fact.

### Network

Default-deny ingress for the namespace, then one rule per flow that exists in the code.
The policy doubles as an accurate dependency diagram, and a wrong one fails loudly rather
than permitting too much.

The property this is really for:

```yaml
# only checkout and the agent may reach payment
podSelector: { matchLabels: { app.kubernetes.io/name: payment } }
ingress:
  - from:
      - podSelector: { matchLabels: { app.kubernetes.io/name: checkout } }
      - podSelector: { matchLabels: { app.kubernetes.io/name: support-agent } }
```

The frontend is deliberately absent, so even if someone added an nginx proxy route to the
payment service by mistake, the network would refuse the connection.

> **Verify enforcement.** NetworkPolicies are accepted by the API server and silently
> ignored if the CNI does not enforce them, which is worse than having none. The
> CloudFormation stack sets `enableNetworkPolicy: "true"` on the VPC CNI add-on. Confirm:
> ```bash
> kubectl get ds aws-node -n kube-system -o yaml | grep -i network-policy
> ```

### Exposure

One Ingress, pointing at the frontend. Every other Service is `ClusterIP`.

nginx proxies an allowlist of six path prefixes. There is no route to the payment service
and no route to the OTLP collector — an internet-facing OTLP endpoint is an open funnel
into your observability bill.

Security headers on every response: a CSP with no `unsafe-eval` and no wildcard script
source, `X-Frame-Options: DENY`, `nosniff`, a restrictive `Permissions-Policy`.

### Card data

The payment service is a simulator, but it handles card data the way a real one must,
because this is the part people copy:

- The number is **never logged, never put on a span, never stored.** Only the last four
  digits and the brand survive the request.
- The CVC is used for the simulated check and dropped. Not persisted in any form — that is
  the actual PCI DSS rule.
- The stored record holds nothing that could be used to charge the card again.
- Spans carry amount, outcome and decline reason. A telemetry backend is not a vault.

Any card number not on the published test list is declined as `card_not_recognised`, so
nobody forms the impression a real card would work.

### The agent

Covered in detail in the [README](../README.md#the-ai-support-agent). The security-relevant
points:

- **Identity is closure-bound.** Tools are built per request with the customer id captured
  in a closure. No tool accepts a `customer_id`, so a prompt-injected model cannot supply
  one.
- **Ownership is re-checked.** Every tool touching customer data verifies ownership against
  the record it fetched. A guessed order id returns the same "not found" as a nonexistent
  one — saying "that exists but is not yours" would confirm the id to whoever is probing.
- **Blocked attempts are logged** with the order id and session customer.
- **Dangerous actions are gated in code.** `request_refund` only works on cancelled or
  failed orders. The system prompt says so too, but the prompt is guidance and the code is
  the rule.
- **Conversations are not served back.** `GET /conversations/{id}` returns counts and
  timestamps, not the transcript. Otherwise a conversation id becomes a credential for
  reading someone's support history.
- **Rate limited twice** — nginx at 2 req/s per IP, and a per-identity sliding window in
  the service — plus a bounded thread pool capping concurrent Bedrock calls.
- **Customer messages are not put on spans.** Support conversations contain personal
  detail; traces are not the place for it.

### Application hygiene

- Every input is validated by a Pydantic model with explicit bounds. `extra="forbid"` on
  card models, so unexpected fields are rejected rather than passed through.
- Ids are checked against `^[A-Za-z0-9_-]{1,64}$` before reaching a URL path or a DynamoDB
  key expression.
- Ids are generated with `secrets`, not counters or timestamps. A cart id is a bearer
  token; a sequential one would be enumerable.
- Unexpected exceptions return an opaque `500`. Internal messages leak table names, ARNs
  and stack detail.
- A downstream `500` is never relayed to our own caller — it may contain another service's
  internals.
- Model output is rendered as text, never as HTML.

---

## Shortcuts

Each of these is a deliberate trade for workshop usability. Each has a replacement.

### 1. There is no authentication

**What happens now.** The customer id is generated in the browser and sent in an
`X-VoltMart-Customer-Id` header, which the backend trusts. Anyone who can reach the
endpoint can claim any customer id and read that customer's orders through the agent.

**Why it is acceptable here.** The data is fabricated and the account is disposable. Adding
a real identity provider would make the stack considerably harder to launch, and the
isolation *mechanism* — which is the lesson — is implemented and tested regardless.

**The fix.** Put an authenticating proxy in front and derive the identity from the verified
token. The ALB can do it with no application change:

```yaml
alb.ingress.kubernetes.io/auth-type: cognito
alb.ingress.kubernetes.io/auth-idp-cognito: >-
  {"UserPoolArn":"...","UserPoolClientId":"...","UserPoolDomain":"..."}
```

Then read `customer_id` from the `x-amzn-oidc-data` JWT instead of the header. The change
is confined to `_identify()` in `services/support-agent/main.py` and the equivalent in the
frontend's session handling.

### 2. HTTP, not HTTPS

**What happens now.** The ALB serves plain HTTP. The checkout page posts card fields in the
clear.

**Why it is acceptable here.** The card numbers are published fakes. There is nothing
confidential in transit.

**The fix.** A certificate and the HTTPS listener. The annotations are already in
[`ingress.yaml`](../deploy/k8s/base/ingress.yaml), commented out, waiting for a domain:

```yaml
alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:...
alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
alb.ingress.kubernetes.io/ssl-redirect: "443"
alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS13-1-2-2021-06
```

Left out of the default only because issuing a certificate needs a domain the workshop
account does not necessarily have.

### 3. `AllowedIngressCidr` defaults to `0.0.0.0/0`

**Why.** So the first deploy works from anywhere, which matters when someone is following
the README in an unfamiliar account.

**The fix.** Set it, every time:

```bash
--allowed-cidr "$(curl -s https://checkip.amazonaws.com)/32"
```

This is the single highest-value change for anything that will live longer than a day,
because it is what stands between the Bedrock endpoint and the internet.

### 4. Raw card data reaches the payment service

**Why.** It is a simulator; no card network is contacted.

**The fix.** Tokenisation. The number goes from the browser straight to the processor's
client-side SDK, which returns a single-use token, and only that token reaches your
servers — so a compromise of the application could never expose card data. Not implemented
here because it requires a real processor account.

### 5. Egress is unrestricted to port 443

**Why.** Pods need DynamoDB, Bedrock, X-Ray, CloudWatch and Prometheus. Restricting to AWS
prefix lists means maintaining them, and getting it wrong produces failures that look
exactly like application bugs.

**The fix.** VPC endpoints for each service, plus an egress policy scoped to the
corresponding prefix lists. The stack already has gateway endpoints for S3 and DynamoDB.

### 6. The cluster API endpoint is publicly reachable

**Why.** So `kubectl` works from a laptop or CloudShell without a bastion.

Note that a public endpoint is not an open cluster — it still requires IAM authentication
and an access entry. Narrowing it removes the ability to even attempt authentication:

```yaml
PublicAccessCidrs: ["YOUR.RANGE/32"]
```

### 7. In-memory rate limiting

**What happens now.** The agent's rate limiter is per replica, so the effective cluster
limit is the configured value times the replica count.

**Why it is acceptable.** The bounded thread pool is the real backstop on Bedrock
concurrency, and it is per replica too but far tighter.

**The fix.** A shared counter in Redis or DynamoDB, or rate limiting at the edge with AWS
WAF.

---

## The deployer role

One role in this stack is intentionally powerful: `<stack>-deployer`. It builds and pushes
images and holds cluster-admin via an EKS access entry.

That is unavoidable — something must be able to apply manifests — and it is contained:

- only CodeBuild in that account can assume it, enforced by an `aws:SourceAccount` condition
- its ECR, log group and DynamoDB permissions are scoped by stack-name prefix
- it is deleted with the stack
- application pods never use it, and have nothing close to its permissions

---

## Operating a workshop

**Before.** Set `--allowed-cidr`. Enable only the Bedrock model you intend to use. Use a
dedicated account, not a shared one.

**During.** Watch the Bedrock spend. `voltmart.agent.tokens` on the dashboard is the
leading indicator, and it moves before the bill does.

**After.** Delete the stack. This is the most important step and the easiest to skip:

```bash
aws cloudformation delete-stack --stack-name voltmart
aws cloudformation wait stack-delete-complete --stack-name voltmart
```

The stack is built to delete cleanly, including the load balancer Kubernetes created
outside CloudFormation — see [`destroy.sh`](../deploy/codebuild/destroy.sh). The S3 assets
bucket is not part of the stack and survives; remove it separately.

Then confirm nothing is left:

```bash
aws eks list-clusters
aws elasticloadbalancing describe-load-balancers --query 'LoadBalancers[].LoadBalancerName'
aws ec2 describe-nat-gateways --filter Name=state,Values=available
```

---

## Reporting something

This is sample code, not a supported product. If you find a flaw that would mislead
someone copying it into production, that is worth raising as an issue — the habit-forming
risk is the one that matters most here.
