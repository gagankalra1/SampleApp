#!/usr/bin/env bash
#
# Publishes the VoltMart templates and source archive to S3, then prints a
# ready-to-run launch command.
#
#   ./scripts/publish-assets.sh --bucket my-voltmart-assets
#   ./scripts/publish-assets.sh --bucket my-voltmart-assets --prefix voltmart/v2 --deploy
#
# Why an S3 bucket at all: CloudFormation reads nested stack templates over
# HTTPS, and the deployer's CodeBuild project needs the application source. An
# S3 bucket in the launching account satisfies both without the account
# needing access to a git remote.
#
# The bucket is created private, with public access blocked. For a multi-
# account workshop see the note printed at the end — do not make it public.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

BUCKET=""
PREFIX="voltmart/latest"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
STACK_NAME="voltmart"
DO_DEPLOY=false
ALLOWED_CIDR=""
BEDROCK_MODEL=""
ADMIN_PRINCIPAL=""

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: ./scripts/publish-assets.sh --bucket <name> [options]

Required:
  --bucket <name>        S3 bucket for the templates and source archive.
                         Created if it does not exist.

Options:
  --prefix <path>        Key prefix. Default: voltmart/latest
                         Use a versioned prefix (voltmart/v2) when you want a
                         stack update to redeploy new code — the stack
                         redeploys when the prefix changes.
  --region <region>      AWS region. Defaults to your configured region.
  --stack-name <name>    Stack name for the printed launch command.
                         Default: voltmart
  --deploy               Create or update the stack immediately after
                         publishing, instead of just printing the command.
  --allowed-cidr <cidr>  Restrict who can reach the storefront. Strongly
                         recommended with --deploy.
  --bedrock-model <id>   Override the support agent's model id.
  --admin-principal <arn>
                         Grant an ADDITIONAL IAM role or user cluster-admin.
                         You do not need this for yourself: whoever launches
                         the stack gets cluster-admin automatically. Passing
                         your own ARN here fails the stack with a 409, because
                         EKS already created that access entry.
  -h, --help             Show this message.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket)         BUCKET="${2:-}"; shift 2 ;;
    --prefix)         PREFIX="${2:-}"; shift 2 ;;
    --region)         REGION="${2:-}"; shift 2 ;;
    --stack-name)     STACK_NAME="${2:-}"; shift 2 ;;
    --allowed-cidr)   ALLOWED_CIDR="${2:-}"; shift 2 ;;
    --bedrock-model)  BEDROCK_MODEL="${2:-}"; shift 2 ;;
    --admin-principal) ADMIN_PRINCIPAL="${2:-}"; shift 2 ;;
    --deploy)         DO_DEPLOY=true; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown option: $1 (try --help)" ;;
  esac
done

[[ -n "$BUCKET" ]] || { usage; die "--bucket is required"; }

command -v aws >/dev/null 2>&1 || die "the AWS CLI is not installed"
command -v zip >/dev/null 2>&1 || die "zip is not installed"

if [[ -z "$REGION" ]]; then
  REGION=$(aws configure get region || true)
  [[ -n "$REGION" ]] || die "no region configured; pass --region"
fi

PREFIX="${PREFIX%/}"

log "Checking credentials"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text) \
  || die "could not call sts:GetCallerIdentity — are your credentials set?"
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)
info "account ${ACCOUNT} in ${REGION}"
info "as ${CALLER_ARN}"

# ---------------------------------------------------------------------------
# Bucket
# ---------------------------------------------------------------------------
log "Preparing s3://${BUCKET}"
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  info "bucket already exists"
else
  info "creating bucket"
  # us-east-1 is the one region where CreateBucket must not be given a
  # LocationConstraint.
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi

  # Private by default. These templates are not secret, but a public bucket is
  # a habit worth not forming, and nothing here needs anonymous access:
  # CloudFormation reads the templates as the identity launching the stack.
  aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
      "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

  aws s3api put-bucket-encryption --bucket "$BUCKET" \
    --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

  # Versioning means a re-publish to the same prefix does not destroy the
  # artifact a running stack was built from.
  aws s3api put-bucket-versioning --bucket "$BUCKET" \
    --versioning-configuration Status=Enabled

  info "bucket created: private, encrypted, versioned"
fi

# ---------------------------------------------------------------------------
# Validate before uploading
# ---------------------------------------------------------------------------
log "Validating templates"
for template in deploy/cloudformation/voltmart.yaml deploy/cloudformation/nested/*.yaml; do
  printf '    %-46s' "$(basename "$template")"
  if aws cloudformation validate-template \
       --template-body "file://${template}" \
       --region "$REGION" >/dev/null 2>/tmp/voltmart-validate.err; then
    printf 'ok\n'
  else
    printf 'FAILED\n'
    cat /tmp/voltmart-validate.err >&2
    die "template validation failed for ${template}"
  fi
done

# ---------------------------------------------------------------------------
# Source archive
# ---------------------------------------------------------------------------
log "Building the source archive"
ARCHIVE="$(mktemp -d)/source.zip"

# Only what the build actually needs. Excluding node_modules in particular
# matters: it is hundreds of megabytes that CodeBuild would download and then
# overwrite with `npm ci` anyway.
zip -q -r "$ARCHIVE" \
  services \
  frontend \
  deploy/k8s \
  deploy/codebuild \
  -x '*/node_modules/*' \
  -x '*/__pycache__/*' \
  -x '*.pyc' \
  -x 'frontend/dist/*' \
  -x 'frontend/.vite/*' \
  -x 'deploy/k8s/.generated/*' \
  -x '*/.DS_Store'

