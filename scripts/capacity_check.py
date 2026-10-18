#!/usr/bin/env python3
"""Pre-launch capacity verification: fail fast instead of blind retrying.

Checks everything that CAN be checked before an apply:

  1. Shape support     - are VM.Standard.A1.Flex / VM.Standard.E2.1.Micro
                         actually offered in the target Availability Domain?
  2. Service quota     - does the tenancy limit have headroom (A1 cores,
                         A1 memory, micro count)? A zero quota fails every
                         launch with LimitExceeded - no point retrying.

What CANNOT be checked (no public OCI API exists for it):

  3. Physical host capacity. The ONLY definitive test is a launch attempt.
     If shape+quota are green but apply still returns "Out of host capacity",
     the AD's hosts are simply dry. Remedies, in order of effectiveness:
       a. Off-peak retries - enable repo variable OFF_PEAK_PROVISION=true and
          the Provision workflow hunts automatically at 04:00 local time.
       b. Another availability domain (multi-AD regions only; e.g. Singapore
          has exactly one).
       c. Upgrade to Pay As You Go - Always Free resources stay $0 (official
          FAQ), launch priority improves, idle reclamation is exempt.
     See docs/TROUBLESHOOTING.md "Out of host capacity".

Environment (same names the workflows already export):
  OCI_REGION / OCI_CLI_REGION
  OCI_TENANCY_OCID / OCI_CLI_TENANCY
  OCI_USER_OCID / OCI_CLI_USER
  OCI_FINGERPRINT / OCI_CLI_FINGERPRINT
  OCI_PRIVATE_KEY / OCI_CLI_KEY_CONTENT   (literal \\n escapes are fine)
Optional:
  TARGET_AD           full AD name; default = first AD of the region
  A1_OCPUS_NEEDED     default 2
  A1_MEMORY_NEEDED    default 12   (GB)
  MICRO_COUNT_NEEDED  default 1

Exit codes:
  0  all checks green (launch is worth attempting)
  1  a required shape is not offered in the AD
  2  service quota is exhausted/zero for a needed resource
  3  OCI API failure (treat as warning in CI; do not block apply)
"""

from __future__ import annotations

import os
import sys

try:
    import oci
except ImportError:  # pragma: no cover
    print("::error::oci python SDK not installed (pip install oci)")
    sys.exit(3)


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


def _norm_key(pem: str) -> str:
    return pem.replace("\\n", "\n")


def _build_config() -> dict:
    region = _env("OCI_REGION", "OCI_CLI_REGION")
    tenancy = _env("OCI_TENANCY_OCID", "OCI_CLI_TENANCY")
    user = _env("OCI_USER_OCID", "OCI_CLI_USER")
    fingerprint = _env("OCI_FINGERPRINT", "OCI_CLI_FINGERPRINT")
    key_content = _norm_key(_env("OCI_PRIVATE_KEY", "OCI_CLI_KEY_CONTENT"))
    missing = [
        n for n, v in (
            ("OCI_REGION", region), ("OCI_TENANCY_OCID", tenancy),
            ("OCI_USER_OCID", user), ("OCI_FINGERPRINT", fingerprint),
            ("OCI_PRIVATE_KEY", key_content),
        ) if not v
    ]
    if missing:
        print(f"::error::missing env: {', '.join(missing)}")
        sys.exit(3)
    return {
        "region": region,
        "tenancy": tenancy,
        "user": user,
        "fingerprint": fingerprint,
        "key_content": key_content,
        "auth_type": "api_key",
    }


A1_SHAPE = "VM.Standard.A1.Flex"
MICRO_SHAPE = "VM.Standard.E2.1.Micro"


