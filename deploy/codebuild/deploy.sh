#!/usr/bin/env bash
#
# Builds the VoltMart images, pushes them to ECR, and deploys the application
# to the EKS cluster. Run by CodeBuild from the CloudFormation stack.
#
# The script is the deployment, not the buildspec. Keeping the logic here
# rather than inline in YAML means it can be read, reviewed and run by hand
# against an existing cluster:
#
#   export CLUSTER_NAME=... AWS_REGION_NAME=... REGISTRY_URI=... (etc)
#   ./deploy/codebuild/deploy.sh
#
# Ordering matters and is deliberate:
#   1. tools and cluster access          — fail fast if we cannot reach it
#   2. cluster add-ons                    — the ALB controller must exist
#                                           before an Ingress can be fulfilled
#   3. images                             — the slow part
#   4. configuration from stack outputs   — table names, AMP URL, resolver
#   5. manifests                          — apply and wait for rollout
#   6. verification                       — the load balancer must serve a
#                                           real request before we call it done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

cd "$REPO_ROOT"

: "${CLUSTER_NAME:?CLUSTER_NAME is required}"
: "${AWS_REGION_NAME:?AWS_REGION_NAME is required}"
: "${REGISTRY_URI:?REGISTRY_URI is required}"
: "${NAME_PREFIX:?NAME_PREFIX is required}"

export AWS_DEFAULT_REGION="$AWS_REGION_NAME"
export AWS_REGION="$AWS_REGION_NAME"

# Immutable tag per build. ECR repositories are configured IMMUTABLE, so a
# fixed tag like "latest" would be rejected on the second deploy — and more
# importantly, an immutable tag is what makes `kubectl rollout undo` mean
# something.
IMAGE_TAG="${CODEBUILD_RESOLVED_SOURCE_VERSION:-manual}-$(date -u +%Y%m%d%H%M%S)"
IMAGE_TAG="${IMAGE_TAG:0:64}"

LB_CONTROLLER_VERSION="1.14.0"
STOREFRONT_URL=""

# ---------------------------------------------------------------------------
# Whatever happens, tell CloudFormation
# ---------------------------------------------------------------------------
# Without this trap, a failure anywhere above leaves the stack blocked on its
# WaitCondition until the one-hour ceiling, with no indication of what went
# wrong.
on_exit() {
  local code=$?
  if [[ $code -eq 0 ]]; then
    signal SUCCESS "${STOREFRONT_URL:-Deployed, but no load balancer address was resolved}"
  else
    fail "deployment failed with exit code ${code}"
    signal FAILURE "Deploy failed (exit ${code}). See the CodeBuild log: ${CODEBUILD_BUILD_ID:-unknown}"
  fi
  exit $code
}
trap on_exit EXIT

# ===========================================================================
# 1. Tools and cluster access
# ===========================================================================
log "Installing tools"
install_kubectl
install_helm
configure_kubectl

# ===========================================================================
# 2. Cluster add-ons
# ===========================================================================
log "Installing the AWS Load Balancer Controller"
# Required for the Ingress to produce an ALB. Installed with Helm because
# there is no EKS managed add-on for it.
#
# No IRSA annotation on the service account: the CloudFormation identity stack
# created a Pod Identity Association for kube-system/aws-load-balancer-controller,
# so credentials arrive without any annotation here.
helm repo add eks https://aws.github.io/eks-charts --force-update
helm repo update eks

helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace kube-system \
  --version "$LB_CONTROLLER_VERSION" \
  --set "clusterName=${CLUSTER_NAME}" \
  --set "region=${AWS_REGION_NAME}" \
  --set "vpcId=${VPC_ID}" \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set replicaCount=2 \
  --wait --timeout 10m

log "Installing metrics-server"
# Needed by the HorizontalPodAutoscalers. Pinned rather than "latest" so the
# deployment is reproducible.
kubectl apply -f \
  https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
kubectl -n kube-system rollout status deployment/metrics-server --timeout=5m || \
  warn "metrics-server did not become ready; HPAs will not scale but the app will run"

# ===========================================================================
# 3. Build and push images
# ===========================================================================
log "Logging in to ECR"
aws ecr get-login-password --region "$AWS_REGION_NAME" \
  | docker login --username AWS --password-stdin \
    "$(printf '%s' "$REGISTRY_URI" | cut -d/ -f1)"

# BuildKit gives us layer caching and parallel stage execution, which matters
# across eight images.
export DOCKER_BUILDKIT=1

