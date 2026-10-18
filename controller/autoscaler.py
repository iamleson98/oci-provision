"""Queue-aware autoscaler for ephemeral GitHub Actions runner containers.

Reconcile loop (every POLL_INTERVAL seconds):

  1. Ask GitHub how many queued jobs match our labels, per scope.
  2. Ask GitHub which of OUR runners are online/busy.
  3. Ask Docker which managed runner containers exist.
  4. desired = clamp(busy + queued, MIN_RUNNERS, MAX_RUNNERS)
     - spawn containers up to desired (weighted toward scopes with deeper queues)
     - stop+remove idle containers beyond desired (never touching busy ones)
  5. Housekeeping: reap exited containers, deregister stale offline runners,
     guard against spawn-fail storms with a cooldown.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("autoscaler")

EXITED_REAP_AFTER_S = 15 * 60
OFFLINE_DEREG_AFTER_S = 20 * 60
REGISTRATION_GRACE_S = 180  # never kill a container younger than this
SPAWN_FAIL_WINDOW_S = 600
SPAWN_FAIL_MAX = 3
SPAWN_COOLDOWN_S = 300


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


class Autoscaler:
    def __init__(self, cfg, gh, dm) -> None:
        self.cfg = cfg
        self.gh = gh
        self.dm = dm

        self.events: deque[tuple[float, str]] = deque(maxlen=120)
        self.state: dict = {
            "healthy": False,
            "queued": {},
            "runners_online": 0,
            "runners_busy": 0,
            "containers_running": 0,
            "containers_exited": 0,
            "desired": 0,
            "min_runners": cfg.min_runners,
            "max_runners": cfg.max_runners,
            "disk_free_gb": None,
            "last_tick": None,
            "idle_since": None,
            "spawn_paused_until": 0,
            "busy_names": [],
            "auth_mode": cfg.auth_mode,
        }
        self._spawn_failures: deque[float] = deque()
        self._offline_seen: dict[str, float] = {}
        self._spawn_ts: dict[str, float] = {}
        self._last_activity: float = _now()

    # ------------------------------------------------------------------ utils

    def event(self, msg: str) -> None:
        self.events.append((_now(), msg))
        log.info("%s", msg)

    def snapshot(self) -> dict:
        return {
            **self.state,
            "events": [{"ts": _iso(ts), "msg": msg} for ts, msg in list(self.events)[-25:]],
        }

    def _is_ours(self, runner: dict) -> bool:
        return str(runner.get("name", "")).startswith(self.cfg.container_prefix)

    # ------------------------------------------------------------------- tick

    def tick(self) -> None:
        cfg = self.cfg

        queued: dict[str, int] = {}
        gh_runners: list[dict] = []
        for scope in cfg.scopes:
            try:
                queued[scope.slug] = self.gh.queued_jobs(scope)
            except Exception as exc:  # noqa: BLE001 - keep polling other scopes
                log.warning("queue poll failed for %s: %s", scope.slug, exc)
                queued[scope.slug] = 0
            try:
                gh_runners.extend(self.gh.list_runners(scope))
            except Exception as exc:  # noqa: BLE001
                log.warning("runner list failed for %s: %s", scope.slug, exc)

        ours = [r for r in gh_runners if self._is_ours(r)]
        busy_names = [r["name"] for r in ours if r.get("busy") and r.get("status") == "online"]
        offline_names = {r["name"] for r in ours if r.get("status") == "offline"}

        containers = self.dm.list_containers()
        running = [c for c in containers if c.get("State") == "running"]
        exited = [c for c in containers if c.get("State") == "exited"]
        running_names = {c["name"] for c in running}
        exited_names = {c["name"] for c in exited}

        total_queued = sum(queued.values())
        desired = min(cfg.max_runners, max(cfg.min_runners, len(busy_names) + total_queued))

        # ------------------------------------------------------------ scale up
        if _now() < self.state.get("spawn_paused_until", 0):
            log.debug("spawn paused (cooldown)")
        elif len(running) < desired:
            self._scale_up(running, desired, queued)

        # ---------------------------------------------------------- scale down
        elif len(running) > desired:
            self._scale_down(running, busy_names, desired)

        # --------------------------------------------------------- housekeeping
        self._reap_exited(exited_names)
        self._deregister_stale(offline_names, running_names)

        # ------------------------------------------------------------ idle bookkeeping
        active = total_queued > 0 or len(busy_names) > 0 or len(running) > 0
        if active:
            self._last_activity = _now()
            self.state["idle_since"] = None
        elif self.state["idle_since"] is None:
            self.state["idle_since"] = _now()

        # -------------------------------------------------------------- publish
        self.state.update(
            healthy=True,
            queued=queued,
            runners_online=len([r for r in ours if r.get("status") == "online"]),
            runners_busy=len(busy_names),
            containers_running=len(running),
            containers_exited=len(exited),
            desired=desired,
            disk_free_gb=self.dm.disk_free_gb(),
            last_tick=_iso(_now()),
            busy_names=busy_names[:20],
        )

    # ------------------------------------------------------------- scale up

    def _scale_up(self, running: list[dict], desired: int, queued: dict[str, int]) -> None:
        cfg = self.cfg
        deficit = desired - len(running)
        # Weight spawns toward the scopes with the deepest queues; idle runners
        # (queued=0 scopes) only serve as MIN fill.
        weights: dict[str, int] = {}
        for scope in cfg.scopes:
            weights[scope.slug] = queued.get(scope.slug, 0)
        existing = {c["name"] for c in running}
        spawned = 0
        for _ in range(deficit):
            scope = self._pick_scope(weights)
            name = f"{cfg.container_prefix}-{scope.slug}-{uuid.uuid4().hex[:8]}"
            try:
                token = self.gh.registration_token(scope)
            except Exception as exc:  # noqa: BLE001
                self.event(f"registration token failed ({exc}); will retry next tick")
                self._note_spawn_failure()
                break
            if not self.dm.spawn_runner(name, scope, token):
                self._note_spawn_failure()
                break
            self._spawn_ts[name] = _now()
            spawned += 1
            if weights.get(scope.slug, 0) > 0:
                weights[scope.slug] -= 1
            if name in existing:
                break
        if spawned:
            self.event(
                f"scaled up: +{spawned} runner(s), running={len(running) + spawned}, desired={desired}"
            )

    def _pick_scope(self, weights: dict[str, int]):
        scopes = self.cfg.scopes
        best = max(scopes, key=lambda s: weights.get(s.slug, 0))
        return best

    def _note_spawn_failure(self) -> None:
        now = _now()
        self._spawn_failures.append(now)
        while self._spawn_failures and now - self._spawn_failures[0] > SPAWN_FAIL_WINDOW_S:
            self._spawn_failures.popleft()
        if len(self._spawn_failures) >= SPAWN_FAIL_MAX:
            self.state["spawn_paused_until"] = now + SPAWN_COOLDOWN_S
            self._spawn_failures.clear()
            self.event(
                f"spawn failures detected -> pausing spawns for {SPAWN_COOLDOWN_S}s "
                "(check GITHUB_* credentials / RUNNER_IMAGE)"
            )

    # ----------------------------------------------------------- scale down

    def _scale_down(self, running: list[dict], busy_names: list[str], desired: int) -> None:
        cfg = self.cfg
        busy = set(busy_names)
        candidates = [
            c for c in running
            if c["name"] not in busy
            and _now() - self._spawn_ts.get(c["name"], 0) > REGISTRATION_GRACE_S
        ]
        # Newest spawned first (they are the most likely to be surplus warm
        # runners); never touch busy runners or freshly spawned ones.
        candidates.sort(key=lambda c: self._spawn_ts.get(c["name"], 0), reverse=True)
        excess = len(running) - desired
        victims = [c["name"] for c in candidates[:excess]]
        if victims:
            self.dm.stop_containers(victims, grace=30)
            self.dm.remove_containers(victims, force=True)
            for n in victims:
                self._spawn_ts.pop(n, None)
            self.event(f"scaled down: -{len(victims)} idle runner(s), running={len(running) - len(victims)}, desired={desired}")

    # -------------------------------------------------------- housekeeping

    def _reap_exited(self, exited_names: set[str]) -> None:
        stale = [
            n for n in exited_names
            if _now() - self._spawn_ts.get(n, 0) > EXITED_REAP_AFTER_S
        ]
        if stale:
            self.dm.remove_containers(stale)
            for n in stale:
                self._spawn_ts.pop(n, None)
            self.event(f"reaped {len(stale)} exited runner container(s)")

    def _deregister_stale(self, offline_names: set[str], running_names: set[str]) -> None:
        now = _now()
        for name in offline_names:
            if name in running_names:
                self._offline_seen.pop(name, None)
                continue
            first = self._offline_seen.setdefault(name, now)
            if now - first > OFFLINE_DEREG_AFTER_S:
                self._deregister_by_name(name)
                self._offline_seen.pop(name, None)
        for name in list(self._offline_seen):
            if name not in offline_names:
                self._offline_seen.pop(name, None)

    def _deregister_by_name(self, name: str) -> None:
        for scope in self.cfg.scopes:
            try:
                runners = self.gh.list_runners(scope)
            except Exception:  # noqa: BLE001
                continue
            for r in runners:
                if r.get("name") == name:
                    if self.gh.delete_runner(scope, r["id"]):
                        self.event(f"deregistered stale offline runner {name}")
                    return

    # ------------------------------------------------------------- idle stop

    def idle_shutdown_ready(self) -> bool:
        """True when the VM may power off (burst profile)."""
        cfg = self.cfg
        if not cfg.idle_stop_enabled:
            return False
        idle_since = self.state.get("idle_since")
        if idle_since is None:
            return False
        idle_minutes = (_now() - idle_since) / 60
        quiet = (
            not any(self.state.get("queued", {}).values())
            and self.state.get("runners_busy", 0) == 0
            and self.state.get("containers_running", 0) == 0
        )
        ready = quiet and idle_minutes >= cfg.idle_stop_minutes
        if ready:
            log.info("idle for %.0f min -> shutdown approved", idle_minutes)
        return ready
