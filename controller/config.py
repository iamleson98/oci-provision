"""Configuration for the oci-github-runner controller (env-driven)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        raise SystemExit(f"[config] {name}={v!r} is not an integer") from None


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        raise SystemExit(f"[config] {name}={v!r} is not a number") from None


def _split_csv(name: str) -> list[str]:
    v = os.environ.get(name, "")
    return [p.strip() for p in v.split(",") if p.strip()]


@dataclass
class Scope:
    """A GitHub registration scope: one repository or one organization."""

    kind: str  # "repo" | "org"
    owner: str
    name: str

    @property
    def slug(self) -> str:
        base = f"{self.owner}-{self.name}" if self.kind == "repo" else f"org-{self.name}"
        return "".join(c if c.isalnum() else "-" for c in base.lower()).strip("-")

    @property
    def url(self) -> str:
        if self.kind == "org":
            return f"https://github.com/orgs/{self.name}"
        return f"https://github.com/{self.owner}/{self.name}"


@dataclass
class Config:
    # --- GitHub auth ---------------------------------------------------------
    auth_mode: str = "pat"  # pat | app
    github_pat: str = ""
    gh_app_id: str = ""
    gh_installation_id: str = ""
    gh_app_private_key: str = ""  # PEM with literal \n escapes, or a file path

    # --- scopes ----------------------------------------------------------------
    scope_type: str = "repo"  # repo | org
    github_repositories: list[str] = field(default_factory=list)  # ["owner/repo", ...]
    github_organization: str = ""

    # --- runners ---------------------------------------------------------------
    runner_labels: str = "oracle-a1"
    match_labels: list[str] = field(default_factory=lambda: ["oracle-a1"])
    runner_group: str = ""
    min_runners: int = 1
    max_runners: int = 2
    runner_image: str = "oci-runner/runner:latest"
    runner_memory_gb: float = 5.0
    runner_cpus: str = ""  # empty = uncapped
    mount_docker_sock: bool = True
    container_prefix: str = "gha-runner"

    # --- ops ---------------------------------------------------------------------
    poll_interval: int = 15
    housekeep_interval: int = 300
    prune_interval: int = 6 * 3600
    idle_stop_enabled: bool = False
    idle_stop_minutes: int = 20

    # --- status -------------------------------------------------------------------
    status_bind: str = "0.0.0.0"
    status_port: int = 8080
    status_token: str = ""

    # ---------------------------------------------------------------------------

    @property
    def scopes(self) -> list[Scope]:
        out: list[Scope] = []
        if self.scope_type == "org":
            out.append(Scope(kind="org", owner=self.github_organization, name=self.github_organization))
        for full in self.github_repositories:
            if "/" not in full:
                raise SystemExit(f"[config] GITHUB_REPOSITORIES entry {full!r} must look like 'owner/repo'")
            o, r = full.split("/", 1)
            out.append(Scope(kind="repo", owner=o, name=r))
        return out

    def validate(self) -> None:
        errors: list[str] = []
        if self.auth_mode == "pat" and not self.github_pat:
            errors.append("GITHUB_PAT is required for AUTH_MODE=pat")
        if self.auth_mode == "app":
            if not (self.gh_app_id and self.gh_installation_id and self.gh_app_private_key):
                errors.append(
                    "GH_APP_ID, GH_INSTALLATION_ID and GH_APP_PRIVATE_KEY are required for AUTH_MODE=app"
                )
        if self.scope_type == "org" and not self.github_organization:
            errors.append("GITHUB_ORGANIZATION is required when SCOPE_TYPE=org")
        if self.scope_type == "repo" and not self.github_repositories:
            errors.append("GITHUB_REPOSITORIES is required when SCOPE_TYPE=repo (format: owner/repo)")
        if self.min_runners < 0 or self.max_runners < 1 or self.min_runners > self.max_runners:
            errors.append("need 0 <= MIN_RUNNERS <= MAX_RUNNERS with MAX_RUNNERS >= 1")
        if self.runner_memory_gb <= 0:
            errors.append("RUNNER_MEMORY_GB must be > 0")
        if not self.match_labels:
            errors.append("MATCH_LABELS must not be empty")
        if errors:
            raise SystemExit("[config] invalid configuration:\n  - " + "\n  - ".join(errors))


def _load_private_key() -> str:
    raw = os.environ.get("GH_APP_PRIVATE_KEY", "")
    path = os.environ.get("GH_APP_PRIVATE_KEY_FILE", "")
    if raw:
        # Allow both literal \n escapes (docker env_file safe) and real newlines.
        return raw.replace("\\n", "\n")
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return ""


def load_config() -> Config:
    cfg = Config(
        auth_mode=os.environ.get("AUTH_MODE", "pat").strip().lower(),
        github_pat=os.environ.get("GITHUB_PAT", "").strip(),
        gh_app_id=os.environ.get("GH_APP_ID", "").strip(),
        gh_installation_id=os.environ.get("GH_INSTALLATION_ID", "").strip(),
        gh_app_private_key=_load_private_key(),
        scope_type=os.environ.get("SCOPE_TYPE", "repo").strip().lower(),
        github_repositories=_split_csv("GITHUB_REPOSITORIES"),
        github_organization=os.environ.get("GITHUB_ORGANIZATION", "").strip(),
        runner_labels=os.environ.get("RUNNER_LABELS", "oracle-a1"),
        match_labels=_split_csv("MATCH_LABELS") or ["oracle-a1"],
        runner_group=os.environ.get("RUNNER_GROUP", "").strip(),
        min_runners=_int("MIN_RUNNERS", 1),
        max_runners=_int("MAX_RUNNERS", 2),
        runner_image=os.environ.get("RUNNER_IMAGE", "oci-runner/runner:latest"),
        runner_memory_gb=_float("RUNNER_MEMORY_GB", 5.0),
        runner_cpus=os.environ.get("RUNNER_CPUS", "").strip(),
        mount_docker_sock=_bool("MOUNT_DOCKER_SOCK", True),
        container_prefix=os.environ.get("CONTAINER_PREFIX", "gha-runner"),
        poll_interval=max(5, _int("POLL_INTERVAL", 15)),
        housekeep_interval=max(30, _int("HOUSEKEEP_INTERVAL", 300)),
        prune_interval=max(600, _int("PRUNE_INTERVAL", 6 * 3600)),
        idle_stop_enabled=_bool("ENABLE_IDLE_STOP", False),
        idle_stop_minutes=max(3, _int("IDLE_STOP_MINUTES", 20)),
        status_bind=os.environ.get("STATUS_BIND", "0.0.0.0"),
        status_port=_int("STATUS_PORT", 8080),
        status_token=os.environ.get("STATUS_TOKEN", "").strip(),
    )
    cfg.validate()
    return cfg
