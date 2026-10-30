"""The few git operations the harness needs, each a checked subprocess call."""
from __future__ import annotations

import subprocess


class GitError(RuntimeError):
    pass


def git(repo, *args, input=None, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def remote_url(repo, name="origin"):
    r = git(repo, "remote", "get-url", name, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def on_remote(repo, sha, ref="origin/main"):
    """(True, "") when sha is contained in the local copy of ref, else (False, why). No network."""
    if git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).returncode != 0:
        return False, f"no {ref} yet: commit {sha[:12]} needs a push (lead)"
    if git(repo, "merge-base", "--is-ancestor", sha, ref, check=False).returncode != 0:
        return False, f"commit {sha[:12]} is not on {ref} yet: push pending (lead)"
    return True, ""


def worktree_at(repo, path, sha):
    """A fresh detached worktree at sha; an old one at the same path is removed first."""
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune")
    git(repo, "worktree", "add", "--detach", str(path), sha)


def apply(worktree, diff_text):
    git(worktree, "apply", "--index", "--whitespace=nowarn", "-", input=diff_text)


def staged(worktree):
    """[(new_mode, path)] for every staged change, renames split into delete + add, as git itself sees them."""
    out = git(worktree, "diff", "--cached", "--raw", "-z", "--no-renames").stdout.split("\0")
    rows = []
    for meta, path in zip(out[0::2], out[1::2]):
        if meta.startswith(":"):
            rows.append((meta[1:].split()[1], path))
    return rows


def commit_branch(worktree, branch, message):
    git(worktree, "checkout", "-b", branch)
    git(worktree, "commit", "-m", message)
    return head(worktree)
