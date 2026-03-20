#!/usr/bin/env bash
# planectl bootstrap — interactive preflight + helm install
#
# Usage (after helm pull --untar):
#   helm repo add planectl https://drulacosmin.github.io/planectl
#   helm repo update planectl
#   helm pull planectl/planectl --untar
#   bash ./planectl/bootstrap.sh
#
# Or from a local clone:
#   bash ./charts/planectl/bootstrap.sh
set -euo pipefail

CYAN="\033[0;36m"; GREEN="\033[0;32m"; YELLOW="\033[1;33m"; BOLD="\033[1m"; NC="\033[0m"

# Chart directory is wherever this script lives
CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "\n${BOLD}${CYAN}  planectl bootstrap${NC}\n"
echo -e "  This script creates the pre-install secrets and runs helm install."
echo -e "  Press Ctrl+C at any time to abort.\n"

# ── kubectl context ────────────────────────────────────────────────────────────
CURRENT_CTX=$(kubectl config current-context 2>/dev/null || echo "none")
echo -e "  Current kubectl context: ${CYAN}${CURRENT_CTX}${NC}"
read -rp "  Use this context? [Y/n] " USE_CTX
USE_CTX="${USE_CTX:-Y}"
if [[ "$(echo "$USE_CTX" | tr '[:upper:]' '[:lower:]')" == "n" ]]; then
  echo ""
  kubectl config get-contexts --no-headers | awk '{print "    " $2}'
  echo ""
  read -rp "  Enter context name: " CURRENT_CTX
  kubectl config use-context "$CURRENT_CTX"
fi

# ── cluster API endpoint ───────────────────────────────────────────────────────
DETECTED_API=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || echo "")
echo -e "\n  Detected cluster API: ${CYAN}${DETECTED_API}${NC}"
read -rp "  Control plane API [${DETECTED_API}]: " CLUSTER_API
CLUSTER_API="${CLUSTER_API:-$DETECTED_API}"

# docker-desktop: 127.0.0.1 is unreachable from inside pods
if echo "$CLUSTER_API" | grep -q "127.0.0.1\|localhost"; then
  PORT="${CLUSTER_API##*:}"
  CLUSTER_API_INTERNAL="https://kubernetes.docker.internal:${PORT}"
  echo -e "  ${YELLOW}docker-desktop detected — will patch server to ${CLUSTER_API_INTERNAL}${NC}"
else
  CLUSTER_API_INTERNAL="$CLUSTER_API"
fi

# ── namespace ──────────────────────────────────────────────────────────────────
echo ""
read -rp "  Target namespace [gitops]: " NAMESPACE
NAMESPACE="${NAMESPACE:-gitops}"

# ── host (browser access) ──────────────────────────────────────────────────────
read -rp "  Host for browser access [localhost]: " HOST
HOST="${HOST:-localhost}"

# ── gitea admin password ───────────────────────────────────────────────────────
echo ""
read -rsp "  Gitea admin password [admin123]: " GITEA_PASS
echo ""
GITEA_PASS="${GITEA_PASS:-admin123}"

# ── AWS (optional) ─────────────────────────────────────────────────────────────
echo -e "\n  AWS credentials — leave blank to skip (add later with helm upgrade)"
read -rsp "  AWS_ACCESS_KEY_ID: " AWS_KEY; echo ""
if [[ -n "$AWS_KEY" ]]; then
  read -rsp "  AWS_SECRET_ACCESS_KEY: " AWS_SECRET; echo ""
  read -rp  "  AWS region [eu-west-1]: " AWS_REGION
  AWS_REGION="${AWS_REGION:-eu-west-1}"
else
  AWS_SECRET=""
  AWS_REGION="eu-west-1"
fi

# ── summary ────────────────────────────────────────────────────────────────────
echo -e "\n  ──────────────────────────────────────────"
echo -e "  Namespace  : ${GREEN}${NAMESPACE}${NC}"
echo -e "  Host       : ${GREEN}${HOST}${NC}"
echo -e "  API server : ${GREEN}${CLUSTER_API_INTERNAL}${NC}"
echo -e "  AWS        : ${GREEN}${AWS_KEY:+enabled}${AWS_KEY:-skipped}${NC}"
echo -e "  ──────────────────────────────────────────"
echo ""
read -rp "  Proceed? [Y/n] " CONFIRM
CONFIRM="${CONFIRM:-Y}"
[[ "$(echo "$CONFIRM" | tr '[:upper:]' '[:lower:]')" == "n" ]] && echo "Aborted." && exit 0

# ── namespace ──────────────────────────────────────────────────────────────────
echo -e "\n  Creating namespace ${CYAN}${NAMESPACE}${NC}..."
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# ── kubeconfig secret ──────────────────────────────────────────────────────────
echo "  Writing planectl-kubeconfig Secret..."
kubectl create secret generic planectl-kubeconfig \
  --namespace "$NAMESPACE" \
  --from-literal=kubeconfig="$(
    kubectl config view --minify --flatten \
      | sed "s|127.0.0.1|kubernetes.docker.internal|g" \
      | sed "s|https://localhost:|https://kubernetes.docker.internal:|g"
  )" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── optional: non-default gitea password ──────────────────────────────────────
if [[ "$GITEA_PASS" != "admin123" ]]; then
  echo "  Writing planectl-credentials Secret..."
  kubectl create secret generic planectl-credentials \
    --namespace "$NAMESPACE" \
    --from-literal=gitea_admin_pass="$GITEA_PASS" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

# ── optional: AWS credentials ──────────────────────────────────────────────────
if [[ -n "$AWS_KEY" ]]; then
  echo "  Writing planectl-aws-credentials Secret..."
  kubectl create secret generic planectl-aws-credentials \
    --namespace "$NAMESPACE" \
    --from-literal=accessKeyId="$AWS_KEY" \
    --from-literal=secretAccessKey="$AWS_SECRET" \
    --from-literal=credentials="$(printf '[default]\naws_access_key_id = %s\naws_secret_access_key = %s\n' "$AWS_KEY" "$AWS_SECRET")" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

# ── helm install ───────────────────────────────────────────────────────────────
echo -e "\n${BOLD}  Running helm install...${NC}\n"

HELM_ARGS=(
  upgrade --install planectl "$CHART_DIR"
  --namespace "$NAMESPACE"
  --set "host=${HOST}"
  --set "gitea.gitea.admin.password=${GITEA_PASS}"
  --timeout 15m
)

if [[ -n "$AWS_KEY" ]]; then
  HELM_ARGS+=(--set "aws.enabled=true")
fi

helm "${HELM_ARGS[@]}"

echo ""
echo "  Waiting for init job..."
kubectl wait --for=condition=complete job/planectl-init \
  -n "$NAMESPACE" --timeout=10m 2>/dev/null || true

echo "  Waiting for wiring job..."
kubectl wait --for=condition=complete job/planectl-wiring \
  -n "$NAMESPACE" --timeout=5m 2>/dev/null || true

echo -e "\n${GREEN}${BOLD}  planectl is up.${NC}"
echo -e "  Gitea    → http://${HOST}:30080"
echo -e "  ArgoCD   → http://${HOST}:8080"
echo -e "  Terminal → http://${HOST}:4000\n"
