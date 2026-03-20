# planectl v0.5.0 — Claude Code task list

Work through these tasks in order. Each section is self-contained and
can be committed separately. File paths are relative to the repo root.

---

## task 1 — delete tutorial.yaml

`charts/planectl/templates/tutorial.yaml` is already neutered (one comment line).
Delete the file entirely so there is no ambiguity.

```bash
rm charts/planectl/templates/tutorial.yaml
```

---

## task 2 — replace pip install with curl in the init Job

**Problem:** `init-job.yaml` runs `pip install httpx --quiet` at runtime,
hitting PyPI on every install. A PyPI timeout fails the whole bootstrap.

**Fix:** Replace the `python:3.12-slim` init container with `alpine/k8s:1.30.2`
(already used by the wiring Job) and rewrite `gitea-init.py` as a shell
script `gitea-init.sh`.

### 2a — rewrite `charts/planectl/files/gitea-init.py` as `charts/planectl/files/gitea-init.sh`

Create `charts/planectl/files/gitea-init.sh` with this content:

```bash
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
HEADERS='-H "Content-Type: application/json"'

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
```

### 2b — update `charts/planectl/templates/init-job.yaml`

Change:
- `image: python:3.12-slim` → `image: alpine/k8s:1.30.2`
- command from `pip install httpx --quiet && python /scripts/gitea-init.py`
  to `sh /scripts/gitea-init.sh`

### 2c — update `charts/planectl/templates/init-configmap.yaml`

Change the data key from `gitea-init.py` to `gitea-init.sh` and
use `.Files.Get "files/gitea-init.sh"`.

Remove `gitea-init.py` from the repo:
```bash
rm charts/planectl/files/gitea-init.py
```

---

## task 3 — add wait for Jobs to bootstrap.sh

At the end of `charts/planectl/bootstrap.sh`, after the `helm upgrade --install`
call and before the success message, add:

```bash
echo ""
echo "  Waiting for init job..."
kubectl wait --for=condition=complete job/planectl-init \
  -n "$NAMESPACE" --timeout=10m 2>/dev/null || true

echo "  Waiting for wiring job..."
kubectl wait --for=condition=complete job/planectl-wiring \
  -n "$NAMESPACE" --timeout=5m 2>/dev/null || true
```

The `|| true` prevents the script from exiting if a Job already cleaned
itself up via `hook-delete-policy: hook-succeeded`.

---

## task 4 — add KEDA as optional fifth chart

### 4a — `charts/planectl/Chart.yaml`

Add to the `dependencies` list:
```yaml
- name: keda
  version: "2.16.0"
  repository: https://kedacore.github.io/charts
  condition: keda.enabled
```

### 4b — `charts/planectl/values.yaml`

Add after the `pulumi-kubernetes-operator` block:
```yaml
# ── KEDA (optional) ───────────────────────────────────────────────────────────
# Kubernetes Event-Driven Autoscaler — scales runners and workloads on
# queue depth, Prometheus metrics, or any KEDA trigger source.
# Enable to unlock the ScaledObject wiring examples.
keda:
  enabled: false
  resources:
    operator:
      requests:
        cpu: 25m
        memory: 64Mi
      limits:
        cpu: 150m
        memory: 256Mi
    metricServer:
      requests:
        cpu: 25m
        memory: 64Mi
      limits:
        cpu: 100m
        memory: 256Mi
```

### 4c — new file `charts/planectl/wiring/keda-runner-scaledobject.yaml`

