#!/usr/bin/env python3
"""
planectl gitea-init script
==========================
Runs as a post-install Helm hook Job inside the cluster.

Actions performed:
  1. Wait for Gitea API to be healthy
  2. Create demo-repo (idempotent)
  3. Upload workflow + infra files to the repo via Gitea Contents API
  4. Store KUBECONFIG_B64 and optional AWS secrets in repo Actions secrets
  5. Obtain runner registration token; create/update gitea-runner-token k8s Secret
  6. Create/update ArgoCD repo Secret (gitea-demo-repo in argocd namespace)
  7. Create/update ArgoCD Application CRs:
       - crossplane-buckets  (path: crossplane/buckets)
       - pulumi-stacks       (path: pulumi/stacks)
"""

import base64
import os
import sys
import time

import httpx
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

# ── Configuration (injected via env from Helm values) ─────────────────────────

GITEA_URL       = os.environ.get("GITEA_URL", "http://localhost:3000")
GITEA_EXT_URL   = os.environ.get("GITEA_EXT_URL", "http://localhost:30080")
GITEA_USER      = os.environ.get("GITEA_ADMIN_USER", "admin")
GITEA_PASS      = os.environ.get("GITEA_ADMIN_PASS", "admin123")
DEMO_REPO       = os.environ.get("DEMO_REPO", "demo-repo")
GITEA_NS        = os.environ.get("GITEA_NS", "default")
ARGOCD_NS       = os.environ.get("ARGOCD_NS", "default")
PULUMI_NS       = os.environ.get("PULUMI_NS", "default")
GITEA_CLUSTER_URL = os.environ.get("GITEA_CLUSTER_URL", GITEA_URL)

KUBECONFIG_B64  = os.environ.get("KUBECONFIG_B64", "")
AWS_KEY_ID      = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET      = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION      = os.environ.get("AWS_REGION", "eu-west-1")

SCRIPTS_DIR     = "/scripts"

# ── Helpers ────────────────────────────────────────────────────────────────────

