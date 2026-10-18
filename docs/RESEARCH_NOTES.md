# Research notes (2026-10-18)

Primary sources consulted while designing this stack, with the decisions they
drove. Re-verify the Oracle numbers before relying on them — the 2026 change
proved they move quietly.

## Oracle Cloud Always Free (as of Oct 2026)

**Sources**

* Oracle marketing page, live crawl: <https://www.oracle.com/cloud/free>
* Oracle docs, Always Free Resources: <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
* InfoQ, "Oracle Quietly Halves Free Tier Ampere A1 Compute Limits with No Public Announcement" (2026-07-03): <https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/>
* Reddit r/oraclecloud, "New Free Tier Limits (confirmed by oracle support)" (Jun 2026)

**Findings**

* **A1 allowance halved on 2026-06-15**: 3,000 → **1,500 OCPU-h/month** and
  18,000 → **9,000 GB-h/month**. Docs now describe it as "equivalent to
  2 OCPUs and 12 GB of memory usable as 1 VM or 2 VMs". No announcement was
  made; users discovered it via shut-down instances and docs diffs.
* Free-tier-only accounts exceeding the allowance: instances **stopped** by
  Oracle until resized to ≤ 2/12. PAYG: docs say "all tenancies"; support
  emails (2026-06-22) say PAYG keeps 4/24 free — unofficial, budget-alert
  worthy. Also: *"If an existing resource is terminated, it may not be
  possible to recreate resources above the updated Always Free limit."*
* **2× VM.Standard.E2.1.Micro (AMD, 1/8 OCPU / 1 GB) remain always free with
  no hour metering** — the reason a micro can host the 24/7 waker.
* Block storage: **200 GB total** (boot + block), min boot 47 GB, 5 backups.
* Egress: **10 TB/month**.
* Idle reclamation: 7-day window, **95th percentile CPU < 20%, network
  < 20%, memory < 20% (A1 only)** → instance stopped. PAYG tenancies exempt.
* A1 "Out of capacity" is the norm on free accounts, especially popular home
  regions; PAYG upgrade and AD/shape rotation are the practical fixes
  (community capacity-hunter scripts: hituri/oracle-cloud-compute-creation-script).

**Decisions driven**

* Default `always-on` profile = 2 OCPU/12 GB (1,488 ≤ 1,500 OCPU-h in a
  31-day month) — guaranteed free on any account type.
* `burst` profile spends the hour budget only while CI runs.
* Waker on the free AMD micro → self-healing against reclamation + status
  page + burst wake-ups.
* Boot volume 100 GB + waker 47 GB = 147/200 GB.

## GitHub Actions self-hosted runners

**Sources**

* GitHub docs, self-hosted runners: <https://docs.github.com/en/actions/reference/runners/self-hosted-runners>
* Azure OSS Developer Support, "Rate Limits and Auth on Github Actions Runners on Azure Container Apps" (2026-08-20): <https://azureossd.github.io/2026/08/20/Rate-Limits-and-Auth-on-Github-Actions-Runners-on-Azure-Container-Apps>
* KEDA GitHub Runner scaler (same poll-the-queue pattern on k8s): <https://keda.sh/docs/2.21/scalers/github-runner/>
* Nomad ephemeral runners walkthrough (FAUN.dev, 2026)
* actions/runner releases: <https://github.com/actions/runner/releases> —
  latest **v2.338.0** (2026-10-06), linux-arm64 asset + published sha256
  `628b4a…041b` (pinned in runner-image/Dockerfile).

**Findings**

* **Ephemeral runners** (`config.sh --ephemeral`) are the canonical
  one-job-per-registration pattern: the runner deregisters itself after the
  job; the container exits; the controller reaps it. Used by ARC, KEDA, and
  every mature autoscaler.
* **Registration tokens last ~1 hour**; a long-lived credential (PAT or
  GitHub App installation token) mints them. Installation tokens
  auto-refresh and have their own rate-limit budget; PATs share the user's
  5,000/h. Token caching is standard practice.
* Queue introspection: `GET /repos/{o}/{r}/actions/workflow-jobs?status=queued`
  (workflow jobs carry a `labels[]` array → filter for our label
  client-side). Org-wide queue listing is not exposed; org scope therefore
  runs a static pool unless you also list repos to monitor.
  Runner inventory (`/actions/runners`) exposes per-runner `status`
  (online/offline) and `busy` — the controller uses both for exact scaling
  and stale-runner cleanup.
* Auth options: classic PAT (`repo` / `admin:org`), fine-grained PAT
  (Administration RW, Metadata R, Actions R), GitHub App (Administration RW
  / Self-hosted runners RW + Actions R). Self-hosted runners **do not
  consume Actions minutes, including private repos** — only artifact/cache
  storage can bill.
* Security: fork-PR code on self-hosted runners is dangerous; mitigation =
  "Require approval for all outside collaborators" + ephemeral runners.

## ARM64 viability

* `actions/runner` publishes a first-class linux-arm64 tarball (pinned here
  at v2.338.0 with checksum verification).
* First-party setup actions (node/python/go/java) ship arm64 builds; some
  third-party actions are amd64-only → mitigate via multi-arch `container:`
  or `--platform linux/amd64` under QEMU (`qemu-user-static` +
  `binfmt-support`, installed by cloud-init on the VM).
