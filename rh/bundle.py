"""Second harvest route: a bundle a cloud task prints in a follow-up turn, for when its Changes view has expired.

The task prints a deterministic tar.gz of its staged files (each at most 64 KiB) as base64 parts, with a sha256 for
each part and for the whole; bigger files are listed by path, size and sha256 only. `rh cloud harvest --bundle <dir>`
reads the saved replies, checks every sha, and rebuilds the task's staged diff in a fresh worktree at the pinned sha,
so collection runs the same custody, RESULT.json and gate checks as for a saved patch."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

from rh import gitx

MAX_FILE = 64 * 1024
PART_CHARS = 90_000
_MARK = re.compile(r"^\s*RH-(BUNDLE|PART|END)\b\s*(.*)$")
_B64 = re.compile(r"^[A-Za-z0-9+/=]+$")

# Runs in the task's container with only python3 and git. It reads the index, not the working tree, so it exports
# exactly what is staged. tar entries are sorted, with mtime 0, owner 0 and no names; gzip carries mtime 0.
_EXPORT = r'''import base64, gzip, hashlib, io, json, subprocess, tarfile
JOB, MAX_FILE, PART_CHARS = "@JOB@", @MAX_FILE@, @PART_CHARS@
git = lambda *a: subprocess.run(["git", *a], capture_output=True, check=True).stdout
raw = git("diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev").decode().split("\0")
files, large, special, deleted = {}, [], [], []
for meta, path in zip(raw[0::2], raw[1::2]):
    _, mode, _, blob, status = meta[1:].split()
    if status.startswith("D"):
        deleted.append(path)
    elif mode not in ("100644", "100755"):
        special.append({"path": path, "mode": mode})
    else:
        data = git("cat-file", "blob", blob)
        if len(data) > MAX_FILE:
            large.append({"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        else:
            files[path] = (mode, data)
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
    for path in sorted(files):
        mode, data = files[path]
        info = tarfile.TarInfo(path)
        info.size, info.mode, info.mtime = len(data), 0o755 if mode == "100755" else 0o644, 0
        tar.addfile(info, io.BytesIO(data))
tgz = gzip.compress(buf.getvalue(), mtime=0)
text = base64.b64encode(tgz).decode()
parts = [text[i:i + PART_CHARS] for i in range(0, len(text), PART_CHARS)]
print("RH-BUNDLE " + json.dumps({"v": 1, "job": JOB, "head": git("rev-parse", "HEAD").decode().strip(),
      "files": len(files), "bytes": len(tgz), "sha256": hashlib.sha256(tgz).hexdigest(), "parts": len(parts),
      "large": large, "special": special, "deleted": sorted(deleted)}))
for n, part in enumerate(parts, 1):
    print("RH-PART " + json.dumps({"n": n, "of": len(parts), "sha256": hashlib.sha256(part.encode()).hexdigest()}))
    for i in range(0, len(part), 76):
        print(part[i:i + 76])
print("RH-END")
'''


class BundleError(ValueError):
    pass


def export_script(job_id):
    return (_EXPORT.replace("@JOB@", job_id).replace("@MAX_FILE@", str(MAX_FILE))
            .replace("@PART_CHARS@", str(PART_CHARS)))


def export_prompt(job_id, sha, paths):
    """The follow-up turn that makes a finished task print its staged work as a bundle."""
    return (f"Export follow-up for {job_id}. Change no file and run no tests. From the root of the checkout:\n"
            f"1. If `git rev-parse HEAD` is not {sha}, run `git reset --soft {sha}`, which keeps your work staged.\n"
            f"2. Run `git add -A -- {' '.join(paths)}`.\n"
            "3. Run the command below exactly. Reply with its complete output in one code block, unchanged and "
            "nothing else; if the reply would be cut off, continue with the next RH-PART in a new reply.\n\n"
            f"python3 - <<'PY'\n{export_script(job_id)}PY\n")


def read(source):
    """Saved replies (one file, or every file in a directory, in name order) -> one text."""
    p = Path(source)
    if p.is_dir():
        files = sorted(f for f in p.iterdir() if f.is_file() and not f.name.startswith("."))
        if not files:
            raise BundleError(f"{p} holds no saved reply")
        return "\n".join(f.read_text(errors="replace") for f in files)
    return p.read_text(errors="replace")


def parse(text, job_id):
    """-> (header, tgz bytes) after checking every part's sha256 and the whole's. Raises BundleError."""
    header, parts, cur = None, {}, None
    for line in text.splitlines():
        m = _MARK.match(line)
        if m and m[1] == "BUNDLE":
            h = _json(m[2], "RH-BUNDLE")
            if header is not None and h != header:
                raise BundleError("two different RH-BUNDLE headers")
            header, cur = h, None
        elif m and m[1] == "PART":
            p = _json(m[2], "RH-PART")
            if not isinstance(p.get("n"), int) or not isinstance(p.get("sha256"), str):
                raise BundleError(f"bad RH-PART line: {line.strip()[:80]}")
            cur = (p, [])
            parts.setdefault(p["n"], []).append(cur)
        elif m:
            cur = None
        elif cur is not None and line.strip() and not line.strip().startswith("```"):
            if not _B64.match(line.strip()):
                raise BundleError(f"part {cur[0]['n']}: a line is not base64: {line.strip()[:40]!r}")
            cur[1].append(line.strip())
    if header is None:
        raise BundleError("no RH-BUNDLE header")
    if header.get("v") != 1 or header.get("job") != job_id:
        raise BundleError(f"bundle is for {header.get('job')!r} (v{header.get('v')}), not {job_id}")
    k = header.get("parts")
    if not isinstance(k, int) or k < 0 or set(parts) - set(range(1, k + 1)):
        raise BundleError(f"part numbers {sorted(parts)} do not fit parts={k}")
    chunks = []
    for n in range(1, k + 1):
        copies = {"".join(lines): p for p, lines in parts.get(n, [])}
        if not copies:
            raise BundleError(f"part {n}/{k} is missing")
        if len(copies) > 1:
            raise BundleError(f"part {n}/{k} appears twice with different text")
        (chunk, p), = copies.items()
        if p.get("of") != k:
            raise BundleError(f"part {n} says of={p.get('of')}, the header says parts={k}")
        if hashlib.sha256(chunk.encode()).hexdigest() != p["sha256"]:
            raise BundleError(f"part {n}/{k}: sha256 mismatch; save that part again")
        chunks.append(chunk)
    try:
        tgz = base64.b64decode("".join(chunks), validate=True)
    except binascii.Error as e:
        raise BundleError(f"bundle is not valid base64: {e}") from None
    if len(tgz) != header.get("bytes") or hashlib.sha256(tgz).hexdigest() != header.get("sha256"):
        raise BundleError("whole-bundle sha256 or size mismatch")
    if header.get("special"):
        raise BundleError(f"the task staged a symlink or submodule, which a bundle cannot carry: {header['special']}")
    return header, tgz


def members(tgz):
    """-> {path: (git mode, bytes)}; anything but a small regular file at a plain relative path is refused."""
    out = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
            for m in tar:
                path = safe_path(m.name)
                if not m.isreg():
                    raise BundleError(f"{path}: not a regular file")
                if m.size > MAX_FILE:
                    raise BundleError(f"{path}: {m.size} bytes, over the {MAX_FILE} byte bundle limit")
                if path in out:
                    raise BundleError(f"{path}: appears twice")
                out[path] = ("100755" if m.mode & 0o111 else "100644", tar.extractfile(m).read())
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BundleError(f"bundle is not a readable tar.gz: {e}") from None
    return out


def safe_path(name):
    parts = name.split("/") if isinstance(name, str) else [""]
    if (not name or name.startswith("/") or "\\" in name or "\0" in name
            or any(p in ("", ".", "..") or p.casefold() == ".git" for p in parts)):
        raise BundleError(f"unsafe path in bundle: {name!r}")
    return name


def to_patch(header, files, repo, sha, work):
    """Write the files and deletions into a fresh worktree at sha -> the staged diff, binary-safe."""
    if header.get("head") != sha:
        raise BundleError(f"bundle was exported at {str(header.get('head'))[:12]}, not the pinned {sha[:12]}: "
                          "stale input")
    deleted = header.get("deleted") or []
    work = Path(work)
    gitx.worktree_at(repo, work, sha)
    try:
        for path, (mode, data) in sorted(files.items()):
            _no_links(work, path)
            dest = work / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(0o755 if mode == "100755" else 0o644)
        if files:
            gitx.git(work, "add", "-f", "--", *sorted(files))
        for path in deleted:
            _no_links(work, safe_path(path))
            gitx.git(work, "rm", "-q", "--", path)
        return gitx.git(work, "diff", "--cached", "--binary", "--full-index", "--no-renames").stdout
    except gitx.GitError as e:
        raise BundleError(f"bundle does not fit the pinned sha: {e}") from None
    except UnicodeDecodeError:
        raise BundleError("the rebuilt diff holds a text file that is not UTF-8") from None
    finally:
        gitx.git(repo, "worktree", "remove", "--force", str(work), check=False)


def _no_links(root, path):
    p = root
    for part in path.split("/"):
        p = p / part
        if p.is_symlink():
            raise BundleError(f"{path}: goes through a symlink in the checkout")


def _json(text, what):
    try:
        v = json.loads(text)
    except ValueError:
        v = None
    if not isinstance(v, dict):
        raise BundleError(f"bad {what} line: {text.strip()[:80]}")
    return v
