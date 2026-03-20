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
  5. Obtain runner registration token from Gitea API
  6. Rotate ArgoCD API token (Gitea API)
  7. Write runner token + ArgoCD token to planectl-wiring-tokens k8s Secret
     (the wiring Job reads this Secret and applies all wiring YAML)
"""

import base64
import os
import sys
import time

import httpx

# ── Configuration (injected via env from Helm values) ─────────────────────────

GITEA_URL         = os.environ.get("GITEA_URL", "http://localhost:3000")
GITEA_EXT_URL     = os.environ.get("GITEA_EXT_URL", "http://localhost:30080")
GITEA_USER        = os.environ.get("GITEA_ADMIN_USER", "admin")
GITEA_PASS        = os.environ.get("GITEA_ADMIN_PASS", "admin123")
DEMO_REPO         = os.environ.get("DEMO_REPO", "demo-repo")
GITEA_NS          = os.environ.get("GITEA_NS", "default")
GITEA_CLUSTER_URL = os.environ.get("GITEA_CLUSTER_URL", GITEA_URL)

KUBECONFIG_B64    = os.environ.get("KUBECONFIG_B64", "")
AWS_KEY_ID        = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET        = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION        = os.environ.get("AWS_REGION", "eu-west-1")

SCRIPTS_DIR       = "/scripts"

# ── In-cluster Kubernetes API (raw httpx, no python-kubernetes) ───────────────

_K8S_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_K8S_CA    = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_K8S_API   = "https://kubernetes.default.svc"


def _k8s_headers() -> dict:
    with open(_K8S_TOKEN) as f:
        return {"Authorization": f"Bearer {f.read().strip()}"}


def k8s_apply_secret(ns: str, name: str, string_data: dict, labels: dict = None):
    """Create or replace a Secret via the in-cluster Kubernetes API."""
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": ns},
        "stringData": string_data,
    }
    if labels:
        body["metadata"]["labels"] = labels

    headers = _k8s_headers()
    base = f"{_K8S_API}/api/v1/namespaces/{ns}/secrets"
    kw = {"headers": headers, "verify": _K8S_CA, "timeout": 15}

    r = httpx.get(f"{base}/{name}", **kw)
    if r.status_code == 200:
        r = httpx.put(f"{base}/{name}", json=body, **kw)
        log(f"  UPDATED  Secret/{name} in {ns}")
    else:
        r = httpx.post(base, json=body, **kw)
        log(f"  CREATED  Secret/{name} in {ns}")
    r.raise_for_status()


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
        """Create or update a file in the repo via the Contents API."""
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


# ── Push seed files to repo ────────────────────────────────────────────────────

FILE_MAP = [
    # (local path under SCRIPTS_DIR,              repo path)
    ("ci.yaml",                  ".gitea/workflows/ci.yaml"),
    ("crossplane-deploy.yaml",   ".gitea/workflows/crossplane-deploy.yaml"),
    ("bucket-deploy.yaml",       ".gitea/workflows/bucket-deploy.yaml"),
    ("argocd-demo-bucket.yaml",  "crossplane/buckets/argocd-demo-bucket.yaml"),
    ("Pulumi.yaml",              "pulumi/programs/demo/Pulumi.yaml"),
    ("__main__.py",              "pulumi/programs/demo/__main__.py"),
    ("requirements.txt",         "pulumi/programs/demo/requirements.txt"),
]


def push_files(gitea: GiteaClient):
    log("\nPushing seed files to repo...")
    for fname, repo_path in FILE_MAP:
        gitea.upsert_file(repo_path, os.path.join(SCRIPTS_DIR, fname))


# ── Store Gitea Actions secrets ────────────────────────────────────────────────

def store_gitea_secrets(gitea: GiteaClient):
    log("\nStoring Gitea Actions secrets...")
    if KUBECONFIG_B64:
        gitea.set_secret("KUBECONFIG_B64", KUBECONFIG_B64)
        log("  OK    KUBECONFIG_B64")
    else:
        log("  SKIP  KUBECONFIG_B64 (create planectl-kubeconfig secret before install)")
    if AWS_KEY_ID and AWS_SECRET:
        gitea.set_secret("AWS_ACCESS_KEY_ID",     AWS_KEY_ID)
        gitea.set_secret("AWS_SECRET_ACCESS_KEY", AWS_SECRET)
        gitea.set_secret("AWS_REGION",            AWS_REGION)
        log("  OK    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION")
    else:
        log("  SKIP  AWS credentials (not provided)")


# ── Write wiring tokens for the wiring Job ─────────────────────────────────────

def store_wiring_tokens(gitea: GiteaClient):
    """Fetch runner + ArgoCD tokens from Gitea; write to k8s Secret.

    The planectl-wiring Job reads this Secret and applies all wiring YAML.
    """
    log("\nPreparing wiring tokens...")
    repo_url = f"{GITEA_CLUSTER_URL}/{GITEA_USER}/{DEMO_REPO}.git"

    runner_token = gitea.get_runner_token()
    log("  Runner token obtained.")

    argocd_token = gitea.rotate_token("argocd-token", ["read:repository"])
    log("  ArgoCD token rotated.")

    k8s_apply_secret(GITEA_NS, "planectl-wiring-tokens", {
        "RUNNER_TOKEN": runner_token,
        "ARGOCD_TOKEN": argocd_token,
        "REPO_URL":     repo_url,
    })
    log("  planectl-wiring-tokens Secret ready for wiring Job.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log("=== planectl init ===")
    wait_healthy()

    gitea = GiteaClient()
    log("\nSetting up demo repo...")
    gitea.ensure_repo()

    push_files(gitea)
    store_gitea_secrets(gitea)
    store_wiring_tokens(gitea)

    log("\n=== planectl init complete ===")


if __name__ == "__main__":
    main()
