"""Waker service: runs on the free AMD E2.1.Micro VM, 24/7.

Responsibilities:
  * Poll GitHub for queued jobs that match our labels.
  * When jobs are waiting and the A1 worker VM is STOPPED -> start it
    (via OCI instance principals; needs the IAM policy Terraform creates).
  * Optional self-healing: ALWAYS_RESUME=true restarts the A1 worker whenever
    it is stopped (covers Oracle idle reclamation, reboots, crashes) - use
    this with the 'always-on' profile. Stop the waker service for maintenance.
  * Serve a 24/7 status page so you can see the fleet without waking the worker.

Environment is documented in deploy/waker.env.example.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

import oci

from config import load_config
from github_api import GitHubClient
import status as status_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
    stream=sys.stdout,
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logging.Formatter.converter = time.gmtime
log = logging.getLogger("waker")

A1_INSTANCE_ID = os.environ.get("A1_INSTANCE_ID", "").strip()
ALWAYS_RESUME = os.environ.get("ALWAYS_RESUME", "false").strip().lower() in ("1", "true", "yes", "on")
WAKE_DEBOUNCE_S = int(os.environ.get("WAKE_DEBOUNCE_S", "150"))
POLL_INTERVAL = max(10, int(os.environ.get("WAKER_POLL_INTERVAL", "20")))
OCI_PROFILE = os.environ.get("OCI_CONFIG_PROFILE", "").strip()  # optional API-key fallback


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def build_compute_client() -> oci.core.ComputeClient:
    if OCI_PROFILE:
        config = oci.config.from_file(profile_name=OCI_PROFILE)
        return oci.core.ComputeClient(config)
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        return oci.core.ComputeClient(config={"region": signer.region}, signer=signer)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "instance principal auth failed (%s). On the waker VM this should just work; "
            "otherwise set OCI_CONFIG_PROFILE to use an API key from ~/.oci/config.",
            exc,
        )
        raise


def main() -> int:
    if not A1_INSTANCE_ID:
        log.error("A1_INSTANCE_ID is required (terraform output a1_instance_id)")
        return 2

    cfg = load_config()
    cfg.idle_stop_enabled = False  # never idle-stop from the waker side
    gh = GitHubClient(cfg)
    try:
        gh.check_auth()
        log.info("github auth OK (%s mode)", cfg.auth_mode)
    except SystemExit as exc:
        log.error("%s", exc)
        return 2

    try:
        compute = build_compute_client()
    except Exception:  # noqa: BLE001
        return 2

    events: deque[tuple[float, str]] = deque(maxlen=120)
    state: dict = {
        "healthy": True,
        "waker": True,
        "a1_state": "unknown",
        "queued": {},
        "desired": 0,
        "containers_running": 0,
        "runners_busy": 0,
        "min_runners": cfg.min_runners,
        "max_runners": cfg.max_runners,
        "last_wake": None,
        "last_tick": None,
        "always_resume": ALWAYS_RESUME,
        "events": [],
    }

    def snapshot() -> dict:
        # NOTE: returns a COPY - fine for one-off dumps, but the status server
        # needs the live `state` dict (see start_server call below).
        return {**state, "events": [{"ts": _iso(ts), "msg": m} for ts, m in list(events)[-25:]]}

    def event(msg: str) -> None:
        events.append((time.time(), msg))
        # keep the live state's events fresh for the status server
        state["events"] = [{"ts": _iso(ts), "msg": m} for ts, m in list(events)[-25:]]
        log.info("%s", msg)

    server, _thread = status_server.start_server(cfg, state, lambda: False)
    log.info("status server on %s:%d", cfg.status_bind, cfg.status_port)

    stop = {"flag": False}

    def _sig(_s, _f):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    last_start_attempt = 0.0

    while not stop["flag"]:
        loop_start = time.time()
        try:
            queued: dict[str, int] = {}
            for scope in cfg.scopes:
                try:
                    queued[scope.slug] = gh.queued_jobs(scope)
                except requests.RequestException as exc:
                    log.warning("queue poll failed for %s: %s", scope.slug, exc)
                    queued[scope.slug] = 0
            total_queued = sum(queued.values())

            a1_state = "unknown"
            try:
                a1_state = compute.get_instance(A1_INSTANCE_ID).data.lifecycle_state
            except oci.exceptions.ServiceError as exc:
                log.warning("get_instance failed: %s", exc.message)

            state["queued"] = queued
            state["a1_state"] = a1_state
            state["last_tick"] = _iso(time.time())

            can_start = (
                a1_state in ("STOPPED",)
                and time.time() - last_start_attempt > WAKE_DEBOUNCE_S
            )
            if total_queued > 0 and can_start:
                last_start_attempt = time.time()
                try:
                    compute.instance_action(A1_INSTANCE_ID, "START")
                    state["last_wake"] = _iso(last_start_attempt)
                    event(f"queued jobs={total_queued} -> START issued for A1 worker")
                except oci.exceptions.ServiceError as exc:
                    log.error("instance START failed: %s", exc.message)
            elif total_queued > 0 and a1_state in ("STOPPING", "STOPPED"):
                log.debug("jobs waiting but A1 is %s (debounce until restart)", a1_state)
            elif ALWAYS_RESUME and a1_state == "STOPPED" and can_start:
                last_start_attempt = time.time()
                try:
                    compute.instance_action(A1_INSTANCE_ID, "START")
                    state["last_wake"] = _iso(last_start_attempt)
                    event("ALWAYS_RESUME: A1 worker was stopped -> START issued")
                except oci.exceptions.ServiceError as exc:
                    log.error("resume START failed: %s", exc.message)

            state["healthy"] = a1_state in ("RUNNING", "STARTING", "STOPPING", "PROVISIONING") or total_queued == 0

        except Exception as exc:  # noqa: BLE001
            log.exception("waker loop error: %s", exc)
            state["healthy"] = False

        elapsed = time.time() - loop_start
        time.sleep(max(2.0, POLL_INTERVAL - elapsed))

    server.shutdown()
    log.info("waker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
