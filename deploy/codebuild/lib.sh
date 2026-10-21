#!/usr/bin/env bash
# Shared helpers for the VoltMart deploy and teardown builds.
#
# The important one is signal(): CloudFormation is blocked on a WaitCondition
# until this build reports back. If the build dies without signalling, the
# stack sits waiting for an hour before failing with no explanation. So every
# exit path signals, which is what the EXIT trap in deploy.sh guarantees.

set -euo pipefail

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# CloudFormation WaitCondition signalling
# ---------------------------------------------------------------------------
# $1 = SUCCESS | FAILURE
# $2 = human-readable reason, surfaced as the stack's StorefrontUrl output
signal() {
  local status="$1" reason="$2"

  if [[ -z "${WAIT_CONDITION_URL:-}" ]]; then
    info "no WAIT_CONDITION_URL set; skipping signal (status would be ${status})"
    return 0
  fi

  # WaitCondition reasons are capped, and a multi-line shell error would
  # produce invalid JSON. Flatten and truncate.
  reason=$(printf '%s' "$reason" | tr '\n\r"' '   ' | cut -c1-1000)

  local payload
  payload=$(printf '{"Status":"%s","Reason":"%s","UniqueId":"voltmart-deploy","Data":"%s"}' \
    "$status" "$reason" "$reason")

  info "signalling ${status} to CloudFormation"
  # Retry: losing the signal costs an hour of stack timeout, so it is worth
  # several attempts.
  local attempt
  for attempt in 1 2 3 4 5; do
    if curl --silent --show-error --fail --max-time 30 \
         -X PUT -H 'Content-Type:' --data-binary "$payload" \
         "$WAIT_CONDITION_URL"; then
      info "signal delivered"
      return 0
    fi
    warn "signal attempt ${attempt} failed; retrying in $((attempt * 5))s"
    sleep $((attempt * 5))
  done

  fail "could not signal CloudFormation after 5 attempts"
  return 1
}

# ---------------------------------------------------------------------------
# Tooling
# ---------------------------------------------------------------------------
# Installs a kubectl matching the cluster's minor version. A client more than
# one minor version away from the server is unsupported and fails in
# confusing ways, and the cluster version is not pinned in this stack, so it
# has to be discovered rather than assumed.
install_kubectl() {
  local cluster_version
  cluster_version=$(aws eks describe-cluster \
    --name "$CLUSTER_NAME" --region "$AWS_REGION_NAME" \
    --query 'cluster.version' --output text)
  info "cluster is running Kubernetes ${cluster_version}"

  local patch_version
  patch_version=$(curl --silent --fail --max-time 20 \
    "https://dl.k8s.io/release/stable-${cluster_version}.txt" || true)

  if [[ -z "$patch_version" ]]; then
    # The stable-<minor>.txt marker is missing for very new releases. .0 is
    # always a real release and is well within the skew policy.
    patch_version="v${cluster_version}.0"
    warn "no stable marker for ${cluster_version}; falling back to ${patch_version}"
  fi

  info "installing kubectl ${patch_version}"
  curl --silent --show-error --fail --location --max-time 120 \
    -o /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/${patch_version}/bin/linux/amd64/kubectl"
  chmod +x /usr/local/bin/kubectl
  kubectl version --client
}

install_helm() {
  if command -v helm >/dev/null 2>&1; then
    info "helm already present: $(helm version --short)"
    return 0
  fi
  info "installing helm"
  curl --silent --show-error --fail --location --max-time 120 \
    https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    | bash
  helm version --short
}

configure_kubectl() {
  log "Configuring kubectl for ${CLUSTER_NAME}"
  aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION_NAME"
  # Prove we can actually reach the API server before going further. Without
  # this check, a missing access entry shows up much later as a cryptic
  # failure in the middle of the apply.
  kubectl version
  kubectl get nodes -o wide
}
