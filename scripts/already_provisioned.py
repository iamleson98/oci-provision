#!/usr/bin/env python3
"""already_provisioned.py - fast auto-quit guard for the scheduled capacity
hunt (first job of .github/workflows/provision.yml).

The Provision workflow fires ~10x/day on schedule to lottery-snipe Always
Free A1 host capacity. This script runs BEFORE terraform init/plan/apply:
if the stack's servers already exist in the target compartment (A1 worker
and/or E2.1.Micro waker, any non-terminal lifecycle state), it reports
provisioned=true and the workflow skips every heavy job - the run ends
green in about a minute instead of grinding through a no-op plan/apply ten
times a day.

This is the FAST layer of the over-provision protection. The deep layers
still guard manual runs and edge cases: scripts/preflight_check.py (after
terraform init; also auto-adopts a state-lost stack) and the plan-time
preconditions in terraform/main.tf. If this guard cannot reach the OCI API
it fails OPEN (provisioned=false + warning) - the deeper layers fail
closed, so a genuinely broken tenancy never slips through silently.

Decision:
    any non-terminated VM.Standard.A1.Flex or VM.Standard.E2.1.Micro in
    the target compartment                       -> provisioned=true  (quit)
    none (terminal states do not count)           -> provisioned=false (hunt)

Output:
    - writes `provisioned=true|false` to $GITHUB_OUTPUT (step output)
    - prints "ALREADY_PROVISIONED=true|false" for plain-log greppability
    - when quitting, appends a short block to $GITHUB_STEP_SUMMARY

Exit codes:
    0   check completed - read the provisioned= output / log verdict
    2   configuration error (missing OCI_* secrets) - fails the guard job
        red so a broken hunt is VISIBLE, never silently stopped

Environment (the same secrets the workflows already export - see
scripts/tf_init.sh and docs/CI_DEPLOYMENT.md):
    OCI_TENANCY_OCID OCI_USER_OCID OCI_FINGERPRINT OCI_REGION
    OCI_PRIVATE_KEY (literal \\n escapes are fine) or OCI_PRIVATE_KEY_FILE
    OCI_COMPARTMENT | TF_VAR_compartment_ocid   (default: tenancy root)
    GITHUB_ACTIONS=1 enables ::error/::warning/::notice annotations
"""

from __future__ import annotations

import os
import sys

TERMINAL_STATES = ("TERMINATED", "TERMINATING")
PROJECT_SHAPES = ("VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro")

OK = 0
CONFIG_ERROR = 2

IN_GH = os.environ.get("GITHUB_ACTIONS", "") == "true"


# --------------------------------------------------------------------- output


def _clean(msg: str) -> str:
    """GitHub annotations must be single-line."""
    return msg.replace("\n", "%0A").replace("\r", "")


def info(msg: str) -> None:
    print(f"[guard] {msg}")


def warn(msg: str) -> None:
    print(f"[guard] WARNING: {msg}", file=sys.stderr)
    if IN_GH:
        print(f"::warning title=Auto-quit guard::{_clean(msg)}")


def error(msg: str) -> None:
    print(f"[guard] ERROR: {msg}", file=sys.stderr)
    if IN_GH:
        print(f"::error title=Auto-quit guard::{_clean(msg)}")


def notice(msg: str) -> None:
    print(f"[guard] {msg}")
    if IN_GH:
        print(f"::notice title=Auto-quit guard::{_clean(msg)}")


# ------------------------------------------------------------------- settings


class ConfigError(Exception):
    pass


def load_settings() -> dict:
    tenancy = os.environ.get("OCI_TENANCY_OCID", "").strip()
    user = os.environ.get("OCI_USER_OCID", "").strip()
    fingerprint = os.environ.get("OCI_FINGERPRINT", "").strip()
    region = os.environ.get("OCI_REGION", "").strip()
    private_key = os.environ.get("OCI_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    private_key_file = os.environ.get("OCI_PRIVATE_KEY_FILE", "").strip()

    missing = [
        n for n, v in (
            ("OCI_TENANCY_OCID", tenancy),
            ("OCI_USER_OCID", user),
            ("OCI_FINGERPRINT", fingerprint),
            ("OCI_REGION", region),
        ) if not v
    ]
    if not private_key and not private_key_file:
        missing.append("OCI_PRIVATE_KEY (or OCI_PRIVATE_KEY_FILE)")
    if missing:
        raise ConfigError("missing required env/secrets: " + ", ".join(missing))

    compartment = (
        os.environ.get("OCI_COMPARTMENT", "").strip()
        or os.environ.get("TF_VAR_compartment_ocid", "").strip()
        or tenancy
    )
    return {
        "tenancy": tenancy,
        "user": user,
        "fingerprint": fingerprint,
        "region": region,
        "private_key": private_key,
        "private_key_file": private_key_file,
        "compartment": compartment,
    }


# -------------------------------------------------------------------- clients


def build_compute_client(s: dict):
    """Build the OCI compute client. Overridable in tests."""
    import oci  # noqa: PLC0415 - deferred so tests can inject a fake module

    cfg = {
        "tenancy": s["tenancy"],
        "user": s["user"],
        "fingerprint": s["fingerprint"],
        "region": s["region"],
    }
    if s["private_key"]:
        cfg["key_content"] = s["private_key"]
    else:
        cfg["key_file"] = s["private_key_file"]
    return oci.core.ComputeClient(cfg)


def list_instances(compute, compartment: str) -> list:
    """Every instance in the compartment (all pages), any lifecycle state."""
    import oci  # noqa: PLC0415

    return oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment
    ).data


