"""GitHub API client: auth (PAT or GitHub App), runner registration tokens,
runner inventory and queued-job introspection for autoscaling."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

try:
    import jwt  # PyJWT
except ImportError:  # only needed for app auth
    jwt = None

log = logging.getLogger("github")

API = "https://api.github.com"
COMMON_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "oci-github-runner-controller",
}
MAX_PAGES = 5
# Queue deep-scan: cache TTL and max runs inspected per scope per scan.
QUEUE_SCAN_TTL = 30
MAX_QUEUE_RUNS = 12


class GitHubClient:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.packages.urllib3.util.retry.Retry(
                total=2, backoff_factor=0.5, status_forcelist=(502, 503, 504), allowed_methods=None
            )
        )
        self.session.mount("https://", adapter)
        self._token: str | None = None
        self._token_exp: datetime | None = None
        self._token_mode = cfg.auth_mode
        self._reg_tokens: dict[str, tuple[str, datetime]] = {}
        self._queue_cache: dict[str, tuple[float, int]] = {}

    # ------------------------------------------------------------------ auth

    def _get_token(self) -> str:
        if self._token_mode == "pat":
            return self.cfg.github_pat
        if self._token and self._token_exp and datetime.now(timezone.utc) < self._token_exp - timedelta(minutes=5):
            return self._token
        if jwt is None:
            raise RuntimeError("PyJWT is required for AUTH_MODE=app")
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self.cfg.gh_app_id}
        app_jwt = jwt.encode(payload, self.cfg.gh_app_private_key, algorithm="RS256")
        resp = self.session.post(
            f"{API}/app/installations/{self.cfg.gh_installation_id}/access_tokens",
            headers={**COMMON_HEADERS, "Authorization": f"Bearer {app_jwt}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["token"]
        self._token_exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        log.info("installation token refreshed (expires %s)", data["expires_at"])
        return self._token

    def _headers(self) -> dict[str, str]:
        token = self._get_token()
        prefix = "Bearer" if self._token_mode == "app" else "token"
        return {**COMMON_HEADERS, "Authorization": f"{prefix} {token}"}

    def invalidate_auth(self) -> None:
        """Force a token refresh on the next call (after a 401)."""
        self._token = None

    def check_auth(self) -> None:
        """Cheap call that verifies the token actually works. Raises on failure."""
        if self.cfg.scopes:
            scope = self.cfg.scopes[0]
            url = f"{API}/repos/{scope.owner}/{scope.name}" if scope.kind == "repo" else f"{API}/orgs/{scope.name}"
        else:  # pragma: no cover
            url = f"{API}/user"
        code, _ = self._request("GET", url)
        if code == 401:
            raise SystemExit(
                "[github] authentication failed (401). Check GITHUB_PAT / GitHub App credentials."
            )
        if code not in (200, 202):
            log.warning("auth check returned HTTP %s for %s", code, url)

    # --------------------------------------------------------------- plumbing

    def _request(self, method: str, url: str, **kw: Any) -> tuple[int, Any]:
        kw.setdefault("timeout", 20)
        resp = self.session.request(method, url, headers=self._headers(), **kw)
        body = None
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, body

    def _get(self, url: str, params: dict | None = None) -> tuple[int, Any]:
        return self._request("GET", url, params=params)

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        """Paginate endpoints that return {workflow_jobs|runners: [...]}."""
        items: list[dict] = []
        params = dict(params or {})
        params["per_page"] = 100
        for _ in range(MAX_PAGES):
            code, body = self._get(url, params)
            if code == 401:
                self.invalidate_auth()
                code, body = self._get(url, params)
            if code == 404:
                return items
            if code != 200:
                log.warning("GET %s -> HTTP %s", url, code)
                return items
            page: list[dict] = []
            if isinstance(body, dict):
                page = body.get("workflow_jobs") or body.get("runners") or []
            items.extend(page)
            if len(page) < 100:
                break
            params["page"] = params.get("page", 1) + 1
        return items

    # ------------------------------------------------------------------ API

    def registration_token(self, scope) -> str:
        """Runner registration token (valid ~1 h), cached for 45 min per scope."""
        key = f"{scope.kind}:{scope.owner}/{scope.name}"
        tok, exp = self._reg_tokens.get(key, ("", datetime.min.replace(tzinfo=timezone.utc)))
        if tok and datetime.now(timezone.utc) < exp - timedelta(minutes=15):
            return tok
        url = (
            f"{API}/orgs/{scope.name}/actions/runners/registration-token"
            if scope.kind == "org"
            else f"{API}/repos/{scope.owner}/{scope.name}/actions/runners/registration-token"
        )
        code, body = self._request("POST", url)
        if code == 401:
            self.invalidate_auth()
            code, body = self._request("POST", url)
        if code != 201:
            raise RuntimeError(f"registration token for {key} failed: HTTP {code} {body}")
        token = body["token"]
        exp = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        self._reg_tokens[key] = (token, exp)
        log.info("registration token refreshed for %s (expires %s)", key, body["expires_at"])
        return token

    def list_runners(self, scope) -> list[dict]:
        url = (
            f"{API}/orgs/{scope.name}/actions/runners"
            if scope.kind == "org"
            else f"{API}/repos/{scope.owner}/{scope.name}/actions/runners"
        )
        return self._paginate(url)

    def delete_runner(self, scope, runner_id: int) -> bool:
        url = (
            f"{API}/orgs/{scope.name}/actions/runners/{runner_id}"
            if scope.kind == "org"
            else f"{API}/repos/{scope.owner}/{scope.name}/actions/runners/{runner_id}"
        )
        code, _ = self._request("DELETE", url)
        if code == 401:
            self.invalidate_auth()
            code, _ = self._request("DELETE", url)
        ok = code in (202, 204)
        if not ok:
            log.warning("delete runner %s -> HTTP %s", runner_id, code)
        return ok

    def queued_jobs(self, scope) -> int:
        """Number of queued jobs that OUR labels can serve.

        The repo-wide workflow-jobs endpoint (/actions/workflow-jobs) returns
        404 for these tokens, so we enumerate queued AND in_progress runs and
        fetch each run's queued jobs (an in_progress run still holds queued
        jobs waiting for a runner - counting only fully-queued runs starves
        multi-job workflows). Results are cached per scope for QUEUE_SCAN_TTL
        seconds to respect rate limits.

        Org scopes without a monitored repo list return 0 (org-wide queue
        introspection is not exposed by the REST API).
        """
        if scope.kind != "repo":
            return 0
        key = f"{scope.kind}:{scope.owner}/{scope.name}"
        now = time.time()
        cached = self._queue_cache.get(key)
        if cached and now - cached[0] < QUEUE_SCAN_TTL:
            return cached[1]

        match = set(self.cfg.match_labels)
        count = 0
        run_ids: list[int] = []
        for status in ("queued", "in_progress"):
            code, body = self._get(
                f"{API}/repos/{scope.owner}/{scope.name}/actions/runs",
                {"status": status, "per_page": 10},
            )
            if code == 200 and isinstance(body, dict):
                run_ids.extend(r["id"] for r in body.get("workflow_runs", []))
        # Cap the deep scan (rate-limit hygiene); newest runs come first.
        for rid in run_ids[:MAX_QUEUE_RUNS]:
            jobs = self._paginate(
                f"{API}/repos/{scope.owner}/{scope.name}/actions/runs/{rid}/jobs",
                {"status": "queued"},
            )
            count += sum(1 for j in jobs if match.intersection(j.get("labels") or []))
        self._queue_cache[key] = (now, count)
        if run_ids:
            log.info("queue scan %s: %d label-matched queued job(s)", key, count)
        return count
