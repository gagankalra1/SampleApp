#!/usr/bin/env bash
#
# Teardown, run when the CloudFormation stack is deleted.
#
# Why this exists
# ---------------
# The AWS Load Balancer Controller creates an Application Load Balancer and a
# security group in response to the Ingress object. CloudFormation did not
# create them and does not know they exist. When the stack is deleted,
# CloudFormation tries to delete the VPC while the ALB's network interfaces
# are still attached to its subnets, and the delete fails:
#
#   "The subnet 'subnet-...' has dependencies and cannot be deleted"
#
# The stack then sits in DELETE_FAILED and someone has to hunt down the
# orphaned load balancer by hand — in every workshop account, every time.
#
# So this script deletes the Ingress first and waits for the controller to
# finish removing the load balancer, before CloudFormation touches the
# network.
#
# Everything here is best effort. A teardown that cannot complete must not
# block the stack delete: a leftover load balancer is a small, findable cost,
# whereas an undeletable stack is a real problem.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

# Deliberately not `set -e` for the body: every step is optional, and an
# early exit here is worse than carrying on.
set +e

: "${CLUSTER_NAME:?CLUSTER_NAME is required}"
: "${AWS_REGION_NAME:?AWS_REGION_NAME is required}"

export AWS_DEFAULT_REGION="$AWS_REGION_NAME"
export AWS_REGION="$AWS_REGION_NAME"

log "VoltMart teardown starting"

# ---------------------------------------------------------------------------
# Cluster access
# ---------------------------------------------------------------------------
# If the cluster is already gone there is nothing Kubernetes-side to clean up,
# and that is a perfectly normal outcome.
if ! aws eks describe-cluster --name "$CLUSTER_NAME" --region "$AWS_REGION_NAME" >/dev/null 2>&1; then
  info "cluster ${CLUSTER_NAME} no longer exists; nothing to tear down"
  exit 0
fi

install_kubectl || { warn "could not install kubectl; skipping Kubernetes cleanup"; exit 0; }
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION_NAME" \
  || { warn "could not configure kubectl; skipping Kubernetes cleanup"; exit 0; }

if ! kubectl get namespace voltmart >/dev/null 2>&1; then
  info "the voltmart namespace is already gone"
else
  # -------------------------------------------------------------------------
  # Delete the Ingress and wait for the ALB to go
  # -------------------------------------------------------------------------
  log "Deleting the Ingress"
  kubectl -n voltmart delete ingress voltmart --ignore-not-found --timeout=5m

  log "Waiting for the load balancer to be removed"
  # Poll the ELB API rather than Kubernetes: the Ingress object disappears
  # immediately, while the controller's deletion of the actual load balancer
  # and its security group takes a few minutes. The ENIs are what block the
  # subnet delete, and they go when the ALB does.
  for attempt in $(seq 1 60); do
    remaining=$(aws elasticloadbalancing describe-load-balancers \
      --region "$AWS_REGION_NAME" \
      --query "length(LoadBalancers[?starts_with(LoadBalancerName, '${CLUSTER_NAME:0:32}')])" \
      --output text 2>/dev/null || echo 0)

    if [[ "$remaining" == "0" ]]; then
      info "load balancer removed"
      break
    fi
    [[ $((attempt % 6)) -eq 0 ]] && info "still waiting, ${remaining} load balancer(s) remaining"
    sleep 10
  done

  # -------------------------------------------------------------------------
  # Remove the rest
  # -------------------------------------------------------------------------
  # Not strictly required — deleting the node group removes the pods anyway —
  # but it lets pods terminate gracefully, which means in-flight checkouts
  # get to finish their saga rather than being cut off mid-compensation.
  log "Deleting the application namespace"
  kubectl delete namespace voltmart --timeout=5m --ignore-not-found

  log "Uninstalling the load balancer controller"
  if command -v helm >/dev/null 2>&1 || install_helm; then
    helm uninstall aws-load-balancer-controller --namespace kube-system --wait --timeout 5m
  else
    warn "helm unavailable; leaving the controller in place (the cluster is about to be deleted anyway)"
  fi
fi

# ---------------------------------------------------------------------------
# Final sweep
# ---------------------------------------------------------------------------
# A last check so the build log records whether anything is still standing.
# If something is, this message is what tells whoever reads the log where to
# look instead of leaving them to guess at a DELETE_FAILED.
log "Checking for leftover load balancers"
leftovers=$(aws elasticloadbalancing describe-load-balancers \
  --region "$AWS_REGION_NAME" \
  --query "LoadBalancers[?starts_with(LoadBalancerName, '${CLUSTER_NAME:0:32}')].LoadBalancerArn" \
  --output text 2>/dev/null)

if [[ -n "$leftovers" && "$leftovers" != "None" ]]; then
  warn "load balancers still present; the VPC delete may fail:"
  for arn in $leftovers; do warn "  ${arn}"; done
  warn "delete them manually, then retry the stack delete"
else
  info "no leftover load balancers"
fi

log "Teardown finished"
# Always zero. The Lambda driving this reports SUCCESS regardless so the
# stack delete can proceed.
exit 0
