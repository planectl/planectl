#!/usr/bin/env sh
# planectl gitea-init
# Runs inside the planectl-init Job (alpine/k8s image — has curl + kubectl).
#
# Actions:
#   1. Wait for Gitea API to be healthy
#   2. Create demo-repo (idempotent)
#   3. Push all seed files via Gitea Contents API
#   4. Store KUBECONFIG_B64 + optional AWS creds as Gitea Actions secrets
#   5. Obtain runner registration token from Gitea API
#   6. Rotate ArgoCD token (Gitea API)
#   7. Write planectl-wiring-tokens Secret (handoff to wiring Job)
set -eu

log() { echo "$*"; }

# ── config from env ────────────────────────────────────────────────────────────
GITEA_URL="${GITEA_URL:-http://localhost:3000}"
GITEA_CLUSTER_URL="${GITEA_CLUSTER_URL:-$GITEA_URL}"
GITEA_USER="${GITEA_ADMIN_USER:-admin}"
GITEA_PASS="${GITEA_ADMIN_PASS:-admin123}"
DEMO_REPO="${DEMO_REPO:-demo-repo}"
GITEA_NS="${GITEA_NS:-default}"
ARGOCD_NS="${ARGOCD_NS:-default}"
KUBECONFIG_B64="${KUBECONFIG_B64:-}"
AWS_KEY_ID="${AWS_ACCESS_KEY_ID:-}"
AWS_SECRET="${AWS_SECRET_ACCESS_KEY:-}"
AWS_REGION="${AWS_REGION:-eu-west-1}"
SCRIPTS_DIR="/scripts"

AUTH="-u ${GITEA_USER}:${GITEA_PASS}"

# ── helpers ────────────────────────────────────────────────────────────────────

gitea_get()  { curl -sf $AUTH "${GITEA_URL}$1"; }
gitea_post() { curl -sf $AUTH -X POST -H "Content-Type: application/json" "${GITEA_URL}$1" -d "$2"; }
gitea_put()  { curl -sf $AUTH -X PUT  -H "Content-Type: application/json" "${GITEA_URL}$1" -d "$2"; }
gitea_del()  { curl -sf $AUTH -X DELETE "${GITEA_URL}$1"; }

b64e() { printf '%s' "$1" | base64 | tr -d '\n'; }
b64e_file() { base64 < "$1" | tr -d '\n'; }

# ── 1. wait for Gitea ─────────────────────────────────────────────────────────

log "Waiting for Gitea API..."
i=0
while [ $i -lt 60 ]; do
  if curl -sf "${GITEA_URL}/api/healthz" > /dev/null 2>&1; then
    log "  Gitea is healthy."
    break
  fi
  i=$((i+1))
  log "  [$i] not yet ready..."
  sleep 5
done

# ── 2. ensure repo ────────────────────────────────────────────────────────────

log "Ensuring repo '${DEMO_REPO}'..."
STATUS=$(curl -sf -o /dev/null -w "%{http_code}" $AUTH "${GITEA_URL}/api/v1/repos/${GITEA_USER}/${DEMO_REPO}")
if [ "$STATUS" = "200" ]; then
  log "  Repo already exists."
else
  gitea_post "/api/v1/user/repos" \
    "{\"name\":\"${DEMO_REPO}\",\"private\":false,\"auto_init\":true,\"default_branch\":\"main\"}"
  log "  Repo created."
  sleep 2
fi

# ── 3. push seed files ────────────────────────────────────────────────────────

log "Pushing seed files..."

upsert_file() {
  local LOCAL="$1"
  local REPO_PATH="$2"
  if [ ! -f "$LOCAL" ]; then
    log "  SKIP  $LOCAL not found"
    return
  fi
  local CONTENT
  CONTENT=$(b64e_file "$LOCAL")
  local URL="/api/v1/repos/${GITEA_USER}/${DEMO_REPO}/contents/${REPO_PATH}"
  local EXISTING_SHA
  EXISTING_SHA=$(curl -sf $AUTH "${GITEA_URL}${URL}" | grep '"sha"' | head -1 | sed 's/.*"sha": *"\([^"]*\)".*/\1/' || true)
  if [ -n "$EXISTING_SHA" ]; then
    gitea_put "$URL" "{\"message\":\"chore: update ${REPO_PATH}\",\"content\":\"${CONTENT}\",\"sha\":\"${EXISTING_SHA}\"}" > /dev/null
  else
    gitea_post "$URL" "{\"message\":\"chore: add ${REPO_PATH}\",\"content\":\"${CONTENT}\"}" > /dev/null
  fi
  log "  OK    ${REPO_PATH}"
}

