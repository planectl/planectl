# planectl refactor proposal
## from opaque circus to transparent management cluster

---

## the idea in one sentence

`helm install planectl` still works unchanged — but after it runs, every
connection between the four building blocks is a readable YAML file in
`charts/planectl/wiring/`, and the tutorial is the README plus the UIs
that are already running in the browser.

---

## what planectl actually is (after refactor)

A **management cluster** — a small set of vanilla CNCF tools glued
together to orchestrate and observe other, heavier deployments.

```
Gitea          source of truth   (git + Actions CI)
ArgoCD         intent delivery   (watches Gitea, syncs cluster state)
Crossplane     infra control     (cloud resources as Kubernetes objects)
Pulumi Op.     functional ctrl   (Pulumi programs as Kubernetes CRs)
[ KEDA ]       event scaling     (optional, adds to the cockpit later)
```

None of these are patched or wrapped. They install from their public
upstream charts. planectl only provides:

1. the values files that right-size them for a local cluster
2. the wiring YAML that connects them to each other
3. a minimal init Job for the parts that need HTTP API calls (Gitea)
4. the terminal (xterm.js) as a cockpit into the running system

---

## current problems

| problem | where it lives |
|---|---|
| wiring logic is Python inside a Job container | `gitea-init.py` lines 130–220 |
| Kubernetes objects built in Python dicts | `wire_k8s()` function |
| tutorial is a 46KB Node.js app | `files/tutorial/index.html` |
| init Job pulls python:3.12-slim + pip installs on every run | `init-job.yaml` |
| kubeconfig passed through Helm values → stored in Helm release Secret | `--set init.kubeconfigB64=...` |
| runner uses floating `latest` tag | `runner.yaml` |
| aws.enabled=false → ArgoCD Application crossplane-buckets SyncFailed | `gitea-init.py` + `aws.yaml` |

---

## proposed file structure after refactor

```
charts/planectl/
  Chart.yaml
  values.yaml
  charts/                        ← unchanged — upstream chart tarballs
    argo-cd-*.tgz
    crossplane-*.tgz
    gitea-*.tgz
    pulumi-kubernetes-operator-*.tgz
  files/
    seed/                        ← repo seed files (unchanged)
      ci.yaml
      crossplane-deploy.yaml
      bucket-deploy.yaml
      argocd-demo-bucket.yaml
      demo-stack.yaml
      demo-program.yaml
      Pulumi.yaml
      __main__.py
      requirements.txt
    terminal/                    ← replaces tutorial/ — just the cockpit
      server.js                  ← kept, slimmed (remove lesson content)
      package.json
  wiring/                        ← NEW — the pedagogically interesting layer
    argocd-repo-secret.yaml      ← ArgoCD learns where Gitea lives
    argocd-applications.yaml     ← what ArgoCD syncs and where
    crossplane-providerconfig.yaml
    pulumi-stack.yaml
    pulumi-program.yaml
  templates/
    init-configmap.yaml          ← now embeds seed/ files only
    init-job.yaml                ← slimmed (no Kubernetes object creation)
    init-rbac.yaml               ← unchanged
    runner.yaml                  ← pin image tag
    terminal.yaml                ← renamed from tutorial.yaml
    aws.yaml                     ← gate ArgoCD App on aws.enabled
    crossplane-providers.yaml    ← unchanged
    wiring-configmap.yaml        ← NEW — embeds wiring/ YAMLs as data keys
    wiring-job.yaml              ← NEW — kubectl apply -f wiring/ inside cluster
    _helpers.tpl                 ← unchanged
  NOTES.txt                      ← updated access URLs
```

---

## step-by-step refactoring tasks

### step 1 — extract wiring out of Python into YAML files

Create `charts/planectl/wiring/` with these files.

Each file is what a student pastes into a chat and says
*"I want to add X"* or *"explain what this does"*.

**`wiring/argocd-repo-secret.yaml`**
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: gitea-demo-repo
  namespace: ARGOCD_NS
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
data:
  type: Z2l0          # "git"
  url: REPO_URL       # filled by init Job
  username: GITEA_USER
  password: ARGOCD_TOKEN   # filled by init Job (rotated on each run)
