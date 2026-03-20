# planectl

**planectl** is a single `helm install` that stands up a full GitOps platform on any Kubernetes cluster — a git server (Gitea), a CD engine (ArgoCD), a cloud resource controller (Crossplane), and a Pulumi operator, all pre-wired and ready to use.

You push infrastructure code to Gitea. A Gitea Actions runner picks it up, ArgoCD syncs it into the cluster, Crossplane provisions real cloud resources, and Pulumi manages the stack state — all from one command.

It is designed as a live demo environment for talks and workshops: everything runs locally on Docker Desktop but the same chart deploys to any cluster.

---

## Install

### Prerequisites

- A running Kubernetes cluster (Docker Desktop, kind, EKS, AKS, ...)
- `helm` and `kubectl` on your PATH, pointing at the target cluster

### One-command bootstrap

```bash
helm repo add planectl https://drulacosmin.github.io/planectl
helm repo update planectl
helm pull planectl/planectl --untar
bash ./planectl/bootstrap.sh
```

The script will prompt for:

| Prompt | Default | Notes |
|---|---|---|
| kubectl context | current context | |
| Control plane API | detected from kubeconfig | Auto-patched for Docker Desktop (`127.0.0.1` → `kubernetes.docker.internal`) |
| Namespace | `gitops` | |
| Host | `localhost` | Use a real hostname or IP for remote clusters |
| Gitea admin password | `admin123` | |
| AWS credentials | *(skip)* | Add later with `helm upgrade --set aws.enabled=true ...` |

### Access

| Service | URL | Credentials |
|---|---|---|
| Gitea | `http://<host>:30080` | `admin` / your password |
| ArgoCD | `http://<host>:8080` | `admin` / see below |
| Terminal | `http://<host>:4000` | — |

ArgoCD initial password:
```bash
kubectl get secret -n gitops argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d
```

### Enable AWS (Crossplane S3 workflow)

```bash
helm upgrade planectl planectl/planectl -n gitops \
  --set aws.enabled=true \
  --set aws.accessKeyId=<KEY> \
  --set aws.secretAccessKey=<SECRET>
```

### Teardown

```bash
helm uninstall planectl -n gitops
```

---

## What gets installed

```
Gitea          — git server + Actions CI runner
ArgoCD         — GitOps CD engine
Crossplane     — Kubernetes-native cloud resource controller
Pulumi Operator — manages Pulumi stacks as Kubernetes CRs
Terminal       — browser-based kubectl terminal pre-configured for the cluster
```

All connections between these components (ArgoCD repo credentials, runner tokens, Pulumi stack definitions) live under `wiring/` as plain YAML and are applied by a post-install job. Inspect them at any time:

```bash
kubectl describe cm planectl-wiring -n gitops
```