```yaml
# KEDA ScaledObject — scales the Gitea Actions runner on workflow queue depth.
# Applied by the wiring Job only when keda.enabled=true.
#
# How it works: KEDA polls the Gitea API for pending workflow jobs.
# When queue depth > 0, it scales the runner Deployment up.
# When the queue drains it scales back to minReplicaCount.
#
# To adapt this for your own workload:
#   change scaleTargetRef.name and the trigger metadata.
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: gitea-runner-scaler
  namespace: $GITEA_NS
spec:
  scaleTargetRef:
    name: gitea-runner
  minReplicaCount: 1
  maxReplicaCount: 5
  cooldownPeriod: 60
  triggers:
    - type: metrics-api
      metadata:
        targetValue: "1"
        url: "http://gitea-http.$GITEA_NS.svc.cluster.local:3000/api/v1/repos/$GITEA_USER/$DEMO_REPO/actions/tasks?state=waiting&limit=1"
        valueLocation: "total_count"
        authMode: "basicAuth"
      authenticationRef:
        name: gitea-keda-auth
---
apiVersion: keda.sh/v1alpha1
kind: TriggerAuthentication
metadata:
  name: gitea-keda-auth
  namespace: $GITEA_NS
spec:
  secretTargetRef:
    - parameter: username
      name: gitea-runner-token
      key: username
    - parameter: password
      name: gitea-runner-token
      key: token
```

### 4d — update `charts/planectl/templates/wiring-configmap.yaml`

Add after the existing data keys:
```yaml
  keda-runner-scaledobject.yaml: |
{{ .Files.Get "wiring/keda-runner-scaledobject.yaml" | indent 4 }}
```

### 4e — update `charts/planectl/templates/wiring-job.yaml`

In the wiring Job shell script, after the AWS block, add:
```sh
if [ "$KEDA_ENABLED" = "true" ]; then
  echo "KEDA enabled -- applying runner ScaledObject..."
  sed \
    -e "s|\\\$GITEA_NS|${GITEA_NS}|g" \
    -e "s|\\\$GITEA_USER|${GITEA_USER}|g" \
    -e "s|\\\$DEMO_REPO|${DEMO_REPO}|g" \
    /wiring/keda-runner-scaledobject.yaml | kubectl apply -f -
fi
```

Add to the `env:` block of the wiring Job container:
```yaml
- name: KEDA_ENABLED
  value: {{ .Values.keda.enabled | quote }}
- name: DEMO_REPO
  value: "demo-repo"
```

### 4f — run `helm dependency update`

```bash
cd charts/planectl
helm dependency update
```

This downloads `keda-2.16.0.tgz` into `charts/planectl/charts/`.

---

## task 5 — terminal security note + opt-in default

The terminal gives a shell inside the cluster pod. For a local demo this
is a useful cockpit. For any shared or remote deployment it should not be
exposed without authentication.

### 5a — `charts/planectl/values.yaml`

Change the terminal block comment to:
```yaml
# ── Terminal ──────────────────────────────────────────────────────────────────
# xterm.js browser terminal — kubectl, helm, and gitea-cli pre-configured.
# Useful cockpit for local demos. For remote/shared clusters, disable this
# or place it behind an authenticating proxy (oauth2-proxy + Okta/GitHub/etc).
#
# To disable:  --set terminal.enabled=false
# To secure:   add an oauth2-proxy sidecar and set terminal.authProxy.enabled=true
#              (see wiring examples in the README)
terminal:
  enabled: true
  port: 4000
```

### 5b — `charts/planectl/templates/terminal.yaml`

Add a warning annotation to the terminal Service so it shows up in
`kubectl describe`:
```yaml
metadata:
  annotations:
    planectl/security-note: "Unauthenticated shell access. Disable or proxy for shared clusters."
```

---

## task 6 — rewrite README.md

Replace `planectl-main/README.md` entirely with the following.

```markdown
# planectl

One `helm install` that stands up a complete GitOps management platform
on any Kubernetes cluster.

**Everything installed is a vanilla upstream chart — no forks, no patches,
no private images.** planectl only provides the values that right-size
them for a local cluster and the wiring that connects them to each other.
You own every component outright.

---

## what it installs

| component | chart | what it does |
|---|---|---|
| **Gitea** | `gitea/gitea` | git server + Actions CI runner |
| **ArgoCD** | `argo/argo-cd` | GitOps CD — watches Gitea, syncs cluster state |
| **Crossplane** | `crossplane-stable/crossplane` | cloud resources as Kubernetes objects |
| **Pulumi Operator** | `oci://ghcr.io/pulumi/helm-charts/...` | Pulumi stacks as Kubernetes CRs |
| **KEDA** *(optional)* | `kedacore/keda` | event-driven autoscaling |

