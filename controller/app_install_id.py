"""One-off helper: find the GitHub App installation id for a repo or org.

Run inside the controller container:

  docker exec oci-runner-controller python app_install_id.py owner/repo

Requires GH_APP_ID and GH_APP_PRIVATE_KEY in the environment (App JWT auth,
no installation token needed - that's the whole point).
"""

from __future__ import annotations

import os
import sys
import time

import requests
import jwt

API = "https://api.github.com"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: app_install_id.py <owner/repo | org>", file=sys.stderr)
        return 2
    target = sys.argv[1].strip().strip("/")

    app_id = os.environ.get("GH_APP_ID", "")
    key = os.environ.get("GH_APP_PRIVATE_KEY", "").replace("\\n", "\n")
    key_file = os.environ.get("GH_APP_PRIVATE_KEY_FILE", "")
    if not app_id or (not key and not key_file):
        print("GH_APP_ID and GH_APP_PRIVATE_KEY(_FILE) must be set", file=sys.stderr)
        return 2
    if not key:
        with open(key_file, encoding="utf-8") as fh:
            key = fh.read()

    now = int(time.time())
    app_jwt = jwt.encode({"iat": now - 60, "exp": now + 300, "iss": app_id}, key, algorithm="RS256")
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if "/" in target:
        url = f"{API}/repos/{target}/installation"
    else:
        url = f"{API}/orgs/{target}/installation"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code == 404:
        print(f"the app is not installed on {target!r} yet", file=sys.stderr)
        return 1
    resp.raise_for_status()
    inst = resp.json()
    print(
        f"installation_id = {inst['id']}\n"
        f"account         = {inst.get('account', {}).get('login')}\n"
        f"app_id          = {inst.get('app_id')}\n"
        f"put this in .env: GH_INSTALLATION_ID={inst['id']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
