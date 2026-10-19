# Troubleshooting

Symptom-first guide. When in doubt: `make doctor` (or run
`scripts/doctor.sh` on the VM) and `make logs`.

## Provisioning

### "Out of host capacity" when creating the A1 instance

Ubiquitous for Always Free A1. **What it means** (official FAQ wording): a
temporary lack of Always Free shapes **in your home region** — not a bug, not
your quota. Research-backed notes:

* **No OCI API can pre-check physical host capacity.** Shape support
  (`oci compute shape list`) and tenancy quota (Limits API) are checkable —
  the Provision/Plan workflows now run both as a "capacity pre-check" and
  abort early when *those* fail (retrying cannot fix a missing shape or a
  zero quota). Green pre-check + failed launch = the hosts themselves are
  dry, and only the options below help.
* **Single-AD regions (e.g. ap-singapore-1) have no AD rotation.** Community
  reports show Singapore can stay dry for weeks (one user retried every
  5 minutes for a month without landing). Plan accordingly.

In order of effectiveness:

1. **Scheduled capacity hunt (automated, free, already enabled)** — the
   Provision workflow runs itself **~10×/day** (every 2–3 h around the
   clock; see the `schedule:` block in `.github/workflows/provision.yml`)
   with bounded launch retries per run. It is fully automatic: an
   **auto-quit guard** (first job — one OCI API call via
   `scripts/already_provisioned.py`) skips plan/apply/deploy once the
   stack exists, and a dry hunt ends green with a "Capacity dry" warning
   instead of failure alerts. No repo variable needed; pause anytime via
   Actions → Provision → "..." → *Disable workflow*. Caveat: GitHub
   auto-disables schedules after 60 days of repo inactivity (it emails you
   first) — any commit or a one-click re-enable restarts the hunt.
2. **Retry now, bounded** — re-run Provision with a higher `capacity_retries`
   input. Capacity frees up constantly; `scripts/capacity_hunter.sh` also
   automates this from Cloud Shell:
   ```bash
   make hunt COMPARTMENT=ocid1.compartment.oc1..xxxx
   ```