ARCHIVE_SIZE=$(du -h "$ARCHIVE" | cut -f1)
info "archive is ${ARCHIVE_SIZE}"

# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
log "Uploading to s3://${BUCKET}/${PREFIX}"

aws s3 cp "$ARCHIVE" "s3://${BUCKET}/${PREFIX}/source.zip" \
  --region "$REGION" --only-show-errors
info "source.zip"

aws s3 cp deploy/cloudformation/voltmart.yaml \
  "s3://${BUCKET}/${PREFIX}/templates/voltmart.yaml" \
  --region "$REGION" --only-show-errors
info "templates/voltmart.yaml"

aws s3 cp deploy/cloudformation/nested/ \
  "s3://${BUCKET}/${PREFIX}/templates/" \
  --recursive --region "$REGION" --only-show-errors
info "templates/ (nested)"

ROOT_TEMPLATE_URL="https://${BUCKET}.s3.${REGION}.amazonaws.com/${PREFIX}/templates/voltmart.yaml"

# ---------------------------------------------------------------------------
# Launch, or print how to
# ---------------------------------------------------------------------------
# AdminPrincipalArn is deliberately NOT set to the caller's ARN.
#
# The cluster is created with BootstrapClusterCreatorAdminPermissions, so
# whoever launches the stack already gets cluster-admin automatically. Asking
# EKS for a second access entry for that same principal fails with a 409
# AlreadyExists and rolls back the whole cluster.
#
# Pass --admin-principal only to grant a *different* role or user access.
PARAMS=(
  "ParameterKey=AssetsBucket,ParameterValue=${BUCKET}"
  "ParameterKey=AssetsPrefix,ParameterValue=${PREFIX}"
)
[[ -n "$ADMIN_PRINCIPAL" ]] && PARAMS+=("ParameterKey=AdminPrincipalArn,ParameterValue=${ADMIN_PRINCIPAL}")
[[ -n "$ALLOWED_CIDR" ]]  && PARAMS+=("ParameterKey=AllowedIngressCidr,ParameterValue=${ALLOWED_CIDR}")
[[ -n "$BEDROCK_MODEL" ]] && PARAMS+=("ParameterKey=BedrockModelId,ParameterValue=${BEDROCK_MODEL}")

if [[ "$DO_DEPLOY" == true ]]; then
  log "Deploying stack ${STACK_NAME}"

  if [[ -z "$ALLOWED_CIDR" ]]; then
    printf '\n\033[1;33mWarning:\033[0m no --allowed-cidr given, so the storefront will be\n'
    printf 'reachable from 0.0.0.0/0. That includes the AI support endpoint, which\n'
    printf 'costs Bedrock tokens per request. Press Ctrl-C to abort, or wait 10s.\n\n'
    sleep 10
  fi

  if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
    info "stack exists; updating"
    ACTION=update-stack
  else
    info "creating stack"
    ACTION=create-stack
  fi

  aws cloudformation "$ACTION" \
    --stack-name "$STACK_NAME" \
    --template-url "$ROOT_TEMPLATE_URL" \
    --parameters "${PARAMS[@]}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$REGION" \
    --tags Key=Project,Value=voltmart

  log "Waiting for the stack (this takes 20-30 minutes)"
  info "the EKS control plane alone is about 10 minutes"
  info "follow along: aws cloudformation describe-stack-events --stack-name ${STACK_NAME} --region ${REGION}"

  WAITER=$([[ "$ACTION" == "create-stack" ]] && echo stack-create-complete || echo stack-update-complete)
  if aws cloudformation wait "$WAITER" --stack-name "$STACK_NAME" --region "$REGION"; then
    log "Stack complete"
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
      --query 'Stacks[0].Outputs[].{Output:OutputKey,Value:OutputValue}' --output table
  else
    die "stack did not complete. Check the events, and the deployer log group /aws/voltmart/${STACK_NAME}/deployer"
  fi
else
  log "Published"
  cat <<EOF

Template URL
  ${ROOT_TEMPLATE_URL}

Launch from the CLI
  aws cloudformation create-stack \\
    --stack-name ${STACK_NAME} \\
    --template-url ${ROOT_TEMPLATE_URL} \\
    --parameters \\
      ParameterKey=AssetsBucket,ParameterValue=${BUCKET} \\
      ParameterKey=AssetsPrefix,ParameterValue=${PREFIX} \\
      ParameterKey=AllowedIngressCidr,ParameterValue=YOUR.IP.RANGE/32 \\
    --capabilities CAPABILITY_NAMED_IAM \\
    --region ${REGION}

  You get cluster-admin automatically as the launching principal, so there is
  no AdminPrincipalArn to set. Add it only to grant someone else access.

Launch from the console
  https://${REGION}.console.aws.amazon.com/cloudformation/home?region=${REGION}#/stacks/create/review?templateURL=$(printf '%s' "$ROOT_TEMPLATE_URL" | sed 's|:|%3A|g; s|/|%2F|g')&stackName=${STACK_NAME}

Before you launch
  * Enable Bedrock model access in ${REGION} for the agent's model, otherwise
    the storefront works but the assistant will report itself unavailable.
  * Set AllowedIngressCidr to your own range. The default publishes the
    storefront, and the Bedrock-backed agent endpoint, to the internet.

Running this in several accounts
  The simplest and safest approach is to run this script once per account, so
  each account owns its own private copy of the assets. Do not make the bucket
  public to share it; if you must share one bucket across an organisation, add
  a bucket policy scoped with aws:PrincipalOrgID instead.

EOF
fi
