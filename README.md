# oci-provision

**Free GitHub Actions runners on Oracle Cloud Always Free — provisioned, deployed and rolled back entirely from GitHub Actions. No laptop, no local terraform.**

> Verified against Oracle's live docs and GitHub's billing docs, October 2026 — see [docs/RESEARCH_NOTES.md](docs/RESEARCH_NOTES.md) and [docs/COST.md](docs/COST.md).

```
 GitHub.com                                          Oracle Cloud (Always Free, $0)
 ─────────                                           ──────────────────────────────
 you click "Provision" ──► Actions workflow ──► terraform ──► ┌──────────────────────┐
 (ubuntu-latest, free)    (state + locking in      (apply)    │  Ampere A1 worker    │
                          OCI Object Storage)                 │  2 OCPU / 12 GB arm64│
                                                             │  ┌────────────────┐  │
 your repos' CI  ◄────── self-hosted runners ◄─────────────── │  │ ephemeral      │  │
 (runs-on: oracle-a1)      (registered by controller)        │  │ 1 job each     │  │
                                                             │  └────────────────┘  │
 rollback = "Rollback" workflow ──► terraform destroy         └──────────┬───────────┘
                                                                        │ wakes/self-heals
                                                             ┌──────────▼───────────┐
                                                             │ AMD micro "waker"    │
                                                             │ 1/8 OCPU / 1 GB, 24/7│
                                                             └──────────────────────┘
```

## What you get

| Piece | Where | What it does |
|---|---|---|
| **Bootstrap** workflow | GitHub Actions | Creates the versioned Object Storage bucket for terraform state, prints your namespace |
| **Provision** workflow | GitHub Actions | **Auto-checks what's already provisioned** (adopts or refuses to duplicate), then `terraform apply` (auto-retries on A1 "out of capacity") → VMs → then SSHes in and installs the whole runner stack |
| **Rollback** workflow | GitHub Actions | `terraform destroy` behind a `DESTROY` confirmation gate |
| **Deploy stack** workflow | GitHub Actions | Reconfigure/redeploy the runner stack without touching infra |
| **Doctor** workflow | GitHub Actions | Weekly + on-demand health check of the VM (disk, docker, controller, security) |
| **Get SSH key** workflow | GitHub Actions | The generated SSH private key as a 1-day artifact (for manual SSH) |
| **controller** | A1 VM (docker) | Queue-aware autoscaler: `desired = clamp(busy + queued, MIN..MAX)` ephemeral runner containers, token broker (PAT or GitHub App), pruning, status page |
| **waker** | Free AMD micro | 24/7 watchdog: self-heals a stopped A1, wakes it for queued jobs (burst mode), serves status |
| **runner image** | A1 VM (docker) | Pinned, checksum-verified `actions/runner` v2.338.0 (linux-arm64) + docker CLI/buildx/compose; one job per container, clean state every time |
| **terraform** | state in OCI | VCN, A1 worker, waker micro, least-privilege IAM, hardened cloud-init (ufw, fail2ban, unattended-upgrades, qemu binfmt, swap) |

Two profiles (workflow input):

* **`always-on`** *(default)* — A1 at **2 OCPU / 12 GB, 24/7**. Fits inside the post-June-2026 Always Free allowance (1,500 OCPU-h + 9,000 GB-h per month) on **any** account type. Zero billing risk.
* **`burst`** — A1 at **4 OCPU / 24 GB**, powered off when idle; the waker boots it when jobs queue (~2 min) and the controller powers it off after 20 idle minutes. Same free hour-budget, double the throughput while awake.

### Over-provision protection (auto-check: already provisioned?)

Every provisioning path refuses to create a second server when one already exists — three layers:

1. **CI preflight** (`scripts/preflight_check.py`, runs in Provision/Plan *before* plan/apply): queries the OCI API + terraform state and cross-checks them. Already provisioned → apply proceeds as a no-op refresh; state lost but servers exist → the existing stack is **auto-adopted via `terraform import`** instead of duplicated; a foreign duplicate or an Always Free budget breach (A1 OCPUs > 4 per tenancy, or a 3rd AMD micro) → the run **fails loudly**.
2. **Terraform-native guards** (`terraform/main.tf` plan-time preconditions): `plan` fails — so `apply` never runs — when a duplicate `gh-runner-a1*`/`gh-runner-waker*` instance is already live, or live A1 OCPUs + planned exceed `overprovision_max_ocpus` (default 4 = the post-June-2026 free envelope; set 2 on free-only accounts).
3. **capacity_hunter.sh guard**: the manual hunter refuses to start while any live A1 exists (override: `--allow-existing`).

Runner *containers* are equally protected at runtime: the controller clamps `desired = min(MAX, max(MIN, busy + queued))`.

## Quickstart (browser only)

Full detail in **[docs/CI_DEPLOYMENT.md](docs/CI_DEPLOYMENT.md)** — the short version:

