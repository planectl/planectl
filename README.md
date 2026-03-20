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
helm repo add planectl https://planectl.github.io/planectl
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
  argocd-repo-secret.yaml              ArgoCD → Gitea (repo credentials)
  argocd-applications.yaml             ArgoCD watches pulumi/stacks in Gitea
  argocd-app-crossplane-buckets.yaml   ArgoCD watches crossplane/buckets (aws only)
  runner-token-secret.yaml             Gitea runner registration token
  pulumi-stack.yaml                    Pulumi Stack CR
  pulumi-program.yaml                  Pulumi Program CR (inline — no external git)
  keda-runner-scaledobject.yaml        KEDA scales runner on queue depth (keda only)
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
