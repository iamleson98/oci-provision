"""Docker operations: spawn/reap ephemeral runner containers, prune, disk info."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any

log = logging.getLogger("docker")

MANAGED_LABEL = "oci-runner.managed=1"


class DockerManager:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._sock_gid: int | None = None

    # ------------------------------------------------------------- plumbing

    def _run(self, args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
        cmd = ["docker", *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log.error("docker %s timed out after %ss", " ".join(args[:3]), timeout)
            return subprocess.CompletedProcess(cmd, 124, "", "timeout")
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            log.warning("docker %s failed: %s", " ".join(args[:4]), err[-1] if err else "unknown")
        return proc

    def healthy(self) -> bool:
        return self._run(["version", "--format", "{{.Server.Version}}"], timeout=10).returncode == 0

    # ------------------------------------------------------------ inventory

    def list_containers(self) -> list[dict[str, Any]] | None:
        """Managed runner containers, or None when docker state is UNKNOWN.

        A failed `docker ps` must NOT look like "zero runners": the
        autoscaler would over-spawn into a storm (observed under heavy
        builds, where containerd's snapshotter usage races with build
        temp-file churn). Callers skip scale decisions on None.
        """
        proc = self._run(["ps", "-a", "--filter", f"label={MANAGED_LABEL}", "--format", "{{json .}}"])
        if proc.returncode != 0:
            # one retry - the snapshotter race is transient
            time.sleep(1)
            proc = self._run(["ps", "-a", "--filter", f"label={MANAGED_LABEL}", "--format", "{{json .}}"])
            if proc.returncode != 0:
                return None
        out: list[dict[str, Any]] = []
        for line in (proc.stdout or "").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        for c in out:
            names = c.get("Names")
            if isinstance(names, list):
                c["name"] = names[0] if names else ""
            else:
                c["name"] = str(names or "")
        return out

    # ---------------------------------------------------------------- spawn

    def _docker_sock_gid(self) -> int | None:
        if self._sock_gid is not None:
            return self._sock_gid
        try:
            self._sock_gid = os.stat("/var/run/docker.sock").st_gid
        except OSError:
            self._sock_gid = None
        return self._sock_gid

    def spawn_runner(self, name: str, scope, registration_token: str) -> bool:
        args = [
            "run", "-d",
            "--name", name,
            "--label", MANAGED_LABEL,
            "--init",
            "--memory", f"{self.cfg.runner_memory_gb}g",
            "--memory-swap", f"{int(self.cfg.runner_memory_gb * 2)}g",
            "-e", f"RUNNER_URL={scope.url}",
            "-e", f"RUNNER_TOKEN={registration_token}",
            "-e", f"RUNNER_LABELS={self.cfg.runner_labels}",
            "-e", f"RUNNER_NAME={name}",
        ]
        if self.cfg.runner_cpus:
            args += ["--cpus", self.cfg.runner_cpus]
        if self.cfg.runner_group:
            args += ["-e", f"RUNNER_GROUP={self.cfg.runner_group}"]
        if self.cfg.mount_docker_sock:
            args += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
            gid = self._docker_sock_gid()
            if gid:
                args += ["--group-add", str(gid)]
        args.append(self.cfg.runner_image)

        proc = self._run(args, timeout=120)
        if proc.returncode != 0:
            log.error("failed to spawn runner %s: %s", name, (proc.stderr or "").strip()[:300])
            return False
        log.info("spawned runner container %s for %s (image=%s mem=%sg)", name, scope.slug,
                 self.cfg.runner_image, self.cfg.runner_memory_gb)
        return True

    # ----------------------------------------------------------------- reap

    def remove_containers(self, names: list[str], force: bool = False) -> None:
        for n in names:
            if not n.startswith(self.cfg.container_prefix):
                log.warning("refusing to remove non-managed container %s", n)
                continue
            args = ["rm"] + (["-f"] if force else []) + ["-v", n]
            if self._run(args).returncode == 0:
                log.info("removed runner container %s", n)

    def stop_containers(self, names: list[str], grace: int = 60) -> None:
        for n in names:
            if not n.startswith(self.cfg.container_prefix):
                continue
            # SIGTERM lets the actions runner cancel the job gracefully
            if self._run(["stop", "-t", str(grace), n], timeout=grace + 60).returncode == 0:
                log.info("stopped runner container %s", n)

    # ---------------------------------------------------------------- prune

    def prune(self) -> list[str]:
        summary: list[str] = []
        ops = [
            (["image", "prune", "-a", "--force", "--filter", "label!=oci-runner.keep"], "images"),
            (["volume", "prune", "--force"], "volumes"),
            (["builder", "prune", "--force"], "build cache"),
            (["network", "prune", "--force"], "networks"),
        ]
        for args, label in ops:
            proc = self._run(args, timeout=300)
            if proc.returncode == 0 and proc.stdout:
                line = proc.stdout.strip().splitlines()[-1]
                summary.append(f"{label}: {line}")
        log.info("prune done: %s", "; ".join(summary) if summary else "nothing to do")
        return summary

    # ----------------------------------------------------------------- misc

    def disk_free_gb(self) -> float | None:
        try:
            st = os.statvfs("/var/lib/docker")
            return round(st.f_bavail * st.f_frsize / 1024**3, 1)
        except OSError:
            return None