After install, a post-install Job wires them together: ArgoCD is pointed
at the Gitea repo, the Actions runner is registered, Pulumi Stack and
Program CRs are created. All connections live in `wiring/` as plain YAML —
readable, copyable, and LLM-navigable.

```bash
kubectl describe cm planectl-wiring -n gitops
```

---

## why planectl

- **Seconds to a working platform.** One bootstrap script, one helm install.
- **Full transparency.** Every connection between components is a YAML file
  you can read, copy, and modify. No hidden glue.
- **Pre-wired examples out of the box.** Push to Gitea → ArgoCD syncs →
  Crossplane provisions cloud resources → Pulumi manages stack state. You
  see a working end-to-end flow immediately, then bend it to your needs.
- **Your components, your versions.** Because everything is a vanilla upstream
  chart, you can pin versions, override values, and upgrade components
  independently without touching planectl.

---

## install

### prerequisites

- Kubernetes cluster (Docker Desktop, kind, EKS, AKS, GKE, ...)
- `helm` ≥ 3.12 and `kubectl` on your PATH

### one-command bootstrap

```bash
helm repo add planectl https://drulacosmin.github.io/planectl
helm repo update planectl
helm pull planectl/planectl --untar
bash ./planectl/bootstrap.sh
```

The script prompts for:

| prompt | default | notes |
|---|---|---|
| kubectl context | current context | |
| control plane API | auto-detected | patched for Docker Desktop automatically |
| namespace | `gitops` | |
| host | `localhost` | set to public IP or hostname for remote clusters |
| Gitea admin password | `admin123` | |
| AWS credentials | *(skip)* | add later via `helm upgrade` |

### access

| service | url | login |
|---|---|---|
| Gitea | `http://<host>:30080` | `admin` / your password |
| ArgoCD | `http://<host>:8080` | `admin` / see below |
| Terminal | `http://<host>:4000` | — |

```bash
# ArgoCD initial password
kubectl get secret -n gitops argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d
```

---

## how it's wired

Every connection between components is a file in `wiring/`. The wiring
Job applies them after the components are running.

```
wiring/
  argocd-repo-secret.yaml          ArgoCD → Gitea (repo credentials)
  argocd-applications.yaml         ArgoCD watches pulumi/stacks in Gitea
  argocd-app-crossplane-buckets.yaml   ArgoCD watches crossplane/buckets (aws only)
  runner-token-secret.yaml         Gitea runner registration token
  pulumi-stack.yaml                Pulumi Stack CR
  pulumi-program.yaml              Pulumi Program CR (inline — no external git)
  keda-runner-scaledobject.yaml    KEDA scales runner on queue depth (keda only)
```

To understand a connection, read the file. To add a new ArgoCD Application:

```bash
# copy the template, edit, apply
cp ./planectl/wiring/argocd-applications.yaml my-app.yaml
# edit repoURL, path, namespace
kubectl apply -f my-app.yaml
```

Or paste it into a chat: *"I want ArgoCD to also watch infra/quotas/ — what do I add?"*

---

## enable AWS (Crossplane S3 workflow)

```bash
helm upgrade planectl planectl/planectl -n gitops \
  --set aws.enabled=true \
  --set aws.accessKeyId=<KEY> \
  --set aws.secretAccessKey=<SECRET>
```

This installs the Crossplane AWS providers and creates the ArgoCD
Application that watches `crossplane/buckets/` in your Gitea repo.
Commit a Bucket manifest and watch it provision a real S3 bucket.

---

## enable KEDA (event-driven autoscaling)

```bash
helm upgrade planectl planectl/planectl -n gitops \
  --set keda.enabled=true
```

