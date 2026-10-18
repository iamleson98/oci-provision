# CI deployment guide — provision & rollback from GitHub Actions

This is the complete, browser-only setup. After Step 3 you never need to clone
this repo or run terraform anywhere: provisioning, configuration, health
checks and rollback are all workflow runs.

> Everything below was verified against live Oracle & GitHub docs in October
> 2026 (see [RESEARCH_NOTES.md](RESEARCH_NOTES.md)).

---

## 0. How it works

```
┌──────────────────────────── GitHub ────────────────────────────┐
│  Secrets: OCI API key, PAT, namespace                          │
│                                                                │
│  Bootstrap ──► creates versioned bucket oci-provision-tfstate  │
│  Provision ──► terraform init (state+lock in bucket)           │
│                terraform apply  ──► OCI: VCN, A1, waker, IAM   │
│                deploy-stack job ──► SSH/rsync to the VMs       │
│                                      cloud-init wait, install  │
│                                      controller + waker envs   │
│  Rollback  ──► terraform destroy (DESTROY confirmation)        │
│  Doctor    ──► weekly SSH health check                         │
└────────────────────────────────────────────────────────────────┘
```

* **Terraform state** lives in OCI Object Storage using the **native `oci`
  backend** (Terraform ≥ 1.12): real state **locking** (no two workflow runs
  can corrupt state), and bucket **versioning** protects history. The bucket
  is created by the Bootstrap workflow.
* **Workflows are serialized** (concurrency group `oci-provision-tf`) — plus
  the backend's own locking — so plan/apply/destroy never race each other.
* **SSH key**: if you don't provide one, terraform generates a dedicated
  ED25519 keypair. The public half goes onto both VMs; the private half is
  read from state by the deploy job (never printed to logs) and is available
  to you via the **Get SSH key** workflow (1-day artifact).
* Provisioning workflows run on `ubuntu-latest` GitHub-hosted runners.
  **Public repos get these for free, unlimited** (this repo is public).
  On a private repo they consume the 2,000 free minutes/month of the GitHub
  Free plan — a provision run uses ~10-15 min.

---

## 1. Prerequisites (browser)

1. **An OCI account** — <https://www.oracle.com/cloud/free/>. A credit card
   is required for identity verification (~$1 temporary hold, then removed).
   Pick your **home region** wisely: it cannot be changed later, and A1
   capacity is easier to find in quieter regions.
