# Cost analysis: how this stays at $0/month

*Re-verified against live Oracle & GitHub docs on October 18, 2026. Sources
are linked in [RESEARCH_NOTES.md](RESEARCH_NOTES.md).*

## The June 2026 change you must know about

Oracle **halved the Ampere A1 Always Free allowance on June 15, 2026** —
silently, via a docs update (InfoQ, July 2026):

| | Before | Since June 15, 2026 |
|---|---|---|
| OCPU-hours / month | 3,000 (≈ 4 OCPU 24/7) | **1,500** |
| GB-hours / month | 18,000 (≈ 24 GB 24/7) | **9,000** |
| Practical meaning | one 4 OCPU / 24 GB VM always-on | **one 2 OCPU / 12 GB VM always-on**, or bigger shapes for fewer hours |

Consequences by account type:

* **Free-tier-only accounts**: an A1 instance above the allowance is simply
  **shut down by Oracle** until you resize it to fit within 2 OCPU/12 GB.
  No bill, but no runner either.
* **Pay-As-You-Go accounts**: "All tenancies get the first 1,500 OCPU hours
  and 9,000 GB hours per month for free" per the current docs. Multiple
  users reported Oracle support confirming **PAYG accounts may still run
  4 OCPU/24 GB free** (June 22, 2026) — but this is not officially
  documented. If you rely on it, verify with support and **set a budget
  alert** (below). A 4/24 VM running 24/7 on PAYG *without* grandfathering
  would cost roughly **$0.01/OCPU-h × ~1,476 excess OCPU-h ≈ $15/month**.

**This project's default (always-on 2 OCPU / 12 GB) is safe on both account
types**: 744 h (longest month) × 2 OCPU = 1,488 ≤ 1,500 OCPU-h and
744 × 12 GB = 8,928 ≤ 9,000 GB-h. That is a 99.2% commitment of the
allowance — fine for a dedicated CI box; if you want headroom for a second
A1 experiment, use the burst profile or drop to 1 OCPU / 6 GB
(`a1_ocpus`/`a1_memory_gbs` terraform variables).

## What the whole stack consumes ("fullest allowed" mode)

| Resource | This stack | Always Free allowance | Headroom |
|---|---|---|---|
| A1 OCPU-h / month | 1,488 (always-on 2/12) | 1,500 | ~0.8% |
| A1 GB-h / month | 8,928 | 9,000 | ~0.8% |
| AMD micro (waker) | 1 VM × 1/8 OCPU, 1 GB | 2 VMs, **no hour limit** | 1 micro spare |
| Block storage | 150 GB (A1 boot) + 47 GB (waker) | 200 GB total | ~3 GB (theoretical max: 153 GB A1 boot) |
| Object storage (state) | bucket `oci-provision-tfstate`, KBs + version history | 20 GB + 50,000 API requests/month | ~100% |
| Outbound transfer | typical CI: 20-100 GB/month | 10 TB / month | ~99% |
| Volume backups | 0 | 5 | — |

Burst profile math (`runner_profile = "burst"`): the allowance is a **budget
of hours**, not a fixed shape. A 4 OCPU/24 GB worker awake for
`H` hours/month costs `4×H` OCPU-h — e.g. **2 h/day of CI ≈ 240 OCPU-h**
(16% of the allowance). Sporadic-CI users keep ~85% of their free A1 budget
unspent and finish jobs twice as fast on the bigger shape. The only cost is
a ~2 minute wake-up latency per idle period (boot + docker + runner
registration).

## Idle reclamation (free-tier accounts)

Oracle reclaims **idle Always Free compute instances**: over a 7-day window,
95th-percentile CPU < 20% AND network < 20% AND memory < 20% (A1 shapes) →
instance gets **stopped** (data preserved, restart manually or… your waker).
Mitigations, pick any:

1. **Run real CI** — active runners comfortably exceed the thresholds.
2. **Use the waker as a self-healer** — `ALWAYS_RESUME=true` in
   `waker.env` restarts the A1 whenever it is unexpectedly stopped. This
   covers reclamation, crashes, and reboots with zero babysitting.
3. **Upgrade to PAYG** — PAYG tenancies are not subject to idle reclamation
   (and get capacity priority).

If you stay on a free-tier-only account and your CI is quiet for weeks at a
time, option 2 is the set-and-forget answer.

## Budget alert (do this on PAYG, 60 seconds)

Console → **Billing & Cost Management → Budgets → Create Budget**:

* Budget amount: `1` (your currency)
* Alert rule: actual spend > `0.01` → email you.

Everything Always Free stays $0; the alert catches accidental paid resources
(large boot volume, extra instances, a shape typo) before they cost real
money.

## The GitHub side of the bill (verified Oct 2026)

* **Self-hosted runner usage is free**, per GitHub's live billing docs
  ("GitHub Actions usage is free for self-hosted runners and for public
  repositories that use standard GitHub-hosted runners").
* **Timeline of the Dec 2025 price announcement** (worth knowing): on
  Dec 16, 2025 GitHub announced a $0.002/min "cloud platform" charge for
  self-hosted runner usage in **private** repos, effective Mar 1, 2026 —
  then **postponed it** the same month after community feedback ("We're
  postponing the announced billing change for self-hosted GitHub Actions
  to take time to re-evaluate our approach"). As of October 2026 the
  charge is not in effect and public-repo usage is explicitly excluded in
  any case. If it ever lands: it counts toward your plan's included
  minutes (2,000/month on the Free plan), and public repos stay free.
* **Provisioning workflows** (this repo) run on GitHub-hosted
  `ubuntu-latest` runners: **free for public repos** (this one), and ~10-15
  minutes per run against the 2,000 free monthly minutes of a private
  repo on the Free plan.
* What can still cost on GitHub: **Actions storage** (artifacts + cache
  beyond the free quota on private repos — public repos free). The example
  workflows use `actions/cache` and short `retention-days` to keep storage
  on GitHub's side and off your VM disk.
* Rate limits: PAT = 5,000 req/h. This stack polls twice per scope every
  15 s ≈ 480-960 req/h — comfortably inside. GitHub App installation tokens
  get a separate, higher limit.

## Efficiency choices baked into this repo

* **Ephemeral one-job runners** — no leaked state, no "runner offline mid-job"
  drift, containers removed with their workspace (`docker rm -v`).
* **Queue-driven scaling** — `desired = clamp(busy + queued, MIN, MAX)`:
  one warm runner for instant pickup, burst to `MAX` when a matrix job fans
  out, scale-down kills only *idle* runners (never busy ones).
* **Token caching** — registration tokens are fetched once per 45 min, not
  per spawn (each POST counts against your API budget).
* **Docker pruning** — the controller prunes images/volumes/build-cache
  every 6 h (keeping its own labeled images) and cloud-init adds a weekly
  deep clean. The 150 GB boot volume stays lean without manual care.
* **Per-runner memory caps** (`--memory 5g`) prevent one runaway job from
  OOM-killing the others; swap gives a soft landing.
* **`live-restore` + log rotation** in `daemon.json` — restarts don't kill
  running jobs; logs can't silently fill the disk.
* **concurrency + timeout in example workflows** — `cancel-in-progress`
  kills superseded runs; timeouts stop wedged jobs from burning free hours.
