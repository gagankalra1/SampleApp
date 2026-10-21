#!/usr/bin/env bash
#
# Everything that can be checked without an AWS account.
#
#   ./scripts/validate.sh
#
# Run this before publishing. It catches the mistakes that otherwise surface
# twenty minutes into a CloudFormation deploy: a YAML typo in a nested
# template, a Python syntax error, a TypeScript error, a manifest that does
# not build.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

PASS=0
FAIL=0

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf '    \033[1;31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
skip() { printf '    \033[1;33m–\033[0m %s (skipped)\n' "$*"; }

# ---------------------------------------------------------------------------
# YAML syntax
# ---------------------------------------------------------------------------
log "CloudFormation templates: YAML syntax"
# CloudFormation's short-form intrinsics (!Ref, !GetAtt, !Sub) are not
# standard YAML tags, so a plain parser rejects them. Registering a
# multi-constructor for the ! prefix lets us check structure without needing
# cfn-lint installed.
if python3 -c 'import yaml' 2>/dev/null; then
  for template in deploy/cloudformation/voltmart.yaml deploy/cloudformation/nested/*.yaml; do
    if python3 - "$template" <<'PYEOF'
import sys, yaml

class CfnLoader(yaml.SafeLoader):
    pass

CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)

path = sys.argv[1]
with open(path) as handle:
    doc = yaml.load(handle, Loader=CfnLoader)

assert isinstance(doc, dict), "template is not a mapping"
assert "Resources" in doc, "template has no Resources section"
assert doc.get("AWSTemplateFormatVersion"), "template has no AWSTemplateFormatVersion"
for name, resource in doc["Resources"].items():
    assert isinstance(resource, dict), f"{name} is not a mapping"
    assert "Type" in resource, f"{name} has no Type"
PYEOF
    then
      ok "$(basename "$template")"
    else
      bad "$(basename "$template")"
    fi
  done
else
  skip "YAML parsing (pip install pyyaml)"
fi

# ---------------------------------------------------------------------------
# cfn-lint, if available
# ---------------------------------------------------------------------------
log "CloudFormation templates: cfn-lint"
if command -v cfn-lint >/dev/null 2>&1; then
  if cfn-lint deploy/cloudformation/voltmart.yaml deploy/cloudformation/nested/*.yaml; then
    ok "cfn-lint clean"
  else
    bad "cfn-lint reported problems"
  fi
else
  skip "cfn-lint (pip install cfn-lint)"
fi

# ---------------------------------------------------------------------------
# Kubernetes manifests
# ---------------------------------------------------------------------------
log "Kubernetes manifests"
if command -v kustomize >/dev/null 2>&1; then
  BUILD_CMD=(kustomize build deploy/k8s/base)
elif command -v kubectl >/dev/null 2>&1; then
  BUILD_CMD=(kubectl kustomize deploy/k8s/base)
else
  BUILD_CMD=()
fi

if [[ ${#BUILD_CMD[@]} -gt 0 ]]; then
  if "${BUILD_CMD[@]}" > /tmp/voltmart-manifests.yaml 2>/tmp/voltmart-kustomize.err; then
    ok "kustomize build"

    if python3 -c 'import yaml' 2>/dev/null; then
      if python3 - <<'PYEOF'
import sys, yaml

docs = [d for d in yaml.safe_load_all(open("/tmp/voltmart-manifests.yaml")) if d]
problems = []

for doc in docs:
    kind = doc.get("kind")
    name = doc.get("metadata", {}).get("name", "<unnamed>")

    if kind == "Service" and doc["spec"].get("type") != "ClusterIP":
        problems.append(f"{name} is a {doc['spec'].get('type')} Service; only the Ingress should be public")

    if kind == "Deployment":
        pod = doc["spec"]["template"]["spec"]
        if not pod.get("securityContext", {}).get("runAsNonRoot"):
            problems.append(f"{name} does not set runAsNonRoot")
        for container in pod["containers"]:
            sc = container.get("securityContext", {})
            if sc.get("readOnlyRootFilesystem") is not True:
                problems.append(f"{name}/{container['name']} has a writable root filesystem")
            if sc.get("allowPrivilegeEscalation") is not False:
                problems.append(f"{name}/{container['name']} allows privilege escalation")
            if sc.get("capabilities", {}).get("drop") != ["ALL"]:
                problems.append(f"{name}/{container['name']} does not drop all capabilities")
            if "resources" not in container or "requests" not in container["resources"]:
                problems.append(f"{name}/{container['name']} has no resource requests")
            for probe in ("livenessProbe", "readinessProbe"):
                if probe not in container:
                    problems.append(f"{name}/{container['name']} has no {probe}")

if problems:
    for problem in problems:
        print("     ", problem, file=sys.stderr)
    sys.exit(1)

print(f"      {len(docs)} manifests checked")
PYEOF
      then
        ok "security and reliability invariants"
      else
        bad "security and reliability invariants"
      fi
    else
      skip "manifest invariants (pip install pyyaml)"
    fi
  else
    bad "kustomize build"
    cat /tmp/voltmart-kustomize.err >&2
  fi
else
  skip "manifests (install kubectl or kustomize)"
fi

# ---------------------------------------------------------------------------
# Python services
# ---------------------------------------------------------------------------
log "Python services"
if python3 -m compileall -q services >/dev/null 2>&1; then
  ok "all services compile"
else
  bad "a Python source file has a syntax error"
  python3 -m compileall -q services
fi

# Each service must have the three files the Dockerfile expects.
for service in catalog inventory cart orders payment checkout support-agent; do
  if [[ -f "services/${service}/main.py" && -f "services/${service}/requirements.txt" ]]; then
    ok "services/${service} is complete"
  else
    bad "services/${service} is missing main.py or requirements.txt"
  fi
done

# ---------------------------------------------------------------------------
# Shell scripts
# ---------------------------------------------------------------------------
log "Shell scripts"
for script in scripts/*.sh deploy/codebuild/*.sh; do
  if bash -n "$script" 2>/dev/null; then
    ok "$(basename "$script")"
  else
    bad "$(basename "$script")"
    bash -n "$script"
  fi
done

if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck --severity=warning scripts/*.sh deploy/codebuild/*.sh; then
    ok "shellcheck clean"
  else
    bad "shellcheck reported problems"
  fi
else
  skip "shellcheck"
fi

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
log "Frontend"
if command -v npm >/dev/null 2>&1; then
  if [[ -d frontend/node_modules ]]; then
    if (cd frontend && npm run typecheck >/dev/null 2>&1); then
      ok "TypeScript type check"
    else
      bad "TypeScript type check"
      (cd frontend && npm run typecheck)
    fi
  else
    skip "type check (run: cd frontend && npm install)"
  fi
else
  skip "frontend (npm not installed)"
fi

# ---------------------------------------------------------------------------
# Cross-cutting consistency
# ---------------------------------------------------------------------------
log "Consistency between layers"

# Every image the manifests expect must have a matching build in deploy.sh.
# This is the check that catches "added a service, forgot to build it" —
# which otherwise manifests as an ImagePullBackOff twenty minutes in.
if python3 - <<'PYEOF'
import pathlib, re, sys

manifests = pathlib.Path("deploy/k8s/base")
images = set()
for path in manifests.glob("*.yaml"):
    images.update(re.findall(r"image:\s*voltmart/([a-z-]+):", path.read_text()))

deploy_sh = pathlib.Path("deploy/codebuild/deploy.sh").read_text()
built = set(re.findall(r"^for service in (.+?); do", deploy_sh, re.M)[0].split()) if "for service in" in deploy_sh else set()
if "frontend/Dockerfile" in deploy_sh:
    built.add("frontend")

missing = images - built
extra = built - images
if missing:
    print("      manifests reference images that deploy.sh never builds:", ", ".join(sorted(missing)), file=sys.stderr)
if extra:
    print("      deploy.sh builds images no manifest uses:", ", ".join(sorted(extra)), file=sys.stderr)
if missing or extra:
    sys.exit(1)
print(f"      {len(images)} images: manifests and build script agree")
PYEOF
then
  ok "every manifest image is built by deploy.sh"
else
  bad "image list mismatch between manifests and deploy.sh"
fi

# ---------------------------------------------------------------------------
log "Summary"
printf '    %d passed, %d failed\n\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
printf '\033[1;32mAll checks passed.\033[0m Publish with:\n'
printf '  ./scripts/publish-assets.sh --bucket <your-bucket>\n\n'
