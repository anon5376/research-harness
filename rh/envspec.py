"""Codex cloud environment spec: the steps an operator follows at chatgpt.com/codex to build the environment the
harness launches into. Every step needs the operator's accounts, so the harness only prints them."""
from __future__ import annotations

import re

SETUP_SCRIPT = "cloud/setup.sh"
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.\.?$)[A-Za-z0-9._-]{1,100}")
_URL_FORM = re.compile(r"(?:https?|ssh|git)://(?:[^@/]*@)?([^/:@]+)(?::\d*)?/(.+)", re.I)
_SCP_FORM = re.compile(r"(?:[^@/:]+@)?([^/:@]+):(?!/)(.+)")


def repo_slug(remote_url) -> str:
    """A github.com remote URL (https, ssh, git or scp-style) -> 'owner/repo'. Credentials never reach errors."""
    s = remote_url.strip() if isinstance(remote_url, str) else ""
    m = _URL_FORM.fullmatch(s) or _SCP_FORM.fullmatch(s)
    if not m:
        raise ValueError("remote is not a URL or scp-style address")
    host, path = m.group(1).lower(), m.group(2).rstrip("/")
    if host not in ("github.com", "www.github.com"):
        raise ValueError(f"remote host {host!r} is not github.com; Codex cloud needs a GitHub repository")
    path = path[:-4] if path.endswith(".git") else path
    if not _SLUG.fullmatch(path):
        raise ValueError("remote path is not owner/repo")
    return path


def env_spec(slug, name="research-harness") -> str:
    """Operator steps to create and publish the cloud environment for `slug`, ending with the config command."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"bad repository slug {slug!r}: want owner/repo")
    if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 for c in name):
        raise ValueError(f"bad environment name {name!r}")
    return f"""Codex cloud environment for {slug}

Operator steps. The harness never does them: each one needs your accounts.

1. GitHub access: give the Codex GitHub app access to {slug}
   (GitHub > Settings > Applications > Installed GitHub Apps > Configure > Repository access).
2. At chatgpt.com/codex > Settings > Environments > Create environment, enter:
   Repository:             {slug}
   Name:                   {name}
   Setup script:           sh {SETUP_SCRIPT}
   Agent internet access:  On
   Domain allowlist:       github.com
   Allowed HTTP methods:   All methods. git fetch over HTTPS POSTs to git-upload-pack, so GET-only
                           breaks the pinned-sha fetch every task starts with.
3. Save and publish the environment.
4. Allow "Full access" for harness tasks. The pinned fetch every task starts with needs the network, and
   "Approve for me" leaves the shell without it. Full access also gives the shell a GitHub token, so only
   prompts the harness wrote run there.
5. Copy the environment's ID from its settings page and record it. New environments may not be listed by
   `codex cloud`; if the CLI cannot resolve the ID, the supervisor hands each launch to a browser session:

rh config set cloud.env_id <ID>
"""