def main() -> int:
    cfg = _build_config()
    region = cfg["region"]
    tenancy = cfg["tenancy"]
    a1_ocpus_needed = float(_env("A1_OCPUS_NEEDED", default="2"))
    a1_mem_needed = float(_env("A1_MEMORY_NEEDED", default="12"))
    micro_needed = int(_env("MICRO_COUNT_NEEDED", default="1"))

    try:
        identity = oci.identity.IdentityClient(cfg)
        compute = oci.core.ComputeClient(cfg)
        limits = oci.limits.LimitsClient(cfg)
    except Exception as exc:  # pragma: no cover
        print(f"::error::client init failed: {exc}")
        return 3

    # ---- availability domains -------------------------------------------------
    try:
        ads = [a.name for a in oci.pagination.list_call_get_all_results(
            identity.list_availability_domains, compartment_id=tenancy).data]
    except Exception as exc:
        print(f"::error::could not list availability domains: {exc}")
        return 3
    if not ads:
        print("::error::tenancy has no availability domains")
        return 3

    target_ad = _env("TARGET_AD") or ads[0]
    print(f"=== Capacity pre-check (region {region}, AD {target_ad}) ===")
    print(f"ADs in region: {ads if len(ads) > 1 else ads[0] + ' (single-AD region - no AD rotation possible)'}")

    # ---- 1. shape support -----------------------------------------------------
    try:
        shapes = {s.shape for s in oci.pagination.list_call_get_all_results(
            compute.list_shapes, compartment_id=tenancy,
            availability_domain=target_ad).data}
    except Exception as exc:
        print(f"::error::could not list shapes: {exc}")
        return 3

    rc = 0
    for needed, label in ((A1_SHAPE, "A1 worker"), (MICRO_SHAPE, "waker micro")):
        if needed in shapes:
            print(f"shape {needed:24} SUPPORTED      ({label})")
        else:
            print(f"::error title=Shape unavailable::shape {needed} ({label}) is not offered in {target_ad}. "
                  f"Pick a different AD via availability_domain_index, or a different region.")
            print(f"shape {needed:24} NOT OFFERED    ({label})")
            rc = 1
    if rc:
        return rc

    # ---- 2. service quota -----------------------------------------------------
    # Discover the relevant limit names (naming varies by tenancy/region).
    try:
        defs = oci.pagination.list_call_get_all_results(
            limits.list_limit_definitions, service_name="compute",
            compartment_id=tenancy).data
    except Exception as exc:
        print(f"::warning::could not list limit definitions: {exc}")
        defs = []

    def _availability(limit_name: str):
        try:
            return limits.get_resource_availability(
                service_name="compute", limit_name=limit_name,
                compartment_id=tenancy,
                availability_domain=target_ad).data
        except Exception:
            return None

    checks = []  # (label, limit_name candidates, needed, unit)
    # Candidate ordering matters: canonical tenancy-wide limit names first,
    # prefixed variants (dvh- = dedicated virtual hosts, dense-, bm-, ...) last
    # - they are DIFFERENT limits that just contain 'a1'/'micro' in the name.
    a1_defs = [d.name for d in defs if "a1" in d.name.lower()]
    micro_defs = [d.name for d in defs if "micro" in d.name.lower()]

    def _rank(names, canonical):
        # canonical exact names first, then discovered ones that share the
        # canonical 'standard-' prefix. Prefixed variants (dvh- = dedicated
        # virtual hosts, dense-, bm-, ...) measure DIFFERENT limits and are
        # dropped entirely - e.g. dvh-standard-a1-core-count is the dedicated-
        # host A1 limit, not the tenancy A1 limit.
        ranked = [n for n in canonical]
        for n in names:
            if n in ranked:
                continue
            if n.startswith(("standard-", "std-")):
                ranked.append(n)
        return ranked

    mem_candidates = _rank(
        [n for n in a1_defs if "memory" in n.lower()],
        ["standard-a1-memory-in-gbs"])
    core_candidates = _rank(
        [n for n in a1_defs if "core" in n.lower()],
        ["standard-a1-core-count"])
    micro_candidates = _rank(
        [n for n in micro_defs if "e21" in n.lower() or "e2" in n.lower()],
        ["standard-e21micro-count"])
    checks.append(("A1 OCPUs", core_candidates, a1_ocpus_needed, "OCPU"))
    checks.append(("A1 memory", mem_candidates, a1_mem_needed, "GB"))
    checks.append(("AMD micros", micro_candidates, micro_needed, "instance(s)"))

    for label, candidates, needed, unit in checks:
        found = None
        for cand in candidates:
            av = _availability(cand)
            if av is not None:
                found = (cand, av)
                break
        if found is None:
            print(f"::warning::quota for {label}: no matching service limit found "
                  f"(tried {candidates}) - skipping this check (non-fatal)")
            continue
        name, av = found
        used = float(av.used or 0)
        available = float(av.available if av.available is not None else 0)
        ok = available >= needed
        status = "OK " if ok else "SHORT"
        print(f"quota {name:34} used {used:g} / available {available:g} "
              f"{unit} (need {needed:g}) -> {status}")
        if not ok:
            print(f"::error title=Quota exhausted::service limit {name} has {available:g} {unit} "
                  f"available but this configuration needs {needed:g}. Launches will fail with "
                  f"LimitExceeded regardless of retries. Request a (free) limit increase: Console -> "
                  f"Governance -> Service limits, or remove other instances burning this quota.")
            rc = 2

    if rc == 0:
        print("Verdict: shapes supported and quota available - a launch attempt is worth making.")
        print("NOTE: physical host capacity cannot be pre-checked (no public OCI API).")
        print("      If apply still hits 'Out of host capacity', the AD's hosts are dry:")
        print("      -> set repo variable OFF_PEAK_PROVISION=true (auto-hunts at 04:00 local),")
        print("      -> or use another AD (multi-AD regions only),")
        print("      -> or upgrade to Pay As You Go (Always Free resources stay $0; launch priority improves).")
    return rc


if __name__ == "__main__":
    sys.exit(main())
