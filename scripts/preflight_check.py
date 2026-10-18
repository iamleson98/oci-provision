#!/usr/bin/env python3
"""preflight_check.py - auto-check whether the runner servers are ALREADY
provisioned before terraform touches anything, to prevent over-provisioning.

The Provision workflow runs this right after `terraform init` and before
`terraform plan/apply` (see .github/workflows/provision.yml). It is the
CI-level layer of the over-provision protection; the terraform-native layer
(plan-time preconditions in terraform/main.tf) is the safety net for local
runs.

What it does:

  1. Queries the OCI API for every live instance in the target compartment,
     plus a best-effort tenancy-wide sweep (A1 OCPUs and AMD micros are
     limited per TENANCY, not per compartment).
  2. Reads the terraform state (same `oci` backend as scripts/tf_init.sh).
  3. Cross-checks the two views and decides:

       state tracks the live servers   -> ALREADY PROVISIONED -> ok, apply
                                           will be an idempotent no-op
       2+ project servers of one role  -> DUPLICATE already exists -> hard
                                           fail (the core over-provision
                                           alarm)
       state tracks a server + an extra
       untracked project server exists -> would duplicate -> hard fail
       state empty, servers exist      -> ADOPT (default): `terraform import`
                                           the whole existing stack so apply
                                           reconciles instead of duplicating;
                                           or abort / report-only via flags
       live A1 OCPUs + planned > limit -> BUDGET exceeded -> hard fail
       live AMD micros >= 2 + waker
       to be created                   -> BUDGET exceeded -> hard fail

Exit codes:
  0  ok (fresh provision / already provisioned / adopted / report-only)
  2  configuration error (missing secrets/env)
  3  untracked servers exist and --on-untracked=abort
  4  duplicate project servers detected (over-provision already happened)
  5  Always Free budget (A1 OCPUs / AMD micros) would be exceeded
  6  adoption failed (terraform import error or ambiguous matches)

Environment (the same secrets the workflows already export - see
scripts/tf_init.sh and docs/CI_DEPLOYMENT.md):
  OCI_TENANCY_OCID OCI_USER_OCID OCI_FINGERPRINT OCI_REGION
  OCI_PRIVATE_KEY (literal \\n escapes are fine) or OCI_PRIVATE_KEY_FILE
  OCI_COMPARTMENT | TF_VAR_compartment_ocid   (default: tenancy root)
  TF_VAR_* variables are read to mirror the terraform configuration:
    TF_VAR_instance_name_prefix (gh-runner), TF_VAR_runner_profile,
    TF_VAR_enable_waker, TF_VAR_create_waker_iam, TF_VAR_a1_ocpus,
    TF_VAR_overprovision_max_ocpus
  PREFLIGHT_ON_UNTRACKED  default for --on-untracked (adopt|abort|report)
  GITHUB_ACTIONS=1        enables ::error/::warning/::notice annotations

Usage:
  python3 scripts/preflight_check.py [--on-untracked adopt|abort|report]
      [--tf-dir terraform] [--json] [--no-tenancy-sweep]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

TERMINAL_STATES = ("TERMINATED", "TERMINATING")
A1_SHAPE = "VM.Standard.A1.Flex"
MICRO_SHAPE = "VM.Standard.E2.1.Micro"
FREE_MAX_MICROS = 2  # Always Free AMD micros per tenancy

# Exit codes
OK = 0
CONFIG_ERROR = 2
UNTRACKED_ABORT = 3
DUPLICATE = 4
BUDGET = 5
ADOPT_FAILED = 6

IN_GH = os.environ.get("GITHUB_ACTIONS", "") == "true"


# --------------------------------------------------------------------- output


def _clean(msg: str) -> str:
    """GitHub annotations must be single-line."""
    return msg.replace("\n", "%0A").replace("\r", "")


def info(msg: str) -> None:
    print(f"[preflight] {msg}")


def warn(msg: str) -> None:
    print(f"[preflight] WARNING: {msg}", file=sys.stderr)
    if IN_GH:
        print(f"::warning title=Over-provision guard::{_clean(msg)}")


def error(msg: str) -> None:
    print(f"[preflight] ERROR: {msg}", file=sys.stderr)
    if IN_GH:
        print(f"::error title=Over-provision guard::{_clean(msg)}")


def notice(msg: str) -> None:
    if IN_GH:
        print(f"::notice title=Provision check::{_clean(msg)}")


# ------------------------------------------------------------------- settings


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_num(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    tenancy: str
    user: str
    fingerprint: str
    region: str
    private_key: str
    private_key_file: str
    compartment: str
    prefix: str
    profile: str
    enable_waker: bool
    create_waker_iam: bool
    a1_ocpus: float
    max_ocpus: float
    on_untracked: str
    tf_dir: str
    tenancy_sweep: bool
    messages: list = field(default_factory=list)


def load_settings(args: argparse.Namespace) -> Settings:
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

    profile = os.environ.get("TF_VAR_runner_profile", "always-on").strip() or "always-on"
    a1_ocpus = _env_num("TF_VAR_a1_ocpus", 0.0) or (4.0 if profile == "burst" else 2.0)
    max_ocpus = _env_num("TF_VAR_overprovision_max_ocpus", 4.0)

    on_untracked = args.on_untracked or os.environ.get("PREFLIGHT_ON_UNTRACKED", "adopt")
    if on_untracked not in ("adopt", "abort", "report"):
        raise ConfigError(f"--on-untracked must be adopt|abort|report, got {on_untracked!r}")

    tf_dir = args.tf_dir
    if not os.path.isdir(tf_dir):
        raise ConfigError(f"--tf-dir {tf_dir!r} is not a directory")

    return Settings(
        tenancy=tenancy,
        user=user,
        fingerprint=fingerprint,
        region=region,
        private_key=private_key,
        private_key_file=private_key_file,
        compartment=compartment,
        prefix=os.environ.get("TF_VAR_instance_name_prefix", "gh-runner").strip() or "gh-runner",
        profile=profile,
        enable_waker=_env_bool("TF_VAR_enable_waker", True),
        create_waker_iam=_env_bool("TF_VAR_create_waker_iam", True),
        a1_ocpus=a1_ocpus,
        max_ocpus=max_ocpus,
        on_untracked=on_untracked,
        tf_dir=tf_dir,
        tenancy_sweep=not args.no_tenancy_sweep,
    )


# ------------------------------------------------------------------- clients


def build_clients(s: Settings):
    """Build (compute, network, identity) OCI clients. Overridable in tests."""
    import oci  # noqa: PLC0415 - deferred so tests can inject a fake module

    cfg = {
        "tenancy": s.tenancy,
        "user": s.user,
        "fingerprint": s.fingerprint,
        "region": s.region,
    }
    if s.private_key:
        cfg["key_content"] = s.private_key
    else:
        cfg["key_file"] = s.private_key_file
    return (
        oci.core.ComputeClient(cfg),
        oci.core.VirtualNetworkClient(cfg),
        oci.identity.IdentityClient(cfg),
    )


def _list_all(fn, *args, **kwargs):
    """Paginated OCI list call -> plain list of objects."""
    import oci  # noqa: PLC0415

    return oci.pagination.list_call_get_all_results(fn, *args, **kwargs).data


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


def norm_obj(obj) -> dict:
    return {
        "id": getattr(obj, "id", "") or "",
        "display_name": getattr(obj, "display_name", "") or getattr(obj, "name", "") or "",
    }


# ------------------------------------------------------------- terraform glue


def _run_tf(tf_dir: str, *tf_args: str) -> tuple[int, str]:
    """Run a terraform command; returns (returncode, combined output).

    Patched by tests to avoid needing a real terraform/backend.
    """
    proc = subprocess.run(
        ["terraform", f"-chdir={tf_dir}", *tf_args],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class TerraformState:
    """Thin wrapper around `terraform state list` / `show -json` / `import`."""

    def __init__(self, tf_dir: str) -> None:
        self.tf_dir = tf_dir
        self._addresses: list[str] | None = None
        self._show: dict | None = None
        self.import_calls: list[tuple[str, str]] = []

    def state_list(self) -> list[str]:
        if self._addresses is None:
            rc, out = _run_tf(self.tf_dir, "state", "list", "-lock=false")
            self._addresses = [
                line.strip() for line in out.splitlines()
                if line.strip() and not line.startswith("No state")
            ] if rc == 0 else []
        return self._addresses

    def show_json(self) -> dict:
        if self._show is None:
            rc, out = _run_tf(self.tf_dir, "show", "-json", "-lock=false")
            if rc != 0 or not out.strip():
                self._show = {}
            else:
                try:
                    self._show = json.loads(out)
                except json.JSONDecodeError:
                    self._show = {}
        return self._show

    def tracked_instances(self) -> dict[str, str]:
        """Map terraform address -> live OCID for every tracked instance."""
        tracked: dict[str, str] = {}
        show = self.show_json()
        root = show.get("values", {}).get("root_module", {}) if show else {}
        modules = [root] + [m for m in root.get("child_modules", []) or []]
        for module in modules:
            for res in module.get("resources", []) or []:
                if res.get("type") == "oci_core_instance":
                    ocid = (res.get("values", {}) or {}).get("id", "") or ""
                    tracked[res.get("address", "")] = ocid
        return tracked

    def has_address(self, address: str) -> bool:
        return address in self.state_list()

    def import_resource(self, address: str, ocid: str) -> tuple[bool, str]:
        self.import_calls.append((address, ocid))
        rc, out = _run_tf(self.tf_dir, "import", "-input=false", "-no-color", address, ocid)
        return rc == 0, out


# ------------------------------------------------------------------- preflight


class Preflight:
    def __init__(self, s: Settings, compute, network, identity, tf: TerraformState) -> None:
        self.s = s
        self.compute = compute
        self.network = network
        self.identity = identity
        self.tf = tf

        self.live: list[dict] = []              # compartment, non-terminated
        self.tenancy_live: list[dict] | None = None  # None = sweep unavailable
        self.project_a1: list[dict] = []
        self.project_waker: list[dict] = []
        self.tracked: dict[str, str] = {}
        self.findings: dict = {}
        self.imports: list[dict] = []

    # ------------------------------------------------------------ gathering

    def gather(self) -> None:
        info(f"compartment: {self.s.compartment}")
        info("listing live instances in the compartment (auto-check: already provisioned?)...")
        try:
            raw = [norm_instance(i) for i in _list_all(self.compute.list_instances, self.s.compartment)]
        except Exception as exc:  # noqa: BLE001
            # Fail CLOSED: if the already-provisioned check cannot run, the run
            # must not continue to apply (that would risk over-provisioning).
            raise RuntimeError(
                f"cannot list instances in the compartment ({exc}); failing closed so the "
                "over-provision check is never skipped. Fix credentials/network and re-run."
            ) from exc
        self.live = [i for i in raw if i["lifecycle_state"] not in TERMINAL_STATES]
        self.project_a1 = [i for i in self.live if i["display_name"].startswith(f"{self.s.prefix}-a1")]
        self.project_waker = [i for i in self.live if i["display_name"].startswith(f"{self.s.prefix}-waker")]

        if self.s.tenancy_sweep and self.s.compartment != self.s.tenancy:
            try:
                info("sweeping the whole tenancy for A1 OCPUs / AMD micros (limits are per tenancy)...")
                raw_t = [norm_instance(i) for i in _list_all(
                    self.compute.list_instances, self.s.tenancy, compartment_id_in_subtree=True
                )]
                self.tenancy_live = [i for i in raw_t if i["lifecycle_state"] not in TERMINAL_STATES]
            except Exception as exc:  # noqa: BLE001 - sweep is best-effort
                warn(f"tenancy-wide instance sweep failed ({exc}); falling back to compartment-only numbers.")
                self.tenancy_live = None

        self.tracked = self.tf.tracked_instances()
        info(f"terraform state tracks {len(self.tracked)} instance(s): "
             f"{', '.join(self.tracked) if self.tracked else '(none)'}")

    # ------------------------------------------------------------- helpers

    def _budget_instances(self) -> list[dict]:
        return self.tenancy_live if self.tenancy_live is not None else self.live

    def _live_a1_ocpus(self) -> float:
        return sum(i["ocpus"] for i in self._budget_instances() if i["shape"] == A1_SHAPE)

    def _live_micros(self) -> int:
        return sum(1 for i in self._budget_instances() if i["shape"] == MICRO_SHAPE)

    def _tracked_alive(self, address: str, instances: list[dict]) -> bool:
        ocid = self.tracked.get(address, "")
        return bool(ocid) and ocid in {i["id"] for i in instances}

    # ------------------------------------------------------------- analysis

    def run(self) -> int:
        self.gather()

        # ---- guard 1: duplicates already exist -> over-provision happened
        if len(self.project_a1) > 1 or len(self.project_waker) > 1:
            self._report(DUPLICATE)
            error(self._duplicate_message())
            return DUPLICATE

        tracked_a1_alive = self._tracked_alive("oci_core_instance.a1", self.project_a1)
        tracked_waker_alive = self._tracked_alive("oci_core_instance.waker[0]", self.project_waker)
        tracked_ids = set(self.tracked.values())
        untracked_a1 = [i for i in self.project_a1 if i["id"] not in tracked_ids]
        untracked_waker = [i for i in self.project_waker if i["id"] not in tracked_ids]
        state_has_instances = bool(self.tracked)

        # ---- guard 2: tracked server + extra untracked one -> would duplicate
        if state_has_instances and (untracked_a1 or untracked_waker):
            self._report(DUPLICATE)
            error(
                "Duplicate project server detected: terraform state already tracks an instance, "
                f"but {len(untracked_a1)} extra A1 worker(s) and {len(untracked_waker)} extra waker(s) "
                f"exist in the compartment outside of state. Applying now would OVER-PROVISION. "
                "Terminate the surplus instance(s) in the OCI console, or - if state is stale - "
                "run 'terraform -chdir=terraform state rm <address>' and re-run with "
                "--on-untracked=adopt. See docs/TROUBLESHOOTING.md."
            )
            return DUPLICATE

        # ---- happy path: state tracks the live servers
        if state_has_instances:
            will_create_a1 = not tracked_a1_alive
            will_create_waker = self.s.enable_waker and not tracked_waker_alive
            if not will_create_a1 and not will_create_waker:
                msg = ("Auto-check: already provisioned - state tracks the live "
                       f"A1 worker{' and waker' if self.s.enable_waker and tracked_waker_alive else ''}; "
                       "terraform apply will be an idempotent no-op refresh.")
                info(msg)
                notice(msg)
            else:
                info("state tracks terminated/absent instance(s) - terraform will (re)create them; allowed.")
            self._report(OK, will_create_a1=will_create_a1, will_create_waker=will_create_waker)
            rc = self._budget_check(will_create_a1, will_create_waker)
            if rc != OK:
                return rc
            return OK

        # ---- state is empty (or has no instances) but servers exist
        if untracked_a1 or untracked_waker or self._project_network_exists():
            if self.s.on_untracked == "report":
                info("REPORT ONLY: existing project servers found that are NOT in terraform state.")
                self._report(OK, untracked=True, will_create_a1=False, will_create_waker=False)
                warn("re-run the Provision workflow (default) or pass --on-untracked=adopt to import "
                     "the existing servers into terraform state; --on-untracked=abort refuses to.")
                return OK
            if self.s.on_untracked == "abort":
                self._report(UNTRACKED_ABORT, untracked=True)
                error(
                    "Servers look already provisioned but terraform state is empty (state bucket/key "
                    "changed or reset?). Refusing to continue because terraform would create "
                    "DUPLICATES and over-provision the Always Free envelope. Re-run with "
                    "--on-untracked=adopt (CI default) to import the existing stack into state, "
                    "or terminate the old servers first. See docs/TROUBLESHOOTING.md."
                )
                return UNTRACKED_ABORT
            # adopt (CI default)
            info("existing project servers found but terraform state is empty -> ADOPTING them "
                 "(terraform import) so nothing gets duplicated...")
            if not self._adopt():
                self._report(ADOPT_FAILED)
                return ADOPT_FAILED
            # The waker may still be missing and about to be created - run the
            # Always Free budget checks for whatever terraform would ADD.
            will_create_waker = not self.project_waker and self.s.enable_waker
            self._report(OK, adopted=True, will_create_a1=False,
                         will_create_waker=will_create_waker)
            notice("existing servers adopted into terraform state; continuing with plan/apply "
                   "(expect an in-place SSH-key metadata refresh when the keypair is regenerated).")
            rc = self._budget_check(False, will_create_waker)
            return rc if rc != OK else OK

        # ---- nothing tracked, nothing existing -> genuinely fresh
        info("no existing project servers found - fresh provision (nothing to over-provision).")
        self._report(OK, fresh=True, will_create_a1=True,
                     will_create_waker=self.s.enable_waker)
        rc = self._budget_check(True, self.s.enable_waker)
        return rc if rc != OK else OK

    # ------------------------------------------------------------ guards

    def _budget_check(self, will_create_a1: bool, will_create_waker: bool) -> int:
        live_ocpus = self._live_a1_ocpus()
        planned = self.s.a1_ocpus if will_create_a1 else 0.0
        total = live_ocpus + planned
        scope = "tenancy" if self.tenancy_live is not None else "compartment"

        if total > self.s.max_ocpus:
            msg = (f"Over-provision guard: live A1 OCPUs in {scope} ({live_ocpus:g}) + planned "
                   f"({planned:g}) = {total:g} exceeds overprovision_max_ocpus = {self.s.max_ocpus:g}. "
                   "The post-June-2026 Always Free envelope is 4 OCPU / 24 GB per tenancy "
                   "(free-only accounts are stopped above 2 OCPU / 12 GB). Terminate or shrink "
                   "other A1 instances, or raise TF_VAR_overprovision_max_ocpus if you pay. "
                   "Refusing to provision.")
            error(msg)
            return BUDGET
        if total > 2:
            warn(f"A1 OCPU total for the {scope} will be {total:g}: fine on PAYG-grandfathered "
                 "accounts, but free-only accounts are forcibly stopped above 2 OCPU / 12 GB.")

        if will_create_waker:
            micros = self._live_micros()
            if micros + 1 > FREE_MAX_MICROS:
                msg = (f"Over-provision guard: {micros} live AMD E2.1.Micro instances in the "
                       f"{scope}, but Always Free includes only {FREE_MAX_MICROS}. Creating the "
                       "waker would start billing. Disable it (enable_waker=false / workflow "
                       "input) or terminate another micro first.")
                error(msg)
                return BUDGET
        return OK

    def _duplicate_message(self) -> str:
        a1 = ", ".join(f"{i['display_name']} ({i['lifecycle_state']}, {i['ocpus']:g} OCPU)" for i in self.project_a1)
        wk = ", ".join(f"{i['display_name']} ({i['lifecycle_state']})" for i in self.project_waker)
        return (
            "Over-provision guard: multiple project servers of the same role already exist - "
            f"A1 workers: [{a1 or '-'}]; wakers: [{wk or '-'}]. This configuration manages exactly "
            "one of each. Terminate the surplus instances in the OCI console (or re-adopt a "
            "single one after cleaning up), then re-run. See docs/TROUBLESHOOTING.md."
        )

    # ------------------------------------------------------------ adoption

    def _project_network_exists(self) -> bool:
        try:
            vcns = [norm_obj(v) for v in _list_all(self.network.list_vcns, self.s.compartment)]
        except Exception as exc:  # noqa: BLE001
            warn(f"could not list VCNs ({exc}); assuming no existing network.")
            return False
        return any(v["display_name"] == f"{self.s.prefix}-vcn" for v in vcns)

    def _find_one(self, items: list[dict], display_name: str, kind: str) -> dict | None:
        matches = [i for i in items if i["display_name"] == display_name]
        if len(matches) > 1:
            raise RuntimeError(
                f"ambiguous adoption target: {len(matches)} {kind} objects named "
                f"{display_name!r} exist; clean up manually before adopting."
            )
        return matches[0] if matches else None

    def _adopt(self) -> bool:
        s = self.s
        imports: list[tuple[str, str]] = []

        try:
            vcns = [norm_obj(v) for v in _list_all(self.network.list_vcns, s.compartment)]
            vcn = self._find_one(vcns, f"{s.prefix}-vcn", "VCN")
            if vcn:
                imports.append(("oci_core_vcn.this", vcn["id"]))
                vcn_id = vcn["id"]
                igws = [norm_obj(o) for o in _list_all(self.network.list_internet_gateways, s.compartment, vcn_id=vcn_id)]
                rts = [norm_obj(o) for o in _list_all(self.network.list_route_tables, s.compartment, vcn_id=vcn_id)]
                sls = [norm_obj(o) for o in _list_all(self.network.list_security_lists, s.compartment, vcn_id=vcn_id)]
                subnets = [norm_obj(o) for o in _list_all(self.network.list_subnets, s.compartment, vcn_id=vcn_id)]
                for address, items, name, kind in (
                    ("oci_core_internet_gateway.this", igws, f"{s.prefix}-igw", "internet gateway"),
                    ("oci_core_route_table.this", rts, f"{s.prefix}-rt", "route table"),
                    ("oci_core_security_list.this", sls, f"{s.prefix}-sl", "security list"),
                    ("oci_core_subnet.this", subnets, f"{s.prefix}-subnet", "subnet"),
                ):
                    obj = self._find_one(items, name, kind)
                    if obj:
                        imports.append((address, obj["id"]))

            if len(self.project_a1) == 1:
                imports.append(("oci_core_instance.a1", self.project_a1[0]["id"]))
            elif len(self.project_a1) > 1:
                raise RuntimeError("multiple A1 workers exist; terminate extras before adopting.")

            if s.enable_waker:
                if len(self.project_waker) == 1:
                    imports.append(("oci_core_instance.waker[0]", self.project_waker[0]["id"]))
                elif len(self.project_waker) > 1:
                    raise RuntimeError("multiple wakers exist; terminate extras before adopting.")

            if s.enable_waker and s.create_waker_iam:
                dgs = [norm_obj(o) for o in _list_all(self.identity.list_dynamic_groups, s.tenancy)]
                dg = self._find_one(dgs, f"{s.prefix}-waker-dg", "dynamic group")
                if dg:
                    imports.append(("oci_identity_dynamic_group.waker[0]", dg["id"]))
                policies = [norm_obj(o) for o in _list_all(self.identity.list_policies, s.tenancy)]
                policy = self._find_one(policies, f"{s.prefix}-waker-policy", "policy")
                if policy:
                    imports.append(("oci_identity_policy.waker[0]", policy["id"]))
        except RuntimeError as exc:
            error(f"adoption aborted: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            error(f"adoption failed while querying OCI: {exc}")
            return False

        if not imports:
            error("adoption found nothing to import (unexpected - servers were detected).")
            return False

        failed = False
        for address, ocid in imports:
            if self.tf.has_address(address):
                info(f"import skipped (already in state): {address}")
                self.imports.append({"address": address, "ocid": ocid, "status": "already-in-state"})
                continue
            info(f"terraform import {address} {ocid}")
            ok, out = self.tf.import_resource(address, ocid)
            status = "imported" if ok else "failed"
            self.imports.append({"address": address, "ocid": ocid, "status": status})
            if not ok:
                error(f"terraform import failed for {address}: {out.strip()[-500:]}")
                failed = True
        return not failed

    # ------------------------------------------------------------ reporting

    def _report(self, rc: int, **extra) -> None:
        budget = self._budget_instances()
        self.findings = {
            "result": {
                OK: "ok",
                UNTRACKED_ABORT: "untracked-abort",
                DUPLICATE: "duplicate",
                BUDGET: "budget-exceeded",
                ADOPT_FAILED: "adopt-failed",
            }.get(rc, "unknown"),
            "exit_code": rc,
            "compartment": self.s.compartment,
            "tenancy_sweep": self.tenancy_live is not None,
            "live_instances": [
                {
                    "display_name": i["display_name"],
                    "shape": i["shape"],
                    "ocpus": i["ocpus"],
                    "lifecycle_state": i["lifecycle_state"],
                    "id": i["id"],
                }
                for i in self.live
            ],
            "live_a1_ocpus_total": sum(i["ocpus"] for i in budget if i["shape"] == A1_SHAPE),
            "live_amd_micros": sum(1 for i in budget if i["shape"] == MICRO_SHAPE),
            "max_ocpus": self.s.max_ocpus,
            "tracked_instances": self.tracked,
            "imports": self.imports,
            **extra,
        }


# ----------------------------------------------------------------------- main

# Last Preflight instance run by main(); lets smoke tests inspect findings
# without parsing stdout. Not used by the CLI itself.
_LAST: "Preflight | None" = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--on-untracked",
        choices=("adopt", "abort", "report"),
        default=None,
        help="what to do when servers exist but terraform state is empty "
             "(default: $PREFLIGHT_ON_UNTRACKED or 'adopt')",
    )
    parser.add_argument("--tf-dir", default=None, help="terraform directory (default: <repo>/terraform)")
    parser.add_argument("--json", action="store_true", help="print a JSON summary at the end")
    parser.add_argument("--no-tenancy-sweep", action="store_true", help="skip the tenancy-wide A1/micro sweep")
    args = parser.parse_args(argv)

    if not args.tf_dir:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        args.tf_dir = os.path.join(repo_root, "terraform")

    try:
        s = load_settings(args)
    except ConfigError as exc:
        error(str(exc))
        return CONFIG_ERROR

    try:
        compute, network, identity = build_clients(s)
    except Exception as exc:  # noqa: BLE001
        error(f"cannot build OCI clients: {exc}")
        return CONFIG_ERROR

    tf = TerraformState(s.tf_dir)
    pf = Preflight(s, compute, network, identity, tf)
    global _LAST
    _LAST = pf
    try:
        rc = pf.run()
    except Exception as exc:  # noqa: BLE001
        error(f"unexpected failure: {exc}")
        rc = CONFIG_ERROR

    if args.json:
        print(json.dumps(pf.findings, indent=2, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