```

**`wiring/argocd-applications.yaml`**
```yaml
# crossplane-buckets only created when aws.enabled=true
# pulumi-stacks always created
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pulumi-stacks
  namespace: ARGOCD_NS
spec:
  project: default
  source:
    repoURL: REPO_URL
    targetRevision: main
    path: pulumi/stacks
  destination:
    server: https://kubernetes.default.svc
    namespace: PULUMI_NS
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true]
```

**`wiring/crossplane-providerconfig.yaml`**
```yaml
apiVersion: aws.upbound.io/v1beta1
kind: ProviderConfig
metadata:
  name: default
spec:
  credentials:
    source: Secret
    secretRef:
      namespace: RELEASE_NS
      name: planectl-aws-credentials
      key: credentials
```

**`wiring/pulumi-stack.yaml`** and **`wiring/pulumi-program.yaml`**
— move directly from `files/demo-stack.yaml` and `files/demo-program.yaml`
with no content changes. They are Kubernetes objects, not seed files.

---

### step 2 — add a wiring Job that applies the wiring YAMLs

Add `templates/wiring-configmap.yaml` — embeds all files from `wiring/`
as ConfigMap data keys using `.Files.Glob`.

Add `templates/wiring-job.yaml` — a post-install hook Job that runs
`bitnami/kubectl` (or `alpine/k8s`) and does:

```sh
kubectl apply -f /wiring/argocd-repo-secret.yaml
kubectl apply -f /wiring/argocd-applications.yaml
# conditionally:
if [ "$AWS_ENABLED" = "true" ]; then
  kubectl apply -f /wiring/crossplane-providerconfig.yaml
  kubectl apply -f /wiring/argocd-app-crossplane-buckets.yaml
fi
kubectl apply -f /wiring/pulumi-stack.yaml
kubectl apply -f /wiring/pulumi-program.yaml
```

No Python. No pip install. Image starts in ~2s. Transparent — the shell
script is in the ConfigMap, readable with `kubectl describe cm`.

---

### step 3 — slim the init Job to Gitea API calls only

Remove from `gitea-init.py`:
- `wire_k8s()` function entirely (~80 lines)
- `_apply_secret()` helper
- `_apply_argocd_app()` helper
- `_load_k8s()` helper
- `from kubernetes import ...` import

What remains in the init Job:
```python
wait_healthy()
ensure_repo()
push_seed_files()          # files/seed/* → Gitea Contents API
store_gitea_secrets()      # KUBECONFIG_B64, AWS creds → Gitea Actions secrets
create_runner_token_secret()  # GET runner token → kubectl create secret
```

Dependency drops from `httpx + kubernetes` to `httpx` only.
The Job still uses `python:3.12-slim` but the startup is cleaner.
Optional future: rewrite as a shell script (`curl` + `kubectl`) and drop
Python entirely.

---

### step 4 — fix kubeconfig secret handling

Remove `init.kubeconfigB64` from `values.yaml`.

Add to `NOTES.txt` and README — before `helm install`, the user runs:

```bash
kubectl create namespace gitops
kubectl create secret generic planectl-kubeconfig \
  --namespace gitops \
  --from-literal=kubeconfig="$(
    kubectl config view --minify --flatten \
      | sed 's|127.0.0.1|kubernetes.docker.internal|g'
  )"
```

In the init Job, replace:
```yaml
- name: KUBECONFIG_B64
  value: {{ .Values.init.kubeconfigB64 | quote }}
```
with:
```yaml
- name: KUBECONFIG_B64
  valueFrom:
    secretKeyRef:
      name: planectl-kubeconfig
      key: kubeconfig
      optional: true
