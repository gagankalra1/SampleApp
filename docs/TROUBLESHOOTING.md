# Troubleshooting

Symptoms first. Each entry says what to check, why it happens, and how to fix it.

---

## Start here

```bash
aws eks update-kubeconfig --region <region> --name <stack-name>

kubectl -n voltmart get pods                    # what is running
kubectl -n voltmart get ingress                 # is there a load balancer
kubectl -n voltmart logs deployment/voltmart-checkout --tail=50
aws logs tail /aws/voltmart/<stack-name>/deployer --since 1h   # why the deploy failed
```

That last one is the most useful command in this file. When a stack fails, the
CloudFormation events say "WaitCondition timed out" and nothing else; the deployer log says
what actually went wrong.

---

## The stack

### Stack fails at `DeployWaitCondition`

The deploy build failed, or died before it could signal.

```bash
aws logs tail /aws/voltmart/<stack-name>/deployer --since 1h
```

Common causes, in order of likelihood:

| In the log | Cause | Fix |
|---|---|---|
| `error: You must be logged in to the server` | The deployer's access entry is not in effect | Rare. Retry the stack |
| `no basic auth credentials` | ECR login failed | Check the deployer role's ECR permissions |
| `failed to solve` / `exit code: 1` during a build | An image failed to build | The error is directly above; usually a dependency resolution failure |
| `Ingress never received a load balancer address` | The ALB controller could not create the ALB | See [Ingress has no address](#ingress-has-no-address) |
| Nothing — the log just stops | The build hit its timeout | Raise `DeployTimeoutMinutes`; the default 45 is usually plenty |

The stack is not wasted. Fix the cause, then re-run the build without recreating the
cluster:

```bash
aws codebuild start-build --project-name <stack-name>-deployer
```

### Stack fails at `ClusterStack`

Usually an account limit rather than a template problem.

```bash
aws cloudformation describe-stack-events --stack-name <stack-name> \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
  --output table
```

- **`Cannot create cluster ... unsupported Kubernetes version`** — you pinned
  `KubernetesVersion` to something no longer supported. Leave it blank to take the EKS
  default.
- **EIP limit exceeded** — the default is 5 per region. Release unused ones, or set
  `SingleNatGateway=true` (already the default).
- **`InvalidParameterException: Subnets specified must be in at least two different AZs`** —
  the region has fewer than two usable AZs for EKS. Pick another region.

### Stack delete hangs or fails

```bash
aws cloudformation describe-stack-events --stack-name <stack-name> \
  --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
  --output table
```

**"The subnet has dependencies and cannot be deleted"** means an orphaned load balancer.
The stack's teardown build normally prevents this; if it could not run:

```bash
# find it
aws elbv2 describe-load-balancers \
  --query "LoadBalancers[?starts_with(LoadBalancerName,'<stack-name>')].[LoadBalancerName,LoadBalancerArn]" \
  --output table

# delete it, then retry the stack delete
aws elbv2 delete-load-balancer --load-balancer-arn <arn>
aws cloudformation delete-stack --stack-name <stack-name>
```

Also check for a leftover security group the controller created:

```bash
aws ec2 describe-security-groups \
  --filters "Name=tag:elbv2.k8s.aws/cluster,Values=<stack-name>" \
  --query 'SecurityGroups[].[GroupId,GroupName]' --output table
```

---

## Pods

### `CreateContainerConfigError`

A ConfigMap key the Deployment references does not exist.

```bash
kubectl -n voltmart describe pod <pod> | grep -A3 Warning
kubectl -n voltmart get configmap voltmart-tables -o yaml
```

The `voltmart-tables` and `voltmart-observability` ConfigMaps are written by the deploy
script from stack outputs. If you applied the manifests by hand, they still hold
placeholders. Re-run the deployer build.

### `CrashLoopBackOff`

```bash
kubectl -n voltmart logs <pod> --previous --tail=50
```

| In the log | Cause | Fix |
|---|---|---|
| `STORAGE_BACKEND=dynamodb requires TABLE_NAME` | `voltmart-tables` is missing or wrong | Re-run the deployer build |
| `ResourceNotFoundException: Requested resource not found` | Table name points at a table that does not exist | Compare the ConfigMap against the stack's table outputs |
| `AccessDeniedException` on DynamoDB | Pod Identity not working | See below |
| nginx: `host not found in resolver` | Wrong `NGINX_RESOLVER` | See [Frontend 502](#frontend-returns-502-or-504) |

### Pod runs but cannot reach AWS

Almost always Pod Identity not taking effect.

```bash
# 1. is the agent running?
kubectl -n kube-system get pods -l app.kubernetes.io/name=eks-pod-identity-agent

# 2. does an association exist for this service account?
aws eks list-pod-identity-associations --cluster-name <stack-name> \
  --query 'associations[].[namespace,serviceAccount,associationId]' --output table

# 3. is the pod using the service account the association names?
kubectl -n voltmart get deployment voltmart-cart \
  -o jsonpath='{.spec.template.spec.serviceAccountName}'

# 4. what identity does the pod actually have?
kubectl -n voltmart exec deployment/voltmart-cart -- \
  python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"
```

Step 4 is the decisive one. If it shows the node role rather than
`<stack-name>-cart`, the association is not being applied — restart the pod, because
credentials are injected at pod start:

```bash
kubectl -n voltmart rollout restart deployment/voltmart-cart
```

---

## The support agent

### "The assistant is unavailable right now"

Ask the service. It classifies the failure for you:

```bash
kubectl -n voltmart port-forward deployment/voltmart-support-agent 8080:8080
curl -s localhost:8080/agent/diagnostics | python3 -m json.tool
```

Read `last_turn_error`:

| `last_turn_error` says | Fix |
|---|---|
| `...is either retired or not offered in <region>` | The model id is dead. Get a current one: `aws bedrock list-inference-profiles --region <region>`, then update `BEDROCK_MODEL_ID` |
| `Access denied invoking...` | Model access is not enabled. Bedrock console → Model access → enable it. Then restart the deployment |
| `Bedrock is throttling this account` | Retry, or request a quota increase |
| `Cannot reach the Bedrock endpoint` | Egress problem — check the NAT gateway |
| `none since start-up` | The agent has not failed. The problem is elsewhere; check the nginx proxy route |

After enabling model access:

```bash
kubectl -n voltmart rollout restart deployment/voltmart-support-agent
```

Updating the model id:

```bash
kubectl -n voltmart patch configmap voltmart-config \
  --type merge -p '{"data":{"BEDROCK_MODEL_ID":"us.anthropic.claude-sonnet-4-5-20250929-v1:0"}}'
kubectl -n voltmart rollout restart deployment/voltmart-support-agent
```

> The IAM policy scopes `bedrock:InvokeModel` to the model id the **stack** was deployed
> with. Changing the ConfigMap to a model outside that scope gives an access-denied error.
> For a permanent change, update the stack's `BedrockModelId` parameter.

### Agent pod is never ready

Expected in an account without Bedrock access — the readiness probe checks that a Bedrock
client can be built, so the pod stays unready rather than returning 500s to customers.

The storefront works fine without it. The deploy script treats this as a warning, not a
failure, for exactly that reason.

### Agent answers without calling tools

The model decided it did not need one. Ask something that requires live data — "is it in
stock?", "where is my order?" — rather than "what do you sell?", which it can answer from
the conversation.

If it never calls tools:

```bash
curl -s localhost:8080/tools | python3 -m json.tool
```

Should report `tool_count: 15`.

### "That is a lot of messages at once"

Rate limited. 20 requests per minute per identity per replica, and nginx allows 2/s per
IP. Wait a minute, or raise `AGENT_RATE_LIMIT_REQUESTS` — bearing in mind every request
costs Bedrock tokens.

---

## Networking

### Ingress has no address

```bash
kubectl -n voltmart describe ingress voltmart
kubectl -n kube-system logs deployment/aws-load-balancer-controller --tail=50
```

| In the controller log | Cause | Fix |
|---|---|---|
| `couldn't auto-discover subnets` | Public subnets are missing the discovery tag | They need `kubernetes.io/role/elb=1`. The network stack sets this |
| `AccessDenied` on `elasticloadbalancing:*` | Pod Identity for the controller is not working | Check `aws eks list-pod-identity-associations` for `kube-system/aws-load-balancer-controller` |
| `UnsupportedCertificate` | A bad `certificate-arn` annotation | Fix or remove it |
| Nothing at all | The controller is not running | `kubectl -n kube-system get pods -l app.kubernetes.io/name=aws-load-balancer-controller` |

### Storefront URL times out

The ALB exists but is not reachable, or its targets are unhealthy.

```bash
# is your IP allowed?
aws cloudformation describe-stacks --stack-name <stack-name> \
  --query 'Stacks[0].Parameters[?ParameterKey==`AllowedIngressCidr`].ParameterValue' --output text
curl -s https://checkip.amazonaws.com

# are the targets healthy?
TG=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?starts_with(TargetGroupName,'k8s-voltmart')].TargetGroupArn" --output text | head -1)
aws elbv2 describe-target-health --target-group-arn "$TG"
```

`AllowedIngressCidr` not matching your address is the most common cause by a wide margin.

### Frontend returns 502 or 504

nginx cannot reach a backend.

```bash
kubectl -n voltmart logs deployment/voltmart-frontend --tail=30
kubectl -n voltmart get endpoints
```

- **Empty endpoints for a service** — its pods are not ready. Fix the pod first.
- **`no resolver defined` or `host not found in resolver`** — `NGINX_RESOLVER` is wrong.
  Compare it with the real cluster DNS address:

  ```bash
  kubectl -n kube-system get service kube-dns -o jsonpath='{.spec.clusterIP}'
  kubectl -n voltmart get deployment voltmart-frontend \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="NGINX_RESOLVER")].value}'
  ```

- **504 on `/api/support`** — an agent turn taking longer than 180 seconds. Usually means
  the model is retrying.

### Services cannot reach each other

Likely a NetworkPolicy. First confirm policies are even being enforced:

```bash
kubectl get ds aws-node -n kube-system -o yaml | grep -i network-policy
```

Then test a specific path:

```bash
kubectl -n voltmart exec deployment/voltmart-cart -- \
  python -c "import httpx; print(httpx.get('http://voltmart-catalog:8080/health', timeout=5).status_code)"
```

A hang means a policy is blocking it. `connection refused` means the pod is not listening.
`name resolution failed` means DNS — check that `allow-dns-egress` is present.

To confirm a policy is the cause, delete it temporarily:

```bash
kubectl -n voltmart delete networkpolicy default-deny-ingress
# test again, then put it back
kubectl apply -f deploy/k8s/base/network-policy.yaml
```

> Payment being unreachable from the frontend is **correct**, not a bug. Only checkout and
> the agent may reach it.

---

## Telemetry

### No traces in X-Ray

```bash
# 1. is the collector running?
kubectl -n voltmart get pods -l app.kubernetes.io/name=collector

# 2. is it receiving anything? (the debug exporter logs batch summaries)
kubectl -n voltmart logs deployment/voltmart-collector --tail=40

# 3. are services pointed at it?
kubectl -n voltmart exec deployment/voltmart-catalog -- \
  printenv OTEL_EXPORTER_OTLP_ENDPOINT
```

| Symptom | Cause |
|---|---|
| Collector logs show nothing arriving | Services have no `OTEL_EXPORTER_OTLP_ENDPOINT`, or a NetworkPolicy is blocking 4317 |
| Collector logs `AccessDeniedException` on X-Ray | Pod Identity for `voltmart-collector` is not working |
| Traces arrive but are empty of the business spans | Normal for health checks — they are filtered out. Generate real traffic |
| Nothing after 2 minutes | Batching. Spans flush every few seconds, metrics every 30 |

### No metrics on the dashboard

Empty widgets mean no traffic, not a broken pipeline. Place an order, then wait 2-3
minutes — EMF metrics are extracted from log events, which adds a delay.

```bash
# did any metric arrive at all?
aws cloudwatch list-metrics --namespace VoltMart --query 'length(Metrics)'

# is the collector managing to write?
kubectl -n voltmart logs deployment/voltmart-collector | grep -i -E 'error|denied' | tail -20
```

### Prometheus remote write failing

```bash
kubectl -n voltmart logs deployment/voltmart-collector | grep -i prometheus | tail -20
kubectl -n voltmart get configmap voltmart-observability -o yaml
```

If `AMP_REMOTE_WRITE_URL` still contains `PLACEHOLDER`, the deploy script did not patch it.
Re-run the deployer build. Traces, logs and CloudWatch metrics are unaffected either way.

---

## Local development

### `docker compose up` fails to build

```bash
docker compose build --no-cache catalog
```

The build context for Python services is `./services`, not the service directory — the
image needs the shared `voltmart_common` package. If you invoke `docker build` by hand,
match what `deploy.sh` does:

```bash
docker build -f services/Dockerfile --build-arg SERVICE=cart -t voltmart-cart services/
```

### Local services start but checkout fails

The `*_URL` variables are wrong. Each service needs to know where the others are:

```bash
curl -s localhost:8086/info    # checkout's view of its configuration
```

`scripts/run-local.sh` sets all of them. Starting services by hand means setting them
yourself.

### Agent fails locally

It still needs real Bedrock access.

```bash
aws sts get-caller-identity
curl -s localhost:8087/agent/diagnostics | python3 -m json.tool
```

`docker-compose.yml` mounts `~/.aws` read-only into the agent container. If you use a
named profile, set `AWS_PROFILE` in your environment before `docker compose up`.

### Everything worked yesterday, now ports are in use

A previous run left processes behind:

```bash
./scripts/run-local.sh stop
# or, bluntly
for p in 8081 8082 8083 8084 8085 8086 8087; do lsof -ti tcp:$p | xargs -r kill -9; done
```

---

## Still stuck?

Collect everything at once:

```bash
{
  echo "=== pods ===";        kubectl -n voltmart get pods -o wide
  echo "=== events ===";      kubectl -n voltmart get events --sort-by=.lastTimestamp | tail -30
  echo "=== ingress ===";     kubectl -n voltmart describe ingress voltmart
  echo "=== endpoints ===";   kubectl -n voltmart get endpoints
  echo "=== config ===";      kubectl -n voltmart get configmap voltmart-config -o yaml
  echo "=== tables ===";      kubectl -n voltmart get configmap voltmart-tables -o yaml
  echo "=== pod identity ==="; aws eks list-pod-identity-associations --cluster-name <stack-name>
  for d in catalog inventory cart orders payment checkout support-agent frontend collector; do
    echo "=== logs: $d ==="
    kubectl -n voltmart logs "deployment/voltmart-$d" --tail=30 2>&1
  done
} > voltmart-diagnostics.txt
```

Check it for credentials before sharing it. There should not be any, but check.