2. **This repo** (you're reading it) on the default branch.
3. A **GitHub PAT** for the runner controller (Step 2.7 below).
4. Nothing else — no CLI, no terraform, no laptop.

---

## 2. Configure the secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**.

First create the OCI API signing key (5 minutes, console only):

1. Log into the OCI Console → click your **profile icon (top right) → My profile & settings**.
2. Left menu, **Resources → API keys → Add API Key** → "Generate API key pair".
3. **Download the private key** (`oci_api_key.pem`) — you will paste its
   contents into a secret next.
4. After adding, OCI shows a configuration snippet — copy the values from it.

Now add the secrets:

| Secret | Value (where to find it) | Required |
|---|---|---|
| `OCI_TENANCY_OCID` | `ocid1.tenancy.oc1...` (from the API key snippet, or Console → Profile → Tenancy) | ✅ |
| `OCI_USER_OCID` | `ocid1.user.oc1...` (from the snippet) | ✅ |
| `OCI_FINGERPRINT` | e.g. `aa:bb:cc:...` (from the snippet) | ✅ |
| `OCI_PRIVATE_KEY` | The **entire contents** of the downloaded `oci_api_key.pem`, including the `-----BEGIN ... -----` lines. Multi-line paste is fine; literal `\n` escapes are also handled. | ✅ |
| `OCI_REGION` | Your home region, e.g. `us-ashburn-1` | ✅ |
| `RUNNER_GITHUB_PAT` | A **classic PAT with `repo` scope** (GitHub → Settings → Developer settings → Tokens). Used by the controller on the VM to register runners and read job queues. Fine-grained PATs also work: "Administration: Read & write", "Actions: Read", "Metadata: Read" on the target repos. | ✅ |
| `OCI_NAMESPACE` | **Added after Step 3** (Bootstrap prints it) | ✅ later |
| `OCI_COMPARTMENT` | Compartment OCID if you don't want the tenancy root. Leave out otherwise. | optional |
| `SSH_PUBLIC_KEY` / `SSH_PRIVATE_KEY` | Provide your own keypair instead of the terraform-generated one (`ssh-ed25519 AAAA...` content / the private key file contents). | optional |
| `WAKER_STATUS_TOKEN` | Shared secret for the waker status page (only relevant with `status_public=true` in terraform). | optional |

Notes:

* **PAT rotation**: classic PATs expire (90 days default — you can set
  custom/no expiry). When it expires, update `RUNNER_GITHUB_PAT` and re-run
  **Deploy stack**. Using the *same* PAT for pushing and running is possible
  but a second, dedicated PAT is cleaner.
* The OCI API key you created is the **user key for automation** — it is the
  credential ALL terraform operations (provision/rollback) use. Protect it
  like a root credential; you can always delete it in the console to freeze
  CI access.

---

## 3. Run Bootstrap (once)

Repo → **Actions** tab → **Bootstrap (one-time)** → **Run workflow** → Run.

It will:

1. Verify the five OCI secrets work (fails loudly with a red ❌ annotation if
   any is missing/wrong).
2. Create bucket **`oci-provision-tfstate`** in your tenancy root compartment,
   with **versioning enabled** (state history recoverable).
3. Print your Object Storage **namespace** in the job summary.

Then: add that namespace as the secret **`OCI_NAMESPACE`** (exact string from
the summary). Done — never run Bootstrap again (safe to re-run idempotently,
e.g. to fix versioning).

---

## 4. Run Provision

Actions → **Provision** → **Run workflow** → choose inputs → Run.

Inputs:

| Input | Default | Notes |
|---|---|---|
| `runner_profile` | `always-on` | `always-on` = 2 OCPU/12 GB 24/7 (fits the 1,500 OCPU-h/month free allowance on any account). `burst` = 4 OCPU/24 GB, woken on demand by the waker, powered off when idle — same free budget, faster jobs. |
| `enable_waker` | `true` | The free AMD micro: self-heals the A1 (restarts it if Oracle stops it), wakes it in burst mode, serves a 24/7 status page. |
| `availability_domain_index` | `0` | Only change if you hit "Out of capacity" — try `1`, then `2`. |
| `admin_cidr` | `0.0.0.0/0` | Better: `your.ip.add.ress/32` — narrows SSH (and status) exposure. |
| `repositories` | *(this repo)* | `owner/repo[,owner/repo2]` the runners will serve. You can change this anytime by re-running **Deploy stack** — no reprovision needed. |
| `max_runners` | *(profile default)* | 2 for always-on, 4 for burst. Lower it if jobs are memory-hungry. |

What the run does (≈ 10-20 min end to end):

1. `terraform plan` — visible in the log, sanity check.
2. `terraform apply` — **retries up to 20 times (~30 min) when OCI reports
   "Out of host capacity"** (the notorious A1 issue). If it still fails:
   re-run with a different `availability_domain_index`, or wait a few hours,
   or (fastest fix) upgrade the OCI account to Pay-As-You-Go — Always Free
   resources stay free, capacity priority improves dramatically.
3. Cloud-init hardens and sets up both VMs (docker, ufw, fail2ban, unattended
   upgrades, qemu for multi-arch builds, swap).
4. The **deploy-stack** job waits for cloud-init, rsyncs this repo to
   `/opt/oci-runner`, writes the controller `.env` (PAT, repos, pool sizing
   from your profile), builds the runner image, and starts everything; then
   the same for the waker with `ALWAYS_RESUME=true` (self-healing).
5. The summary shows the VM IPs.

**If the workflow fails at apply with `LimitExceeded` / quota:** some new
free-tier tenancies ship with an A1 OCPU service limit of 0 — request an
increase (free): Console → Governance → Service limits → "Request service
limit update" for `Standard.A1.Flex`. Retry Provision after approval.

### 4.1 Point your CI at the runners

In any repo listed in `repositories`, add e.g. `.github/workflows/ci.yml`
(see [`examples/ci-node.yml`](../examples/ci-node.yml)):

```yaml
on: [push]
jobs:
  test:
    runs-on: [self-hosted, oracle-a1]   # the custom label
    timeout-minutes: 20                 # always set timeouts on self-hosted runners
    steps:
      - uses: actions/checkout@v4
      - run: make test
```

Push and watch it land on your Oracle runner (Actions tab of that repo → the
job will show a "self-hosted" runner badge; or the **Doctor** workflow /
status page). Docker-in-CI (`docker build`) works out of the box via the
host socket; multi-arch builds work (qemu registered).

---

## 5. Rollback

Actions → **Rollback (destroy)** → Run workflow → type `DESTROY` in the
`confirm` input → Run.

* Terminates both VMs (boot volumes deleted), removes the VCN, security list,
  dynamic group and policy.
* The empty state file + its version history stay in the bucket (tiny,
  free). Re-running **Provision** rebuilds everything from scratch.
* **Config rollback** (smaller blast radius): revert the offending commit on
  the default branch and re-run **Provision** — terraform converges to the
  old configuration without destroying the VMs (in-place changes like boot
  volume size force instance replacement; terraform's plan output will show
  exactly what would happen — use the **Plan** workflow to preview).

---

## 6. Day-2 operations (all from the Actions tab)

| Task | Workflow | Notes |
|---|---|---|
| Change repos / pool sizing / memory | **Deploy stack** | No infra changes; rewrites controller env, restarts services |
| Upgrade the runner agent | edit `RUNNER_VERSION`+`RUNNER_CHECKSUM` in `runner-image/Dockerfile`, merge, run **Deploy stack** | Checksum from the [actions/runner releases](https://github.com/actions/runner/releases) page |
| Health check | **Doctor** (weekly Mondays 06:00 UTC + manual) | Disk, docker, controller, security posture; optional docker prune input |
| Manual SSH | **Get SSH key** → download artifact → `ssh -i key.pem ubuntu@<ip>` | Artifact auto-expires in 1 day |
| Preview changes | **Plan** | `terraform plan` against live state |
| Status page | `ssh -N -L 8080:localhost:8080 ubuntu@<a1-ip>` → http://localhost:8080 | Or expose it publicly: set `status_public` terraform var + `WAKER_STATUS_TOKEN` secret (requires editing terraform + Provision) |

### Tuning knobs (Deploy stack inputs → controller env)

* `min_runners` — warm runners kept ready (default 1 → instant pickup).
* `max_runners` — hard ceiling; each runner is capped at `runner_memory_gb`
  (default 5 GB) so 2 runners fit the 12 GB always-on VM with OS headroom.
* `repositories` — comma-separated list; the autoscaler polls each repo's
  queue and spawns runners weighted by queue depth.

---

## 7. Security

* **Ephemeral runners**: every job gets a fresh container; the runner
  deregisters itself and the container is removed (`docker rm -v`) after the
  job. No state leaks between jobs.
* **Untrusted code**: if any served repo accepts outside contributions,
  enable *Settings → Actions → "Require approval for all outside
  collaborators"* in that repo. Self-hosted runners + fork PRs without
  approval are a known RCE vector.
* **docker socket**: runner containers mount `/var/run/docker.sock`
  (root-equivalent on the host) so jobs can `docker build`. Fine for a
  single-user hobby fleet; set `MOUNT_DOCKER_SOCK=false` in the controller
  env if you don't need it.
* **Secrets surface**: `RUNNER_GITHUB_PAT` lands in root-readable env files
  on your VMs (0600); the generated SSH private key lives in the private,
  versioned state bucket; GitHub masks secrets in workflow logs.
* **VM hardening** (cloud-init): SSH key-only + fail2ban + ufw (port 22
  only) + unattended security upgrades + docker log rotation + swap.
* **Rotate credentials** periodically: the PAT (created with expiry), and
  the OCI API key if it ever leaks (delete in console → new key → update
  4 secrets).

---

## 8. Troubleshooting quick hits

Full guide: [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Most common:

| Symptom | Fix |
|---|---|
| Bootstrap/preflight red ❌ `Missing secret` | Add the named secret (table in §2) |
| Apply: `Out of host capacity` (retries exhausted) | Re-run with different `availability_domain_index`; wait; or upgrade to PAYG (free resources stay free) |
| Apply: `LimitExceeded` | Request A1 service-limit increase (free, §4) — then re-run |
| `terraform init` 404/403 on bucket | Re-run **Bootstrap** (creates bucket); check `OCI_NAMESPACE` matches exactly |
| deploy-stack: `could not reach ubuntu@...` | Instance still booting/cloud-init failed — check the VM console; re-run **Deploy stack** after cloud-init is healthy |
| Jobs stay queued | Label mismatch: workflow must use `runs-on: [self-hosted, oracle-a1]` and `MATCH_LABELS=oracle-a1`; or PAT lost repo access (expired) → update secret + Deploy stack |
| destroy fails on already-terminated instance | Re-run Rollback once (terraform refreshes); stubborn state: `terraform state rm oci_core_instance.a1` locally or via a Plan run's log guidance |