* Native linux/arm64 docker builds at full speed; multi-arch via buildx
  (plugin installed in the runner image; builder container runs through the
  mounted host socket).

---

# Re-verification pass 2 (October 18, 2026)

Second full pass before wiring CI-driven provisioning; every number above
was re-checked plus the CI-specific stack.

## Oracle Always Free — re-confirmed numbers

**Sources (live fetches this date):**

* Oracle docs, Always Free Resources: <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
* Oracle marketing page: <https://www.oracle.com/cloud/free/>

Both agree on:

* **A1 allowance (post-June-15-2026)**: 1,500 OCPU-h + 9,000 GB-h per month
  ("All tenancies get the first 1,500 OCPU hours and 9,000 GB hours per
  month for free for VM instances using the VM.Standard.A1.Flex shape";
  "For Always Free tenancies, this is equivalent to 2 OCPUs and 12 GB").
* **AMD micros**: two VM.Standard.E2.1.Micro VMs, 1/8 OCPU + 1 GB each, no
  hour metering.
* **Block volume**: 200 GB total (boot + block combined), 5 volume backups,
  default boot volume 50 GB (min 47 GB). → our sizing: waker 47 + A1 150 =
  197 GB ("fullest allowed" with a small safety margin; theoretical max
  A1 boot 153 GB).
* **Object Storage**: free-tier-only accounts get **20 GB combined**
  (Standard + Infrequent Access + Archive) — paid/PAYG accounts get
  10+10+10 GB; **50,000 API requests/month**. Terraform state is a few KB —
  negligible.
* **Egress**: 10 TB/month outbound.
* **Idle reclamation** (unchanged): 7-day window, 95th-percentile CPU < 20%
  AND network < 20% AND memory < 20% (A1 only) → instance stopped.

## Terraform state in OCI (CI backbone)

**Sources:**

* Oracle docs, "Using Object Storage for State Files":
  <https://docs.oracle.com/en-us/iaas/Content/dev/terraform/object-storage-state.htm>
* Terraform docs, Backend Type: oci:
  <https://developer.hashicorp.com/terraform/language/backend/oci>

* Terraform ships a **native `oci` backend since v1.12.0** (current docs
  line: v1.16.x). Oracle's own docs now mark the older S3-compatibility
  backend method as **deprecated** ("Only use this method if you're not
  able to upgrade to Terraform version v1.12.0 or greater").
* The native backend stores state as an object in a bucket, supports
  **state locking** (lock object via `If-None-Match: *`), **workspaces**,
  and authenticates with the same **API-key credentials** as the provider
  (tenancy_ocid / user_ocid / fingerprint / private_key_path / auth=APIKey)
  — so CI needs no Customer Secret Keys (that was the S3-compat way).
  `bucket` + `namespace` cannot come from env vars; we pass them (and all
  auth) via `-backend-config` at init (scripts/tf_init.sh).
* Oracle's docs **warn to enable bucket versioning** on the state bucket —
  the Bootstrap workflow creates the bucket with versioning on.
* The deprecated S3-compat flags, for the record: endpoint
  `https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`,
  `use_path_style`/`force_path_style`, `skip_region_validation`,
  `skip_credentials_validation`, `skip_metadata_api_check` (from both
  Oracle's page and hashicorp/terraform#34053).

## GitHub Actions billing (the "completely free" question)

**Sources:**

* GitHub billing docs (live): <https://docs.github.com/billing/managing-billing-for-github-actions/about-billing-for-github-actions>
* GitHub changelog, "Update to GitHub Actions pricing" (2025-12-16, with
  update note): <https://github.blog/changelog/2025-12-16-coming-soon-simpler-pricing-and-a-better-experience-for-github-actions/>
* The Register coverage of the walk-back (2025-12-17):
  <https://www.theregister.com/software/2025/12/17/github-walks-back-plan-to-charge-for-self-hosted-runners/>
* GitHub pricing page: <https://github.com/pricing>

Facts:

* Current official statement: **"GitHub Actions usage is free for
  self-hosted runners and for public repositories that use standard
  GitHub-hosted runners."**
* Timeline: Dec 16, 2025 GitHub announced a **$0.002/min cloud-platform
  charge for self-hosted runner usage in private repos** (effective Mar 1,
  2026; usage would count toward plan minutes). After community backlash
  GitHub **postponed** the change ("We're postponing the announced billing
  change for self-hosted GitHub Actions to take time to re-evaluate our
  approach"). As of this verification (Oct 2026) it is **not in effect**,
  and public-repo usage was always excluded.
* GitHub Free plan: 2,000 Actions minutes/month for **private** repos;
  public repos unlimited. Our provisioning repo is public → workflows
  themselves cost nothing.

**Consequence for this repo**: running CI on public repos is free with no
strings. Private-repo usage is free today; if the postponed charge ever
lands, it draws from the 2,000 included minutes (≈ 2,000 runner-hours/month
— far beyond this fleet's use). Details in COST.md.

## Repo facts

* `iamleson98/oci-provision`: public, empty at start of work; PAT grants
  push permission (verified via the REST API before writing).
* `actions/runner` latest release re-checked: v2.338.0 (2026-10-06),
  linux-arm64 asset with published sha256 — pinned in runner-image/Dockerfile.