3. **Try another availability domain** — `availability_domain_index = 1` or
   `2` (only in multi-AD regions; check the Inspect workflow's AD report).
4. **Upgrade to Pay As You Go** — the reliable fix. Capacity priority
   improves dramatically and Always Free resources stay free (official FAQ:
   "with a paid account, you'll continue to have access to Always Free
   resources, and you'll only pay for the standard resources you use"). PAYG
   is also exempt from idle reclamation. Set a [budget alert](COST.md) first.
   (Unofficially, PAYG accounts have also been allowed to keep 4 OCPU/24 GB
   free since the June 2026 change — confirm with support before relying on
   it.)
5. **New account in a quieter home region** — last resort (one account per
   person per Oracle policy; also your current state bucket/secrets would
   need recreating). Community-reported roomy regions: us-phoenix-1,
   us-chicago-1, eu-frankfurt-1. Singapore/Tokyo/Sydney are chronically dry.

A cancelled Provision run can leave a stale state lock — the **Unlock state**
workflow releases it automatically (safe to run anytime).

### Terraform auth errors (`401 NotAuthorizedOrNotFound`)

* Cloud Shell: `export TF_VAR_auth=SecurityToken`.
* Local: create an API signing key (Console → Profile → API keys), fill
  `tenancy_ocid`, `user_ocid`, `fingerprint`, `private_key_path` in
  `terraform.tfvars`.
* Identity resources (dynamic group / policy) need admin or a
  tenancy-level compartment — new accounts have this by default. Otherwise
  set `create_waker_iam = false` and create the policy manually (copy the
  statements from `terraform/main.tf`).

## Controller / runners

### Jobs stay "Queued", no runner appears

1. `make logs` — look for `queued jobs=N` lines. If `queued=0` forever:
   * `MATCH_LABELS` must match a label your workflow actually requests
     (`runs-on: [self-hosted, oracle-a1]` ↔ `MATCH_LABELS=oracle-a1`).
   * Token may lack repo access (PAT scope, or the repo is not in
     `GITHUB_REPOSITORIES`).
2. `spawn paused (cooldown)` in logs → registration failing repeatedly;
   check `GITHUB_PAT` validity (PATs expire!) and that
   `oci-runner/runner:latest` exists (`docker images`).
3. Runner registered but "Idle" while jobs queue → label mismatch: check
   the runner's labels on GitHub (repo → Settings → Actions → Runners)
   versus your workflow's `runs-on`.

### `docker logs` shows 401 from GitHub

* PAT: expired or revoked — create a new one, update `.env`, `make restart`.
* App: private key/ID/installation-ID mismatch; regenerate carefully.
* `docker exec oci-runner-controller python app_install_id.py owner/repo`
  verifies app credentials end-to-end.

### Controller exits with "docker daemon is not reachable"

The container needs the socket mount (default in `deploy/compose.yml`).
If you edited compose, keep `- /var/run/docker.sock:/var/run/docker.sock`.

### Jobs fail with "no space left on device"

```bash
make prune              # immediate
df -h /                 # check trend
docker system df        # what's holding space
```
The controller prunes every 6 h by itself (plus a weekly cron from
cloud-init). Persistent growth usually = huge build artifacts inside
`_work` (should vanish with ephemeral containers) or an actions/cache
misconfiguration — prefer GitHub-hosted cache (`cache:` in setup actions).

### A job got OOM-killed (`exit 137`, `dmesg` shows oom-killer)

* Lower `MAX_RUNNERS` (2 runners × 5 GB on a 12 GB VM leaves ~1.5 GB for
  the OS + controller — that's the intended ceiling), or lower
  `RUNNER_MEMORY_GB` and run fewer parallel jobs.
* Switch to the `burst` profile (4 OCPU / 24 GB) if your jobs are genuinely
  big; it's the same free allowance when CI is sporadic.

### An action fails with `exec format error`

That action ships amd64-only binaries. Fixes: upgrade the action (many have
arm64 builds now), run the job in a multi-arch container
(`container: node:22`), or wrap the step in
`docker run --platform linux/amd64 …` (QEMU, ~5-10× slower — binfmt is
already registered on the VM).

### Multi-arch docker build fails on `--platform linux/amd64`

```bash
ssh ubuntu@<a1> 'ls /proc/sys/fs/binfmt_misc/ | grep qemu'   # should list qemu-x86_64
sudo apt install -y qemu-user-static binfmt-support           # if missing
```

## Waker / burst profile

### Waker can't start the A1 (`instance_action` 401/404)

* Instance-principal auth needs the Terraform-created dynamic group + policy
  (`create_waker_iam = true`, default). Check: Console → Identity → Dynamic
  Groups → `gh-runner-waker-dg` contains the waker instance; the policy
  statements are in `terraform/main.tf`.
* `A1_INSTANCE_ID` in `waker.env` must be the A1's OCID
  (`terraform output a1_instance_id`).
* Manual fallback: put an API-key profile in `/home/ubuntu/.oci/config` on
  the waker and set `OCI_CONFIG_PROFILE=DEFAULT`.

### The A1 never powers off (burst)

* `/etc/oci-runner/idle-stop.enabled` must exist (install.sh creates it when
  the VM's profile file says `burst` — which terraform writes from
  `runner_profile`), and `ENABLE_IDLE_STOP=true` in `.env`.
* Check the timer: `systemctl list-timers | grep idle` and
  `journalctl -u oci-runner-idle-stop.service`.

### The A1 powers off mid-job

Shouldn't happen — the controller only approves shutdown when there are no
queued jobs, no busy runners and no running containers. If a job queued in
the exact second of poweroff, the waker re-boots the VM within ~20 s and the
job runs then. For extra safety raise `IDLE_STOP_MINUTES`.

## VM oddities

### Instance was stopped overnight and you didn't ask for it

That's Oracle's idle reclamation (free-tier accounts, 7-day <20% utilization
— see COST.md). Fix: `ALWAYS_RESUME=true` in `waker.env` (self-healing) or
upgrade to PAYG.

### After a VM reboot, in-flight jobs failed

Containers die with the VM (by design — ephemeral). GitHub re-queues *new*
runs; a job that was mid-flight marks failed after the runner goes offline.
Rare on a dedicated CI box; enable `ALWAYS_RESUME` + the waker to minimise
downtime. Reboot latency ≈ 90 s + runner registration.

### `make status` shows nothing

The status page is bound to `127.0.0.1:8080` on the VM; `make status` opens
an SSH tunnel. If you set `STATUS_TOKEN`, append `?token=…` to the URL.
For an always-visible page use the **waker** (`make status-waker`) and
optionally `status_public = true` in terraform.

## CI-driven provisioning (GitHub Actions)

Primary reference: [CI_DEPLOYMENT.md](CI_DEPLOYMENT.md).

### The over-provision guard fired (`preflight_check.py` / "Over-provision guard")

This is the *auto-check for already-provisioned servers* doing its job: the
run stopped instead of creating a duplicate VM. Two places can fire it:

* **`scripts/preflight_check.py`** (runs in Provision/Plan before plan/apply).
  Match the exit code / log line:
  - `exit 4 — multiple project servers` / `Duplicate project server detected`:
    more than one `gh-runner-a1*` (or `gh-runner-waker*`) instance is live, or
    one exists outside terraform state while state tracks another. Terminate
    the surplus instance in the OCI console (Compute → Instances), then re-run
    Provision. If terraform state is the stale side, `terraform -chdir=terraform
    state rm oci_core_instance.<address>` first and let the preflight adopt.
  - `exit 5 — budget exceeded`: live A1 OCPUs (+ planned) would pass
    `overprovision_max_ocpus` (default 4 = the post-June-2026 Always Free
    envelope), or a 3rd AMD micro would be created. Terminate/shrink other A1
    instances, or set `TF_VAR_overprovision_max_ocpus` deliberately.
  - `exit 3 — untracked servers + abort`: servers exist but state is empty and
    `PREFLIGHT_ON_UNTRACKED=abort` was set. CI's default is `adopt`
    (auto-`terraform import`), which needs no action.
  - `exit 6 — adoption failed`: a `terraform import` errored (often IAM
    permissions or an ambiguous duplicate name). Read the step log; fix, then
    re-run — imports already done are skipped.
* **`terraform plan` itself fails** with `Over-provision guard:` — that is the
  plan-time precondition layer in `terraform/main.tf` (independent of state
  and of the script). Same remedies as above; the message names the exact
  condition and counts.

Debug any time from a machine with the `OCI_*` env vars:

```bash
python3 scripts/preflight_check.py --json          # machine-readable findings
python3 scripts/preflight_check.py --on-untracked abort   # never imports
```

`scripts/capacity_hunter.sh` has the same guard: it refuses to hunt while any
live A1 exists in the compartment (`--allow-existing` to override — only sane
when you consciously want to pay, or after terminating the old one).

### Preflight fails with `Missing secret: X`

Add the named repo secret (Settings → Secrets and variables → Actions).
The table in CI_DEPLOYMENT.md §2 lists all of them and where each value
comes from.

### Bootstrap fails with exit code 2 and no visible error

The original Bootstrap swallowed `oci` CLI output with `>/dev/null`, and
`oci os bucket create` uses **`--name`** (not `--bucket-name`) — the resulting
usage error goes to *stdout* (so the redirect hid it) and exits with code 2,
producing a silent red ❌. Bootstrap now captures and prints all `oci` output
on failure. If you see this on an old revision, update to the current
`main` and re-run — it is idempotent.

General rule kept in all workflows: never redirect `oci` command output to
`/dev/null` — usage errors print to stdout, service errors to stderr; capture
both and echo them on failure.

### `terraform init` fails (404/403 on the state bucket)

* `OCI_NAMESPACE` must match exactly what the **Bootstrap** summary printed.
* The bucket `oci-provision-tfstate2` must exist — re-run **Bootstrap**
  (idempotent; it now accepts a `bucket_name` input to probe a fresh name).
* **Deleted-bucket name reservation:** if a bucket was deleted (e.g. while
  cleaning up), OCI keeps the name reserved for a while — `bucket get`
  returns 404 *and* `bucket create` returns 409 `BucketAlreadyExists` at the
  same time, **for that one previously-used name**. Either wait (minutes to
  hours) or move to a fresh bucket name (dispatch **Bootstrap** with a
  `bucket_name` input to test/probe; for a *permanent* switch also update
  the `TF_STATE_BUCKET` default in `scripts/tf_init.sh` and the other
  workflow env blocks). This is why the repo moved from
  `oci-provision-tfstate` to `oci-provision-tfstate2`. Losing the state
  file is not fatal: the next Provision run's preflight auto-adopts the live
  resources via `terraform import`.
* **Account write-block (all Object Storage writes denied):** if `bucket
  create` returns 409 `BucketAlreadyExists` ("either exists or you are not
  authorized to create it") even for a **fresh, never-used name** — while
  `oci os ns get` and `oci os bucket list` still succeed — the *account*
  is refusing writes, not the name. A second signature: a deleted bucket
  stuck in the `bucket list` output for many hours (a ghost bucket: it
  lists, but `get` 404s). Causes: free-tier account suspension/
  restriction, a tenancy termination in progress, or the API user's IAM
  permissions were demoted. Nothing in this repo can fix that from CI —
  check in the OCI console:
  1. Try creating a bucket **by hand** (Object Storage → Create Bucket).
     If the console *also* refuses → account-level block: look for a
     termination/suspension banner (Profile → Tenancy, Account Management),
     settle billing/identity verification, or open a support request. If
     the console *works* → the API user lost permission: put user
     `OCI_USER_OCID` back in a group with
     `Allow group <group> to manage buckets in tenancy`.
  2. Once writes work again: re-run **Bootstrap** (fresh `bucket_name` if
     the old name is still reserved), then run **Provision** — preflight
     auto-adopts the live VCN/subnet and the scheduled hunt resumes on its
     own. Until then every scheduled run will fail red at `terraform init`
     by design (visible, not swallowed); disable the Provision workflow if
     the emails get noisy.
* `OCI_PRIVATE_KEY` must be the full PEM including BEGIN/END lines, and
  `OCI_FINGERPRINT` must match that key pair.

### Apply fails with `Out of host capacity` (after the bounded retries)

Apply stops after `capacity_retries` attempts (default 6 — no more hour-long
grinds; retrying a dry AD rarely lands) and the run ends **green** with a
"Capacity dry" warning — by design, so the ~10 scheduled hunts/day do not
spam failure alerts. The schedule keeps hunting automatically; other levers:
rotate AD in multi-AD regions, or upgrade to PAYG for the fast path (decision
matrix at the top of this file). The **capacity pre-check** step in the
Provision/Plan logs also shows exactly which shapes/quota your AD has.

Last resort: `scripts/capacity_hunter.sh` in OCI Cloud Shell (creates an
instance outside terraform state — the Provision preflight auto-adopts it on
the next run, or `terraform import` manually).

### Apply fails with `LimitExceeded` / quota

Some brand-new free-tier tenancies ship with an A1 OCPU limit of 0. Request
a (free) increase: Console → Governance → Service limits → Request service
limit update → `Standard.A1.Flex`. Re-run **Provision** after approval.

### deploy-stack: `could not reach ubuntu@<ip> within 10 minutes`

cloud-init was still running or failed. Check the instance console output
(Compute → Instances → the A1 → console). If cloud-init errored (apt/docker
hiccups), SSH in via the **Get SSH key** workflow, fix or just re-run the
**Deploy stack** workflow once the VM is healthy.

### Jobs queue but never get picked up

* Label mismatch: workflow must say `runs-on: [self-hosted, oracle-a1]`;
  controller `MATCH_LABELS` defaults to `oracle-a1`.
* `RUNNER_GITHUB_PAT` expired or lost repo access → update the secret and
  re-run **Deploy stack**.
* Repo not listed in `GITHUB_REPOSITORIES` → re-run **Deploy stack** with
  the right `repositories` input.

### Rollback/destroy fails on an instance that was deleted manually

Re-run **Rollback** once — terraform refreshes and continues. For a
stubborn resource, remove it from state then destroy the rest:

```
# from a machine with the OCI_* env (or Cloud Shell with the state pulled):
terraform -chdir=terraform state rm oci_core_instance.a1
terraform -chdir=terraform destroy
```

### Importing a manually-hunted instance into terraform state

If you used `scripts/capacity_hunter.sh`:

```
terraform -chdir=terraform import oci_core_instance.a1 ocid1.instance.oc1...
```

Then re-run **Provision** to attach the VCN/security-list-managed parts it
may be missing (the hunter's instance uses its own hunt VCN — simplest is
to destroy it and let Provision recreate, hunting only buys you a foot in
the door in truly dry regions).

