"""Persistence and workflow dispatch helpers for GitHub-backed price history."""
import base64
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "pako17p-cmd/monitor-cen-hoteli")
BRANCH = os.environ.get("HISTORY_BRANCH", "price-history")
STATE_FILE = "history.json.gz"
API_ROOT = "https://api.github.com"


def _request(method, path, payload=None):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Brak GITHUB_TOKEN w konfiguracji Rendera.")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        API_ROOT + path,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "monitor-cen-hoteli",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            return 404, None
        if exc.code == 409 or exc.code == 422:
            return exc.code, raw
        raise RuntimeError(f"GitHub API zwróciło {exc.code}: {raw[:300]}") from exc


def _repo_path(path):
    return f"/repos/{REPOSITORY}/{path}"


def _ensure_branch():
    ref_path = _repo_path(f"git/ref/heads/{urllib.parse.quote(BRANCH, safe='')}")
    status, _ = _request("GET", ref_path)
    if status == 200:
        return
    default_status, default_ref = _request("GET", _repo_path("git/ref/heads/main"))
    if default_status != 200:
        raise RuntimeError("Nie udało się znaleźć gałęzi main w repozytorium.")
    create_status, _ = _request("POST", _repo_path("git/refs"), {
        "ref": f"refs/heads/{BRANCH}",
        "sha": default_ref["object"]["sha"],
    })
    if create_status not in (201, 422):
        raise RuntimeError("Nie udało się utworzyć gałęzi historii cen.")


def read_state():
    """Return the state stored in history.json, or None before first save."""
    path = _repo_path(f"contents/{STATE_FILE}?ref={urllib.parse.quote(BRANCH, safe='')}")
    status, result = _request("GET", path)
    if status == 404:
        return None
    try:
        content = gzip.decompress(base64.b64decode(result["content"])).decode("utf-8")
        return json.loads(content)
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("Plik historii na gałęzi GitHub ma nieprawidłowy format.") from exc


def write_state(state):
    """Create/update the state file on the dedicated history branch."""
    _ensure_branch()
    path = _repo_path(f"contents/{STATE_FILE}")
    content = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.b64encode(gzip.compress(content, compresslevel=9)).decode("ascii")
    for attempt in range(3):
        status, existing = _request("GET", path + "?ref=" + urllib.parse.quote(BRANCH, safe=''))
        payload = {
            "message": "Update hotel price history",
            "content": encoded,
            "branch": BRANCH,
        }
        if status == 200:
            payload["sha"] = existing["sha"]
        result, _ = _request("PUT", path, payload)
        if result in (200, 201):
            return
        if result not in (409, 422) or attempt == 2:
            raise RuntimeError("Nie udało się zapisać historii cen w GitHubie.")
        time.sleep(1 + attempt)


def dispatch_price_check():
    """Ask GitHub Actions to perform a check immediately."""
    status, _ = _request("POST", _repo_path("actions/workflows/check-prices.yml/dispatches"), {"ref": "main"})
    if status not in (204, 200):
        raise RuntimeError("GitHub nie przyjął zlecenia sprawdzenia cen.")

