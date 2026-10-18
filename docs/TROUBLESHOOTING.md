# Troubleshooting

Symptom-first guide. When in doubt: `make doctor` (or run
`scripts/doctor.sh` on the VM) and `make logs`.

## Provisioning

### "Out of host capacity" when creating the A1 instance

Ubiquitous for Always Free A1. In order of effectiveness:

1. **Retry** — capacity frees up constantly; `scripts/capacity_hunter.sh`
   automates this (Cloud Shell has the OCI CLI ready):
   ```bash
   make hunt COMPARTMENT=ocid1.compartment.oc1..xxxx
   ```
2. **Try another availability domain** — `availability_domain_index = 1` or `2`
   in `terraform.tfvars`.
3. **Try another region** — home region can't change later, but a new
   account in a quieter region has far better odds. Also see
   `scripts/capacity_hunter.sh --fallbacks "2,12;1,6"` to accept a smaller
   shape now and resize later.
4. **Upgrade to Pay As You Go** — the reliable fix. Capacity priority
   improves dramatically and Always Free resources stay free. Set a
   [budget alert](COST.md) first. (Unofficially, PAYG accounts have also
   been allowed to keep 4 OCPU/24 GB free since the June 2026 change —
   confirm with support before relying on it.)

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

### Preflight fails with `Missing secret: X`

Add the named repo secret (Settings → Secrets and variables → Actions).
The table in CI_DEPLOYMENT.md §2 lists all of them and where each value
comes from.

### `terraform init` fails (404/403 on the state bucket)

* `OCI_NAMESPACE` must match exactly what the **Bootstrap** summary printed.
* The bucket `oci-provision-tfstate` must exist — re-run **Bootstrap**
  (idempotent).
* `OCI_PRIVATE_KEY` must be the full PEM including BEGIN/END lines, and
  `OCI_FINGERPRINT` must match that key pair.

### Apply fails with `Out of host capacity` (after ~30 min of retries)

1. Re-run **Provision** with `availability_domain_index` = 1, then 2.
2. Still stuck? The region is genuinely dry — wait a few hours and re-run,
   or create a new account in a quieter home region, or **upgrade to Pay As
   You Go** (free resources stay free; capacity priority jumps; PAYG is also
   exempt from idle reclamation).
3. Last resort: `scripts/capacity_hunter.sh` in OCI Cloud Shell (creates an
   instance outside terraform state — `terraform import` it afterwards, see
   below).

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
terraform -ch-dir=terraform state rm oci_core_instance.a1
terraform -ch-dir=terraform destroy
```

### Importing a manually-hunted instance into terraform state

If you used `scripts/capacity_hunter.sh`:

```
terraform -ch-dir=terraform import oci_core_instance.a1 ocid1.instance.oc1...
```

Then re-run **Provision** to attach the VCN/security-list-managed parts it
may be missing (the hunter's instance uses its own hunt VCN — simplest is
to destroy it and let Provision recreate, hunting only buys you a foot in
the door in truly dry regions).