def b64e(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def log(msg: str):
    print(msg, flush=True)


# ── Gitea client ───────────────────────────────────────────────────────────────

class GiteaClient:
    def __init__(self):
        self.base = GITEA_URL.rstrip("/")
        self.auth = (GITEA_USER, GITEA_PASS)

    def _get(self, path, **kw):
        return httpx.get(f"{self.base}{path}", auth=self.auth, timeout=15, **kw)

    def _post(self, path, **kw):
        return httpx.post(f"{self.base}{path}", auth=self.auth, timeout=15, **kw)

    def _put(self, path, **kw):
        return httpx.put(f"{self.base}{path}", auth=self.auth, timeout=15, **kw)

    def _delete(self, path, **kw):
        return httpx.delete(f"{self.base}{path}", auth=self.auth, timeout=15, **kw)

    # ── Repo ──────────────────────────────────────────────────────────────────

    def ensure_repo(self):
        r = self._get(f"/api/v1/repos/{GITEA_USER}/{DEMO_REPO}")
        if r.status_code == 200:
            log(f"  Repo '{DEMO_REPO}' already exists.")
            return
        r = self._post("/api/v1/user/repos", json={
            "name": DEMO_REPO,
            "private": False,
            "auto_init": True,
            "default_branch": "main",
        })
        r.raise_for_status()
        log(f"  Repo '{DEMO_REPO}' created.")
        time.sleep(2)  # allow Gitea to finish the init commit

    def upsert_file(self, repo_path: str, local_path: str):
        """Create or update a file in the repo via the Contents API.

        Gitea API:
          POST .../contents/{path}  — create (no sha)
          PUT  .../contents/{path}  — update (requires sha of existing blob)
        """
        if not os.path.exists(local_path):
            log(f"  SKIP  {local_path} not found")
            return
        with open(local_path) as fh:
            content = fh.read()
        encoded = b64e(content)
        url = f"/api/v1/repos/{GITEA_USER}/{DEMO_REPO}/contents/{repo_path}"
        existing = self._get(url)
        if existing.status_code == 200:
            sha = existing.json()["sha"]
            r = self._put(url, json={"message": f"chore: update {repo_path}", "content": encoded, "sha": sha})
        else:
            # File does not exist yet — use POST to create
            r = self._post(url, json={"message": f"chore: add {repo_path}", "content": encoded})
        r.raise_for_status()
        log(f"  OK    {repo_path}")

    # ── Secrets ───────────────────────────────────────────────────────────────

    def set_secret(self, key: str, value: str):
        r = self._put(
            f"/api/v1/repos/{GITEA_USER}/{DEMO_REPO}/actions/secrets/{key}",
            json={"data": value},
        )
        r.raise_for_status()

    # ── Runner token ──────────────────────────────────────────────────────────

    def get_runner_token(self) -> str:
        r = self._get("/api/v1/admin/runners/registration-token")
        r.raise_for_status()
        return r.json()["token"]

    # ── API tokens ────────────────────────────────────────────────────────────

    def rotate_token(self, name: str, scopes: list) -> str:
        """Delete any existing token with `name` then create a fresh one."""
        tokens_r = self._get(f"/api/v1/users/{GITEA_USER}/tokens")
        if tokens_r.status_code == 200:
            for t in tokens_r.json():
                if t["name"] == name:
                    self._delete(f"/api/v1/users/{GITEA_USER}/tokens/{t['id']}")
                    break
        r = self._post(f"/api/v1/users/{GITEA_USER}/tokens",
                       json={"name": name, "scopes": scopes})
        r.raise_for_status()
        return r.json()["sha1"]


# ── Wait for Gitea ─────────────────────────────────────────────────────────────

def wait_healthy(max_s: int = 300):
    log("Waiting for Gitea API...")
    elapsed = 0
    while elapsed < max_s:
        try:
            r = httpx.get(f"{GITEA_URL}/api/healthz", timeout=5)
            if r.status_code == 200:
                log("  Gitea is healthy.")
                return
        except Exception:
            pass
        time.sleep(5)
        elapsed += 5
        log(f"  [{elapsed}s] not yet ready...")
    sys.exit(f"FATAL: Gitea not healthy after {max_s}s")


# ── Push repo files ────────────────────────────────────────────────────────────

FILE_MAP = [
    # (local path under SCRIPTS_DIR,              repo path)
    ("ci.yaml",                  ".gitea/workflows/ci.yaml"),
    ("crossplane-deploy.yaml",   ".gitea/workflows/crossplane-deploy.yaml"),
    ("bucket-deploy.yaml",       ".gitea/workflows/bucket-deploy.yaml"),
    ("argocd-demo-bucket.yaml",  "crossplane/buckets/argocd-demo-bucket.yaml"),
    ("demo-stack.yaml",          "pulumi/stacks/demo-stack.yaml"),
    ("demo-program.yaml",        "pulumi/stacks/demo-program.yaml"),
    ("Pulumi.yaml",              "pulumi/programs/demo/Pulumi.yaml"),
    ("__main__.py",              "pulumi/programs/demo/__main__.py"),
    ("requirements.txt",         "pulumi/programs/demo/requirements.txt"),
]


def push_files(gitea: GiteaClient):
    log("\nPushing files to repo...")
    for fname, repo_path in FILE_MAP:
        gitea.upsert_file(repo_path, os.path.join(SCRIPTS_DIR, fname))


# ── Store Action secrets ───────────────────────────────────────────────────────

def store_secrets(gitea: GiteaClient):
    log("\nStoring Gitea Actions secrets...")
    if KUBECONFIG_B64:
        gitea.set_secret("KUBECONFIG_B64", KUBECONFIG_B64)
        log("  OK    KUBECONFIG_B64")
    else:
        log("  SKIP  KUBECONFIG_B64 (not provided -- set init.kubeconfigB64 in values)")
    if AWS_KEY_ID and AWS_SECRET:
        gitea.set_secret("AWS_ACCESS_KEY_ID",     AWS_KEY_ID)
        gitea.set_secret("AWS_SECRET_ACCESS_KEY", AWS_SECRET)
        gitea.set_secret("AWS_REGION",            AWS_REGION)
        log("  OK    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION")
    else:
        log("  SKIP  AWS credentials (not provided)")


# ── Kubernetes wiring ──────────────────────────────────────────────────────────

def _load_k8s():
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    return k8s_client.ApiClient()


def _apply_secret(v1: k8s_client.CoreV1Api, ns: str, secret: k8s_client.V1Secret):
    name = secret.metadata.name
    try:
        v1.create_namespaced_secret(ns, secret)
        log(f"  CREATED  Secret/{name} in {ns}")
    except k8s_client.ApiException as e:
        if e.status == 409:
            v1.replace_namespaced_secret(name, ns, secret)
            log(f"  UPDATED  Secret/{name} in {ns}")
        else:
            raise


def _apply_argocd_app(coa: k8s_client.CustomObjectsApi, ns: str, body: dict):
    name = body["metadata"]["name"]
    group, version, plural = "argoproj.io", "v1alpha1", "applications"
    try:
        coa.create_namespaced_custom_object(group, version, ns, plural, body)
        log(f"  CREATED  Application/{name} in {ns}")
    except k8s_client.ApiException as e:
        if e.status == 409:
            existing = coa.get_namespaced_custom_object(group, version, ns, plural, name)
            body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
            coa.replace_namespaced_custom_object(group, version, ns, plural, name, body)
            log(f"  UPDATED  Application/{name} in {ns}")
        else:
            raise


def wire_k8s(gitea: GiteaClient):
    log("\nWiring Kubernetes resources...")
    api = _load_k8s()
    v1  = k8s_client.CoreV1Api(api)
    coa = k8s_client.CustomObjectsApi(api)

    # 1. Runner registration token secret
    runner_token = gitea.get_runner_token()
    log(f"  Runner token obtained.")
    _apply_secret(v1, GITEA_NS, k8s_client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=k8s_client.V1ObjectMeta(name="gitea-runner-token", namespace=GITEA_NS),
        string_data={"token": runner_token},
    ))

    # 2. ArgoCD repository secret (rotates token on each run)
    repo_url = f"{GITEA_CLUSTER_URL}/{GITEA_USER}/{DEMO_REPO}.git"
    argocd_token = gitea.rotate_token("argocd-token", ["read:repository"])
    _apply_secret(v1, ARGOCD_NS, k8s_client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=k8s_client.V1ObjectMeta(
            name="gitea-demo-repo",
            namespace=ARGOCD_NS,
            labels={"argocd.argoproj.io/secret-type": "repository"},
        ),
        data={
            "type":     b64e("git"),
            "url":      b64e(repo_url),
            "username": b64e(GITEA_USER),
            "password": b64e(argocd_token),
        },
    ))

    # 3. ArgoCD Application: crossplane-buckets
    _apply_argocd_app(coa, ARGOCD_NS, {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "crossplane-buckets", "namespace": ARGOCD_NS},
        "spec": {
            "project": "default",
            "source": {"repoURL": repo_url, "targetRevision": "main", "path": "crossplane/buckets"},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "default"},
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": ["CreateNamespace=true"],
            },
        },
    })

    # 4. ArgoCD Application: pulumi-stacks
    _apply_argocd_app(coa, ARGOCD_NS, {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "pulumi-stacks", "namespace": ARGOCD_NS},
        "spec": {
            "project": "default",
            "source": {"repoURL": repo_url, "targetRevision": "main", "path": "pulumi/stacks"},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": PULUMI_NS},
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": ["CreateNamespace=true"],
            },
        },
    })


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log("=== planectl init ===")
    wait_healthy()

    gitea = GiteaClient()
    log("\nSetting up demo repo...")
    gitea.ensure_repo()

    push_files(gitea)
    store_secrets(gitea)
    wire_k8s(gitea)

    log("\n=== planectl init complete ===")


if __name__ == "__main__":
    main()
