"""Controller entrypoint: run the autoscaler loop forever.

Environment is documented in .env.example (see repo root).
"""

from __future__ import annotations

import logging
import signal
import sys
import time

from config import load_config
from github_api import GitHubClient
from docker_manager import DockerManager
from autoscaler import Autoscaler
import status as status_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    stream=sys.stdout,
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logging.Formatter.converter = time.gmtime
log = logging.getLogger("controller")


def main() -> int:
    cfg = load_config()
    log.info(
        "starting controller: scopes=%s auth=%s pool=[%d..%d] labels=%s match=%s sock=%s idle-stop=%s",
        [f"{s.kind}:{s.owner}/{s.name}" for s in cfg.scopes],
        cfg.auth_mode,
        cfg.min_runners,
        cfg.max_runners,
        cfg.runner_labels,
        cfg.match_labels,
        cfg.mount_docker_sock,
        cfg.idle_stop_enabled,
    )

    gh = GitHubClient(cfg)
    try:
        gh.check_auth()
        log.info("github auth OK (%s mode)", cfg.auth_mode)
    except SystemExit as exc:
        log.error("%s", exc)
        return 2

    dm = DockerManager(cfg)
    if not dm.healthy():
        log.error("docker daemon is not reachable - is /var/run/docker.sock mounted?")
        return 2

    scaler = Autoscaler(cfg, gh, dm)
    # Pass the LIVE state dict (not a snapshot copy): /healthz flips to 200
    # only after the first successful tick, so `compose up --wait` gates
    # systemd start on a controller that actually works.
    server, thread = status_server.start_server(
        cfg, scaler.state, scaler.idle_shutdown_ready
    )
    log.info("status server listening on %s:%d", cfg.status_bind, cfg.status_port)

    stop = {"flag": False}

    def _sig(_sig, _frm):
        log.info("signal received - shutting down (leaving runner containers alone)")
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    last_housekeep = 0.0
    last_prune = 0.0

    while not stop["flag"]:
        loop_start = time.time()
        try:
            scaler.tick()
        except Exception as exc:  # noqa: BLE001 - never die on a single bad tick
            log.exception("tick failed: %s", exc)
            scaler.state["healthy"] = False

        now = time.time()
        if now - last_housekeep >= cfg.housekeep_interval:
            last_housekeep = now
            scaler.state["disk_free_gb"] = dm.disk_free_gb()
        if now - last_prune >= cfg.prune_interval:
            last_prune = now
            try:
                dm.prune()
            except Exception as exc:  # noqa: BLE001
                log.warning("prune failed: %s", exc)

        elapsed = time.time() - loop_start
        time.sleep(max(1.0, cfg.poll_interval - elapsed))

    server.shutdown()
    log.info("controller stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