1. **Add secrets** (repo → Settings → Secrets and variables → Actions): `OCI_TENANCY_OCID`, `OCI_USER_OCID`, `OCI_FINGERPRINT`, `OCI_PRIVATE_KEY` (API signing key — created in the OCI console under your profile), `OCI_REGION`, and `RUNNER_GITHUB_PAT` (a classic PAT with `repo` scope).
2. Run the **Bootstrap** workflow once → it creates the versioned state bucket and auto-saves secret `OCI_NAMESPACE` (using `RUNNER_GITHUB_PAT`; falls back to printing the value for manual entry).
3. Run the **Provision** workflow (default inputs are fine) → ~15 min later the summary shows your VM IPs and the runner stack is live.
4. In the repo you want CI'd, use:

```yaml
jobs:
  test:
    runs-on: [self-hosted, oracle-a1]
    steps:
      - uses: actions/checkout@v4
      ...
```

Copy a ready-made workflow from [`examples/`](examples/). Roll back anytime with the **Rollback** workflow.

## Using the free tier to the fullest (verified limits)

| Resource | This stack uses | Always Free allowance |
|---|---|---|
| A1 compute | 1,488 OCPU-h + 8,928 GB-h / month (always-on 2/12) | **1,500 OCPU-h + 9,000 GB-h** |
| AMD micros | 1 (waker) | **2** (no hour metering) |
| Block storage | 50 GB (waker) + 147 GB (A1 boot) = 197 GB | **200 GB total** (boot volume API minimum: 50 GB) |
| Object storage | state bucket, KBs | **20 GB** + 50k requests/month |
| Outbound transfer | typical CI: 20-100 GB/month | **10 TB/month** |
| GitHub side | self-hosted runners: **$0** (public repos always free; private-repo charge postponed — see COST.md) | — |

Do **not** raise A1 OCPUs above 2 on a free-tier-only account (Oracle stops oversized instances since June 15, 2026). Use the `burst` profile for more muscle instead.

## Runner mode: why ephemeral Docker containers (not host-installed)

The runner stack deliberately runs each job in a **fresh, throwaway container**
(`--ephemeral` runners: register → serve exactly one job → container `rm -v`),
rather than installing a persistent runner service directly on the host. This
was a deliberate choice over host-mode:

* **Clean by construction** — every job gets a pristine workspace and runner
  binary state; after the job the container and its anonymous volumes are
  removed (`rm -v`), the controller prunes, and a weekly cron deep-cleans
  (`docker system prune --filter "until=168h"`). Nothing accumulates between
  jobs.
* **Fork/PR safety posture** — a container that only lives for one job limits
  what any single malicious job can persist on the host (caveat: containers
  here are a *freshness* control, not a hard security boundary — see below).
* **Auto-scaling** — the controller spawns one runner per queued job and
  reaps it after, clamped to free-tier-safe limits.

Host-mode (`systemd` runner service on the VM) is a valid lighter-weight setup
for trusted-only workloads, but it needs hand-rolled workspace cleanup,
per-job reregistration loops, and systemd resource caps to reach the same
hygiene. For this repo's goals (fork-safe, always-clean, $0) the container
approach is the better default.

### Making runners serve ONLY your repos (and never fork PRs)

1. **Repo allow-list is built in** — the controller only registers runners for
   the repos listed in `GITHUB_REPOSITORIES` (set by the Provision/Deploy
   workflows). No other repo can pick up the `oracle-a1` label.
2. **For your repos: require approval for fork PRs** (recommended) — repo →
   Settings → Actions → General → *Fork pull request workflows from outside
   collaborators* → **Require approval for all outside collaborators**. This
   makes fork-opened PRs wait for your click before any workflow (and thus any
   self-hosted job) runs.
3. **Never use `pull_request_target` with secrets** in a repo served by
   self-hosted runners — it runs workflow code from the PR *fork* with your
   base-repo secrets. The `pull_request` trigger (what `examples/` uses) runs
   the *base* repo's workflow file and is the safe default.
4. Optional hardening: keep `admin_cidr` tight, set `MOUNT_DOCKER_SOCK=false`
   if your jobs don't build images, and run the **Doctor** workflow weekly
   (it also prunes).

## Repo layout

```
.github/workflows/  bootstrap, plan, provision, rollback, deploy-stack, doctor, ssh-key
terraform/          VCN, A1 + waker, IAM, cloud-init (native `oci` state backend)
controller/         autoscaler + waker services (python)
runner-image/       ephemeral ARM64 runner image
deploy/             compose.yml, systemd units, install.sh, env templates
scripts/            tf_init.sh, ci_deploy.sh, capacity_hunter.sh, doctor.sh
examples/           drop-in workflow files for your repos
docs/               CI_DEPLOYMENT.md, COST.md, GITHUB_APP_SETUP.md, TROUBLESHOOTING.md, RESEARCH_NOTES.md
```

## Security notes

* **Read [docs/CI_DEPLOYMENT.md §Security](docs/CI_DEPLOYMENT.md#security) once.** Highlights: ephemeral one-job runners; never run untrusted fork-PR code without enabling approval requirements; runner containers mount the host docker socket (disable via `MOUNT_DOCKER_SOCK=false`).
* The generated SSH private key lives in the **private, versioned** state bucket and is only exposed via the masked `ssh-key` workflow artifact (1-day retention). Provide your own keypair (`SSH_PUBLIC_KEY`/`SSH_PRIVATE_KEY` secrets) if you prefer.
* Rotate the PAT you pasted into secrets periodically (classic PATs expire after 90 days anyway — that expiry is a feature).

## License

MIT — see [LICENSE](LICENSE).