```

The kubeconfig never enters the Helm release Secret in etcd.

---

### step 5 — gate crossplane-buckets ArgoCD Application on aws.enabled

In `wiring/argocd-applications.yaml` (or a separate
`wiring/argocd-app-crossplane-buckets.yaml`), the file only gets applied
by the wiring Job when `aws.enabled=true`. This prevents the Application
from being created pointing at a path of Crossplane manifests that
reference a CRD that doesn't exist yet.

Result: fresh install with `aws.enabled=false` → ArgoCD shows one
Application (`pulumi-stacks`), green, synced. No SyncFailed noise.

---

### step 6 — rename tutorial → terminal, remove lesson content

`files/tutorial/` → `files/terminal/`

`server.js` stays — it provides the xterm.js terminal which is the
cockpit into the running system (kubectl, helm, gitea-cli from inside
the cluster). Remove any hard-coded lesson step logic or HTML that
describes the workshop — that moves to the README.

`index.html` — keep the xterm.js shell. Remove the guided lesson panels,
progress steps, and embedded instruction text. The browser terminal is
the tool; the README is the guide.

`templates/tutorial.yaml` → `templates/terminal.yaml`
Values key `tutorial.*` → `terminal.*`

---

### step 7 — pin the runner image tag

In `templates/runner.yaml`, replace:
```yaml
image: gitea/act_runner:latest
imagePullPolicy: Always
```
with:
```yaml
image: gitea/act_runner:0.2.11
imagePullPolicy: IfNotPresent
```

Add `runner.image` and `runner.imageTag` to `values.yaml` so it can be
overridden without editing the template.

---

### step 8 — update README to be the tutorial

The README becomes the primary learning surface. Suggested structure:

```
## what this is
  management cluster — 4 vanilla CNCF tools, wired together

## install
  prerequisites + one-command install

## the four layers
  Layer 1 — Gitea (source of truth)
  Layer 2 — ArgoCD (intent delivery)
  Layer 3 — Crossplane (infra control)
  Layer 4 — Pulumi Operator (functional control)

## how it's wired
  wiring/argocd-repo-secret.yaml      ← Layer 1 → Layer 2
  wiring/argocd-applications.yaml     ← Layer 2 → Layer 3 + 4
  wiring/crossplane-providerconfig.yaml
  wiring/pulumi-stack.yaml

## cockpit (what's running in your browser)
  Gitea:  http://localhost:30080
  ArgoCD: http://localhost:8080
  Terminal: http://localhost:4000

## bending it to your needs
  add a new ArgoCD Application
  add a cloud provider to Crossplane
  add a KEDA ScaledObject
  point it at a remote cluster instead of docker-desktop

## teardown
```

---

## what gets deleted

| file/dir | reason |
|---|---|
| `files/tutorial/index.html` (46KB) | lesson content moves to README |
| `files/tutorial/package.json` | replaced by `files/terminal/package.json` |
| `wire_k8s()` and k8s helpers in `gitea-init.py` | replaced by wiring Job |
| `from kubernetes import ...` in `gitea-init.py` | no longer needed |
| `init.kubeconfigB64` in `values.yaml` | replaced by pre-created Secret |
| `tutorial.enabled` / `tutorial.port` in `values.yaml` | renamed to `terminal.*` |

---

## what does not change

- the install command: `helm install planectl planectl/planectl -n gitops --create-namespace`
- all four upstream subcharts and their versions
- the seed files pushed to Gitea (`ci.yaml`, workflows, Pulumi program)
- the runner deployment and docker.sock mount
- NOTES.txt access URLs
- ArtifactHub metadata

---

## version bump

`0.4.2` → `0.5.0`

Semantic minor because:
- `init.kubeconfigB64` removed (breaking change for anyone using `--set`)
- `tutorial.*` renamed to `terminal.*` in values
- wiring is now visible and overridable

---

## the copilot-navigable outcome

After this refactor, `wiring/` is the answer to
*"show me how the pieces connect"*.

A user can:
```
cat charts/planectl/wiring/argocd-repo-secret.yaml
```
and immediately see that ArgoCD learns about Gitea via a Secret labelled
`argocd.argoproj.io/secret-type: repository`.

Or paste it into a chat:
> *"I want ArgoCD to also watch a second repo at github.com/myorg/infra —
> what do I add?"*

The wiring files are the curriculum. The terminal is the hands-on tool.
The README is the guide. The four vanilla charts are the subject matter.