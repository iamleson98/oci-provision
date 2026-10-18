# GitHub App setup (recommended for orgs; optional for repos)

A GitHub App avoids the classic PAT's 90-day expiry, scopes permissions
tightly, and gives the controller a higher API rate limit. Setup is ~10
minutes, once.

## 1. Create the App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**:

| Field | Value |
|---|---|
| Name | e.g. `oci-runner-manager` |
| Homepage URL | anything valid (your repo URL is fine) |
| Webhook → *Active* | **unchecked** (we poll; no webhook infrastructure needed) |

Repository permissions:

| Permission | Access | Why |
|---|---|---|
| **Administration** | Read & write | register/remove runners, mint registration tokens |
| **Actions** | Read-only | read the queued-jobs endpoint for autoscaling |
| **Metadata** | Read-only | mandatory baseline |

Organization permissions (only for org-scoped runners):

| Permission | Access | Why |
|---|---|---|
| **Self-hosted runners** | Read & write | register/remove org-level runners |

Click **Create GitHub App**. On the app page:

1. **Generate a private key** (`*.pem` downloads automatically).
2. Note the **App ID** (top of the page).

## 2. Install it

*Install App* → install to your user account (choose the repos) or your
organization. The installation gives you an **Installation ID** — visible in
the URL (`/settings/installations/<ID>`) or:

```bash
# from the A1 worker, once the controller .env has GH_APP_ID + key:
make app-install-id TARGET=youruser/your-repo     # or TARGET=yourorg
```

## 3. Configure the controller

`/opt/oci-runner/.env` on the A1 worker:

```ini
AUTH_MODE=app
GH_APP_ID=123456
GH_INSTALLATION_ID=7890123

# Option A (docker env_file-safe, single line with \n):
GH_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\nMIIEpA...\n-----END RSA PRIVATE KEY-----\n"

# Option B (file mount):
# cp your-app.pem /opt/oci-runner/deploy/secrets/app.pem   # chmod 600
# GH_APP_PRIVATE_KEY_FILE=/opt/oci-runner/secrets/app.pem
```

One-liner to convert a PEM into the `\n`-escaped single line:

```bash
awk 1 ORS='\\n' your-app.pem | sed 's/\\n$//' ; echo
```

Then:

```bash
sudo systemctl restart oci-runner.service
docker logs oci-runner-controller 2>&1 | grep -i "installation token"
```

Give the waker the same `AUTH_MODE`/`GH_APP_ID`/`GH_INSTALLATION_ID`/
`GH_APP_PRIVATE_KEY` values in its `waker.env` (it only needs read access to
the queue, but sharing credentials keeps the setup simple).

## Notes

* Installation tokens auto-refresh ~every 55 minutes inside the controller —
  nothing to rotate manually, unlike PATs.
* The private key never leaves your VMs (root-readable file, 0600).
* If the app is installed on *additional* repos later, each has a different
  installation id — use one installation per target or switch
  `SCOPE_TYPE=repo` + list the repos you installed it on.