upsert_file "$SCRIPTS_DIR/ci.yaml"                 ".gitea/workflows/ci.yaml"
upsert_file "$SCRIPTS_DIR/crossplane-deploy.yaml"  ".gitea/workflows/crossplane-deploy.yaml"
upsert_file "$SCRIPTS_DIR/bucket-deploy.yaml"      ".gitea/workflows/bucket-deploy.yaml"
upsert_file "$SCRIPTS_DIR/argocd-demo-bucket.yaml" "crossplane/buckets/argocd-demo-bucket.yaml"
upsert_file "$SCRIPTS_DIR/Pulumi.yaml"             "pulumi/programs/demo/Pulumi.yaml"
upsert_file "$SCRIPTS_DIR/__main__.py"             "pulumi/programs/demo/__main__.py"
upsert_file "$SCRIPTS_DIR/requirements.txt"        "pulumi/programs/demo/requirements.txt"

# ── 4. store Actions secrets ──────────────────────────────────────────────────

log "Storing Gitea Actions secrets..."
set_secret() {
  gitea_put "/api/v1/repos/${GITEA_USER}/${DEMO_REPO}/actions/secrets/$1" \
    "{\"data\":\"$2\"}" > /dev/null
}

if [ -n "$KUBECONFIG_B64" ]; then
  set_secret "KUBECONFIG_B64" "$KUBECONFIG_B64"
  log "  OK    KUBECONFIG_B64"
else
  log "  SKIP  KUBECONFIG_B64 (planectl-kubeconfig Secret not found)"
fi

if [ -n "$AWS_KEY_ID" ]; then
  set_secret "AWS_ACCESS_KEY_ID"     "$AWS_KEY_ID"
  set_secret "AWS_SECRET_ACCESS_KEY" "$AWS_SECRET"
  set_secret "AWS_REGION"            "$AWS_REGION"
  log "  OK    AWS credentials"
fi

# ── 5. runner registration token ──────────────────────────────────────────────

log "Obtaining runner registration token..."
RUNNER_TOKEN=$(gitea_get "/api/v1/admin/runners/registration-token" | grep '"token"' | sed 's/.*"token": *"\([^"]*\)".*/\1/')
log "  OK    runner token obtained"

# ── 6. rotate ArgoCD token ────────────────────────────────────────────────────

log "Rotating ArgoCD token..."
TOKEN_ID=$(gitea_get "/api/v1/users/${GITEA_USER}/tokens" \
  | grep -B1 '"argocd-token"' | grep '"id"' | sed 's/[^0-9]//g' || true)
if [ -n "$TOKEN_ID" ]; then
  gitea_del "/api/v1/users/${GITEA_USER}/tokens/${TOKEN_ID}" > /dev/null || true
fi
ARGOCD_TOKEN=$(gitea_post "/api/v1/users/${GITEA_USER}/tokens" \
  '{"name":"argocd-token","scopes":["read:repository"]}' | grep '"sha1"' | sed 's/.*"sha1": *"\([^"]*\)".*/\1/')
log "  OK    argocd token rotated"

# ── 7. write planectl-wiring-tokens Secret ────────────────────────────────────

REPO_URL="${GITEA_CLUSTER_URL}/${GITEA_USER}/${DEMO_REPO}.git"
log "Writing planectl-wiring-tokens Secret..."

kubectl create secret generic planectl-wiring-tokens \
  --namespace "$ARGOCD_NS" \
  --from-literal="RUNNER_TOKEN=${RUNNER_TOKEN}" \
  --from-literal="ARGOCD_TOKEN=${ARGOCD_TOKEN}" \
  --from-literal="REPO_URL=${REPO_URL}" \
  --dry-run=client -o yaml | kubectl apply -f -

log "=== planectl init complete ==="