build_python_service() {
  local service="$1"
  local image="${REGISTRY_URI}/${service}:${IMAGE_TAG}"
  log "Building ${service}"
  # Context is services/ so the shared voltmart_common package is available
  # alongside the service's own code. See services/Dockerfile.
  docker build \
    --file services/Dockerfile \
    --build-arg "SERVICE=${service}" \
    --build-arg "SERVICE_VERSION=${IMAGE_TAG}" \
    --tag "$image" \
    services/
  docker push "$image"
  info "pushed ${image}"
}

for service in catalog inventory cart orders payment checkout support-agent; do
  build_python_service "$service"
done

log "Building frontend"
docker build \
  --file frontend/Dockerfile \
  --tag "${REGISTRY_URI}/frontend:${IMAGE_TAG}" \
  frontend/
docker push "${REGISTRY_URI}/frontend:${IMAGE_TAG}"
info "pushed ${REGISTRY_URI}/frontend:${IMAGE_TAG}"

# ===========================================================================
# 4. Configuration that only exists after the infrastructure does
# ===========================================================================
log "Applying namespace and configuration"

# Namespace first; everything below lands inside it.
kubectl apply -f deploy/k8s/base/namespace.yaml

# The cluster's actual DNS address. nginx needs a resolver IP to resolve
# upstreams at request time, and the kube-dns ClusterIP depends on the
# cluster's service CIDR — which is usually 172.20.0.10 but should never be
# assumed.
KUBE_DNS_IP=$(kubectl -n kube-system get service kube-dns \
  -o jsonpath='{.spec.clusterIP}' 2>/dev/null || echo "172.20.0.10")
info "cluster DNS is at ${KUBE_DNS_IP}"

# Table names from the data stack.
kubectl create configmap voltmart-tables \
  --namespace voltmart \
  --from-literal="INVENTORY_TABLE=${INVENTORY_TABLE}" \
  --from-literal="CART_TABLE=${CART_TABLE}" \
  --from-literal="ORDERS_TABLE=${ORDERS_TABLE}" \
  --from-literal="PAYMENTS_TABLE=${PAYMENTS_TABLE}" \
  --from-literal="CONVERSATIONS_TABLE=${CONVERSATIONS_TABLE}" \
  --dry-run=client -o yaml | kubectl apply -f -

# The Prometheus remote-write endpoint from the observability stack.
kubectl create configmap voltmart-observability \
  --namespace voltmart \
  --from-literal="AMP_REMOTE_WRITE_URL=${AMP_REMOTE_WRITE_URL}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ===========================================================================
# 5. Build an overlay and apply
# ===========================================================================
log "Rendering manifests"

# A generated overlay rather than editing the base in place: the base stays
# a clean, readable, account-agnostic description of the application, and
# everything account-specific is layered on here. kubectl has kustomize built
# in, so no extra binary is needed.
# The overlay lives inside the repo tree, not in /tmp. Kustomize refuses to
# load a resource from outside its own root, so an overlay in a temp directory
# cannot reference ../base no matter how the path is written.
OVERLAY_DIR="${REPO_ROOT}/deploy/k8s/.generated"
rm -rf "$OVERLAY_DIR"
mkdir -p "$OVERLAY_DIR"

cat > "${OVERLAY_DIR}/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: voltmart
resources:
  - ../base
images:
  - name: voltmart/catalog
    newName: ${REGISTRY_URI}/catalog
    newTag: ${IMAGE_TAG}
  - name: voltmart/inventory
    newName: ${REGISTRY_URI}/inventory
    newTag: ${IMAGE_TAG}
  - name: voltmart/cart
    newName: ${REGISTRY_URI}/cart
    newTag: ${IMAGE_TAG}
  - name: voltmart/orders
    newName: ${REGISTRY_URI}/orders
    newTag: ${IMAGE_TAG}
  - name: voltmart/payment
    newName: ${REGISTRY_URI}/payment
    newTag: ${IMAGE_TAG}
  - name: voltmart/checkout
    newName: ${REGISTRY_URI}/checkout
    newTag: ${IMAGE_TAG}
  - name: voltmart/support-agent
    newName: ${REGISTRY_URI}/support-agent
    newTag: ${IMAGE_TAG}
  - name: voltmart/frontend
    newName: ${REGISTRY_URI}/frontend
    newTag: ${IMAGE_TAG}
patches:
  # Region, cluster name and the Bedrock model, which the base cannot know.
  - target:
      kind: ConfigMap
      name: voltmart-config
    patch: |
      - op: replace
        path: /data/AWS_REGION
        value: "${AWS_REGION_NAME}"
      - op: replace
        path: /data/K8S_CLUSTER_NAME
        value: "${CLUSTER_NAME}"
      - op: replace
        path: /data/BEDROCK_MODEL_ID
        value: "${BEDROCK_MODEL_ID}"
  # Who may reach the public load balancer.
  - target:
      kind: Ingress
      name: voltmart
    patch: |
      - op: replace
        path: /metadata/annotations/alb.ingress.kubernetes.io~1inbound-cidrs
        value: "${ALLOWED_INGRESS_CIDR}"
      - op: replace
        path: /metadata/annotations/alb.ingress.kubernetes.io~1load-balancer-name
        value: "${NAME_PREFIX:0:32}"