def norm_instance(inst) -> dict:
    """Normalize an SDK/fake instance object into a plain dict."""
    shape_config = getattr(inst, "shape_config", None)
    ocpus = getattr(shape_config, "ocpus", None) if shape_config is not None else None
    return {
        "id": getattr(inst, "id", "") or "",
        "display_name": getattr(inst, "display_name", "") or "",
        "lifecycle_state": getattr(inst, "lifecycle_state", "") or "",
        "shape": getattr(inst, "shape", "") or "",
        "ocpus": float(ocpus or 0),
    }


# ------------------------------------------------------------------- emission


def _emit(provisioned: bool) -> None:
    """Step output (`provisioned=`) + greppable log line."""
    value = "true" if provisioned else "false"
    out_file = os.environ.get("GITHUB_OUTPUT", "")
    if out_file:
        with open(out_file, "a", encoding="utf-8") as fh:
            fh.write(f"provisioned={value}\n")
    print(f"ALREADY_PROVISIONED={value}")


def _summary(lines: list) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file and lines:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


# ----------------------------------------------------------------------- main


def main() -> int:
    try:
        s = load_settings()
    except ConfigError as exc:
        error(
            f"{exc} - the guard job fails red on purpose so a broken "
            "scheduled hunt is visible instead of silently stopping. "
            "See docs/CI_DEPLOYMENT.md (secrets table)."
        )
        return CONFIG_ERROR

    short = s["compartment"].rsplit(".", 1)[-1][:12] or s["compartment"]
    try:
        compute = build_compute_client(s)
        instances = [
            norm_instance(i) for i in list_instances(compute, s["compartment"])
        ]
    except Exception as exc:  # noqa: BLE001 - fail OPEN on purpose
        warn(
            f"could not query the OCI API ({exc.__class__.__name__}: {exc}) - "
            "treating the tenancy as NOT provisioned and letting the full run "
            "proceed; the preflight after terraform init fails closed if the "
            "API is genuinely down, so nothing slips through silently."
        )
        _emit(False)
        info("verdict: provisioned=false (UNVERIFIED - API unreachable)")
        return OK

    live_project = [
        i for i in instances
        if i["shape"] in PROJECT_SHAPES
        and i["lifecycle_state"] not in TERMINAL_STATES
    ]
    info(
        f"scanned compartment ...{short}: {len(instances)} instance(s) "
        f"returned, {len(live_project)} live project server(s)"
    )
    for inst in live_project:
        info(
            f"  live project server: {inst['display_name']} "
            f"[{inst['shape']}] state={inst['lifecycle_state']} "
            f"ocpus={inst['ocpus']:g}"
        )

    if live_project:
        notice(
            f"already provisioned - {len(live_project)} live project "
            f"server(s) ({', '.join(i['lifecycle_state'] for i in live_project)}); "
            "auto-quitting this run: terraform init/plan/apply and the "
            "deploy job are skipped."
        )
        _emit(True)
        _summary([
            "## Auto-quit: servers already provisioned",
            "",
            f"{len(live_project)} live project server(s) found via the OCI API:",
            "",
            "| VM | Shape | State |",
            "|---|---|---|",
            *[
                f"| `{i['display_name']}` | {i['shape']} | "
                f"{i['lifecycle_state']} |"
                for i in live_project
            ],
            "",
            "terraform init/plan/apply and the deploy job were skipped on "
            "purpose - no point hunting for capacity that is already in "
            "use. To redeploy software on the existing servers use the "
            "**Deploy stack** workflow; to rebuild from scratch terminate "
            "the old servers first (the Provision preflight then adopts or "
            "creates cleanly).",
        ])
        info("verdict: provisioned=true - auto-quit")
        return OK

    terminal_project = [i for i in instances if i["shape"] in PROJECT_SHAPES]
    if terminal_project:
        info(
            f"  note: {len(terminal_project)} project server(s) in terminal "
            "state only (TERMINATED/TERMINATING) - they do not count"
        )
    non_project = [
        i for i in instances if i["shape"] not in PROJECT_SHAPES
    ]
    if non_project:
        info(
            f"  note: {len(non_project)} non-project instance(s) ignored "
            "(different shape)"
        )

    _emit(False)
    info("verdict: provisioned=false - the hunt continues (init/plan/apply)")
    return OK


if __name__ == "__main__":
    sys.exit(main())