Installs KEDA and applies a ScaledObject that scales the Gitea Actions
runner from 1 to 5 replicas based on workflow queue depth. Push 10
workflows and watch the runner scale up, then back down as the queue drains.

---

## the terminal

The browser terminal at `:4000` is an xterm.js shell inside the cluster
pod — `kubectl`, `helm`, and `gitea-cli` are pre-configured.

It is useful as a cockpit for local demos. **For remote or shared clusters
it should be disabled or placed behind an authenticating proxy.**

```bash
# disable
helm upgrade planectl planectl/planectl -n gitops --set terminal.enabled=false

# or secure with oauth2-proxy (Okta, GitHub, Google, ...)
# see https://oauth2-proxy.github.io/oauth2-proxy/
```

---

## teardown

```bash
helm uninstall planectl -n gitops
kubectl delete namespace gitops
```

---

## bending it to your needs

planectl is a starting point. Common modifications:

**Add a new ArgoCD Application** — copy `wiring/argocd-applications.yaml`,
change `path` and `namespace`, `kubectl apply -f`.

**Point at a remote cluster** — set `host` to your server IP or hostname
in `bootstrap.sh`, or `--set host=<IP>` on `helm upgrade`.

**Add a cloud provider to Crossplane** — install the provider, create a
ProviderConfig pointing at your credentials Secret. The pattern is in
`wiring/` — Crossplane ProviderConfig comes next in a future wiring file.

**Change the Pulumi program** — edit `wiring/pulumi-program.yaml` and
`kubectl apply -f` it. The Pulumi Operator picks up the change and
reconciles immediately.

**Use planectl as the base for your own platform chart** — fork the repo,
add your own components as chart dependencies, add their wiring files.
The bootstrap + wiring pattern scales to N components.
```

---

## task 7 — version bump and Chart.yaml description

### 7a — `charts/planectl/Chart.yaml`

- Change `version: 0.4.2` → `version: 0.5.0`
- Change `appVersion: "0.4.2"` → `appVersion: "0.5.0"`
- Update `description` to:

```yaml
description: |
  One helm install. Four vanilla upstream CNCF charts (Gitea, ArgoCD,
  Crossplane, Pulumi Operator) pre-wired and ready to use as a GitOps
  management platform. All connections live in readable wiring/ YAML files.
  Optional: KEDA for event-driven autoscaling.
```

### 7b — `charts/planectl/templates/NOTES.txt`

Add at the top before the access URLs:

```
All components are vanilla upstream charts — no forks, no private images.
Inspect the wiring between them:
  kubectl describe cm planectl-wiring -n {{ .Release.Namespace }}
```

---

## task 8 — package and publish

```bash
# from repo root
helm package charts/planectl -d docs/
helm repo index docs/ --url https://drulacosmin.github.io/planectl
git add .
git commit -m "release: v0.5.0 — wiring layer, shell init, KEDA, bootstrap"
git push
```

---

## verification checklist

After `bash ./planectl/bootstrap.sh` completes on Docker Desktop:

- [ ] `kubectl get pods -n gitops` — all pods Running
- [ ] `kubectl get job planectl-init -n gitops` — Complete
- [ ] `kubectl get job planectl-wiring -n gitops` — Complete
- [ ] Gitea at `http://localhost:30080` — demo-repo exists with seed files
- [ ] ArgoCD at `http://localhost:8080` — `pulumi-stacks` Application Synced/Healthy
- [ ] Runner registered: `http://localhost:30080/-/admin/runners`
- [ ] Terminal at `http://localhost:4000` — shell prompt, `kubectl get nodes` works
- [ ] `kubectl describe cm planectl-wiring -n gitops` — shows all wiring files

With KEDA enabled:
- [ ] `kubectl get scaledobjects -n gitops` — `gitea-runner-scaler` Active

With AWS enabled:
- [ ] `kubectl get providers.pkg.crossplane.io` — Healthy after ~3min
- [ ] ArgoCD `crossplane-buckets` Application — Synced/Healthy