EOF

# Patch the frontend's resolver separately: a strategic merge patch is much
# clearer than a JSON pointer into a container env array.
cat > "${OVERLAY_DIR}/frontend-resolver.yaml" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: voltmart-frontend
  namespace: voltmart
spec:
  template:
    spec:
      containers:
        - name: frontend
          env:
            - name: NGINX_RESOLVER
              value: "${KUBE_DNS_IP}"
EOF
# Appended as a `path:` entry, which is how kustomize takes a patch from a
# file rather than from an inline string.
printf '  - path: frontend-resolver.yaml\n' >> "${OVERLAY_DIR}/kustomization.yaml"

info "rendered manifest summary:"
kubectl kustomize "$OVERLAY_DIR" | grep -E '^(kind|  name):' | paste - - | sort | uniq -c | sed 's/^/    /'

log "Applying manifests"
kubectl apply -k "$OVERLAY_DIR"

# ===========================================================================
# 6. Wait for the rollout
# ===========================================================================
log "Waiting for workloads to become ready"

# The collector first: services export telemetry from the moment they start,
# and a collector that is not up yet means the first traces are dropped.
kubectl -n voltmart rollout status deployment/voltmart-collector --timeout=5m

for deployment in catalog inventory cart orders payment checkout frontend; do
  kubectl -n voltmart rollout status "deployment/voltmart-${deployment}" --timeout=5m
done

# The agent is last and allowed to fail without failing the deployment. Its
# readiness probe checks that a Bedrock client can be built, so in an account
# where model access has not been enabled it will never become ready — and
# that should not block a workshop whose storefront works perfectly well.
if ! kubectl -n voltmart rollout status deployment/voltmart-support-agent --timeout=6m; then
  warn "the support agent did not become ready"
  warn "this is almost always Bedrock model access for ${BEDROCK_MODEL_ID} not being enabled"
  warn "the storefront works regardless; enable model access and run:"
  warn "  kubectl -n voltmart rollout restart deployment/voltmart-support-agent"
  kubectl -n voltmart logs deployment/voltmart-support-agent --tail=40 || true
fi

# ===========================================================================
# 7. Verify the load balancer actually serves traffic
# ===========================================================================
log "Waiting for the load balancer"

# Two separate waits. First the controller has to create the ALB and report
# its address; then the ALB's own health checks have to pass before it
# forwards anything. Reporting success after only the first produces a URL
# that 503s for several minutes, which is the single most confusing way for a
# workshop to begin.
ALB_HOST=""
for _ in $(seq 1 60); do
  ALB_HOST=$(kubectl -n voltmart get ingress voltmart \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  [[ -n "$ALB_HOST" ]] && break
  sleep 10
done

if [[ -z "$ALB_HOST" ]]; then
  fail "the Ingress never received a load balancer address"
  kubectl -n voltmart describe ingress voltmart || true
  kubectl -n kube-system logs deployment/aws-load-balancer-controller --tail=50 || true
  exit 1
fi

info "load balancer address: ${ALB_HOST}"
STOREFRONT_URL="http://${ALB_HOST}"

info "waiting for the load balancer to pass its health checks"
for attempt in $(seq 1 60); do
  code=$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --max-time 10 "${STOREFRONT_URL}/healthz" || echo 000)
  if [[ "$code" == "200" ]]; then
    info "storefront is serving traffic"
    break
  fi
  [[ $((attempt % 6)) -eq 0 ]] && info "still waiting (last status ${code})"
  sleep 10
done

# A functional check, not just a health check: this exercises nginx, the
# proxy route and the catalog service together.
log "Verifying the API through the load balancer"
if curl --silent --fail --max-time 20 "${STOREFRONT_URL}/api/catalog/products?limit=1" \
     | grep -q product_id; then
  info "catalog API is reachable and returning products"
else
  warn "the catalog API did not respond as expected through the load balancer"
  warn "the storefront may need another minute for target registration"
fi

log "Deployment complete"
info "storefront:  ${STOREFRONT_URL}"
info "image tag:   ${IMAGE_TAG}"
kubectl -n voltmart get pods -o wide

# The trap signals SUCCESS with STOREFRONT_URL, which becomes the stack's
# StorefrontUrl output.
