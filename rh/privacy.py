#!/usr/bin/env python3
"""Privacy guard for a public repo: flag home paths, personal emails, tokens, Codex task ids
and private deny terms before they are committed.

    python3 -I rh/privacy.py                        # tracked files (working copy)
    python3 -I rh/privacy.py --staged               # index blobs of staged changes
    python3 -I rh/privacy.py --paths F...           # given files
    python3 -I rh/privacy.py --file F --path NAME   # one file reported as NAME (commit message)

Prints `path:line: rule: excerpt` per finding (line 0 = the file name or the whole file) and exits 1;
exits 0 with `PRIVACY CLEAN files=<n>`; 2 on usage errors. Excerpts are masked; deny terms and
tokens never print. Standalone: imports nothing from rh.
"""
from __future__ import annotations

import argparse
import codecs
import os
import re
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass

MAX_BYTES = 2 * 1024 * 1024
SNIFF_BYTES = 8192
REDACTED = "[redacted]"


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    excerpt: str

    def __str__(self):
        return f"{_display(self.path)}:{self.line}: {self.rule}: {self.excerpt}"


class PrivacyError(Exception):
    """git or a file could not be read; callers must fail closed."""


class UsageError(PrivacyError):
    """Bad arguments or unreadable input named on the command line."""


# Placeholders such as <name>, $USER or {user} cannot start a name, so they pass. A word char,
# `.` or `~` before the slash means a URL or relative path; `-I` style flags still count.
_HOME = re.compile(
    r"(?P<p>(?<![A-Za-z0-9])[A-Za-z]:(?:\\{1,2}|/)(?i:users)(?:\\{1,2}|/)"
    r"|(?:(?<![\w.~])|(?<=-[A-Za-z]))(?:/mnt/[A-Za-z])?/(?:Users|home)/)"
    r"(?P<n>\w[\w.-]*)"
)
_EMAIL = re.compile(r"(?<![\w.%+-])(?P<l>[\w.%+-]+)@(?P<d>(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])")
_EMAIL_OK = {"noreply@anthropic.com", "noreply@github.com"}
_DOMAIN_OK = ("noreply.github.com", "example.com", "example.org", "example.net")
_TLD_OK = {"example", "test", "invalid", "localhost"}
_GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}
_RETINA = re.compile(r"\d+x\.")
# (pattern, body must look random: a digit or mixed case)
_TOKENS = (
    (re.compile(r"(?<![\w-])(?P<p>sk-)[A-Za-z0-9_-]{20,}"), True),
    (re.compile(r"(?<![A-Za-z0-9])(?P<p>gh[pousr]_)[A-Za-z0-9]{30,}"), True),
    (re.compile(r"(?<![A-Za-z0-9])(?P<p>github_pat_)[A-Za-z0-9_]{22,}"), True),
    (re.compile(r"(?<![A-Za-z0-9])(?P<p>xox[abprs]-)[A-Za-z0-9-]{10,}"), True),
    (re.compile(r"(?<![A-Z0-9])(?P<p>A[KS]IA)[A-Z0-9]{16}(?![A-Z0-9])"), False),
    (re.compile(r"(?P<p>eyJ)[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)?"), False),
    (re.compile(r"(?<![A-Za-z0-9])(?P<p>AIza)[A-Za-z0-9_-]{35}"), True),
    (re.compile(r"(?<![A-Za-z0-9])(?P<p>[rs]k_live_)[A-Za-z0-9]{20,}"), True),
    (re.compile(r"(?<![A-Za-z0-9])(?P<p>glpat-)[A-Za-z0-9_-]{20,}"), True),
    (re.compile(r"(?<![A-Za-z0-9])(?P<p>hf_)[A-Za-z0-9]{30,}"), True),
    (re.compile(r"(?<![A-Za-z0-9])(?P<p>npm_)[A-Za-z0-9]{36}"), True),
)
_PEM = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY")
_TASK = re.compile(r"task_e_[0-9a-f]{16,}")
# the UUID task ids of the new environment system, as its task URLs show them (/codex/tasks/<id>, /local/<id>)
_TASK_URL = re.compile(r"(?i)chatgpt\.com/(?:codex/tasks|local)/(?P<v>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
# an id labelled as an account, org, user or workspace id (the codex CLI's error.log prints account_id=<uuid>)
_ACCOUNT = re.compile(r"(?i)\b(?:chatgpt-)?(?:account|org(?:anization)?|user|workspace)[_-]?id\b[\"']?(?:\s+[a-z]+)?\s*[:=]?\s*[\"']?"
                      r"(?P<v>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[A-Za-z0-9_-]{16,})")
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
)


def _norm(s):
    return unicodedata.normalize("NFKC", s).casefold()


def _plausible(body, random_body):
    """False for placeholders (xxxx...) and, where the body must look random, for words and slugs."""
    if len(set(body)) < 4:
        return False
    return not random_body or bool(re.search(r"\d", body) or (re.search(r"[a-z]", body) and re.search(r"[A-Z]", body)))


def _email_ok(local, domain):
    local, domain = local.lower(), domain.lower()
    if f"{local}@{domain}" in _EMAIL_OK or (local == "git" and domain in _GIT_HOSTS):
        return True
    if domain.rsplit(".", 1)[-1] in _TLD_OK or _RETINA.match(domain):
        return True
    return any(domain == ok or domain.endswith("." + ok) for ok in _DOMAIN_OK)


def _hits(line):
    """Yield (rule, masked excerpt) for every pattern hit in one line."""
    for m in _HOME.finditer(line):
        yield "home-path", m["p"] + m["n"][0] + "***"
    for m in _EMAIL.finditer(line):
        local, domain = m["l"], m["d"]
        if not _email_ok(local, domain):
            yield "email", f"{local[0]}***@{domain[0]}***.{domain.rsplit('.', 1)[1]}"
    for rx, random_body in _TOKENS:
        for m in rx.finditer(line):
            tok, prefix = m[0], m["p"]
            if not _plausible(tok[len(prefix):], random_body) or (prefix.endswith("IA") and tok.endswith("EXAMPLE")):
                continue
            yield "token", f"{prefix}***({len(tok)} chars)"
    if _PEM.search(line):
        yield "token", "PRIVATE KEY block"
    for m in _TASK.finditer(line):
        yield "codex-task-id", f"task_e_***({len(m[0])} chars)"
    for m in _TASK_URL.finditer(line):
        if len(set(m["v"])) > 4:
            yield "codex-task-id", f"chatgpt.com/.../***({len(m['v'])} chars)"
    for m in _ACCOUNT.finditer(line):
        v = m["v"]
        uuid = len(v) == 36 and v.count("-") == 4
        if (uuid and len(set(v)) > 4) or (not uuid and _plausible(v, True)):
            yield "account-id", f"***({len(v)} chars)"


def _terms(deny_terms):
    """Normalise deny terms given as strings (numbered 1..n) or (line, term) pairs."""
    out = []
    if isinstance(deny_terms, str):
        deny_terms = [deny_terms]
    for i, item in enumerate(deny_terms, 1):
        n, raw = item if isinstance(item, tuple) else (i, item)
        raw = raw.strip()
        if raw:
            out.append((n, raw, _norm(raw)))
    return out


def _redact(s, terms):
    if not terms:
        return s
    for _, raw, _ in terms:
        s = re.sub(re.escape(raw), REDACTED, s, flags=re.I)
    shown, placeholder = _norm(s), _norm(REDACTED)
    return REDACTED if any(t in shown for _, _, t in terms if t not in placeholder) else s


def _display(s):
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in s):
        return repr(s)[1:-1]
    return s


def _scan(text, path, terms):
    shown = _redact(path, terms)
    found = []
    for i, line in enumerate(text.split("\n"), 1):
        for rule, excerpt in _hits(line):
            found.append(Finding(shown, i, rule, _redact(excerpt, terms)))
        if terms:
            folded = _norm(line)
            found += [Finding(shown, i, f"deny-term:{n}", REDACTED) for n, _, t in terms if t in folded]
    return found


def scan_text(text, path, deny_terms=()) -> list[Finding]:
    """Findings for one text; `path` is only the label."""
    return _scan(text, path, _terms(deny_terms))


def _text(data):
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data.decode(enc, "replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _decode(data):
    """Text of a blob, or None when it is binary (NUL in the first 8 KB and no UTF-16/32 BOM)."""
    if b"\0" in data[:SNIFF_BYTES] and not any(data.startswith(bom) for bom, _ in _BOMS):
        return None
    return _text(data)


def load_deny(path) -> list[tuple[int, str]]:
    """(line number, term) per deny-file line; `#` starts a comment, blank lines are skipped."""
    with open(path, "rb") as f:
        lines = _text(f.read()).split("\n")
    terms = []
    for n, line in enumerate(lines, 1):
        term = re.split(r"\s#", line, maxsplit=1)[0].strip()
        if term and not term.startswith("#"):
            terms.append((n, term))
    return terms


def default_deny_path():
    home = os.environ.get("RH_HOME") or "~/.research-harness"
    return os.path.join(os.path.expanduser(home), "privacy-deny.txt")


class _Run:
    def __init__(self, terms):
        self.terms, self.findings, self.files, self.binary = terms, [], 0, 0

    def name(self, path):
        self.findings += [Finding(f.path, 0, f.rule, f.excerpt) for f in _scan(path, path, self.terms)]

    def blob(self, path, data, size=None):
        """Scan one file's bytes; data may be None when size is over the limit."""
        self.files += 1
        size = len(data) if size is None else size
        if size > MAX_BYTES:
            self.findings.append(Finding(_redact(path, self.terms), 0, "too-large", f"{size} bytes > {MAX_BYTES}"))
            return
        text = _decode(data)
        if text is None:
            self.binary += 1
        else:
            self.findings += _scan(text, path, self.terms)


def _git(top, *args, stdin=None):
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", top, *args], input=stdin, capture_output=True)
    except OSError as e:
        raise PrivacyError(f"cannot run git: {e}") from e
    if r.returncode:
        raise PrivacyError(f"git {args[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def _index(top):
    """{path bytes: (mode, oid)} from the index, stage 0 preferred."""
    entries = {}
    for rec in _git(top, "ls-files", "-z", "--stage").split(b"\0"):
        if rec:
            meta, _, name = rec.partition(b"\t")
            mode, oid, stage = meta.split(b" ")
            if stage == b"0" or name not in entries:
                entries[name] = (mode, oid)
    return entries


def _blobs(top, oids):
    """{oid: (size, data or None when over the limit)} via one batch-check and one batch read."""
    uniq = list(dict.fromkeys(oids))
    if not uniq:
        return {}
    sizes = {}
    for line in _git(top, "cat-file", "--batch-check", stdin=b"\n".join(uniq) + b"\n").splitlines():
        parts = line.split(b" ")
        if len(parts) != 3 or parts[1] != b"blob":
            raise PrivacyError(f"cannot read index object: {line.decode('utf-8', 'replace')}")
        sizes[parts[0]] = int(parts[2])
    if set(sizes) != set(uniq):
        raise PrivacyError("git cat-file returned an incomplete answer")
    small = [o for o in uniq if sizes[o] <= MAX_BYTES]
    data, pos = {}, 0
    out = _git(top, "cat-file", "--batch", stdin=b"\n".join(small) + b"\n") if small else b""
    for oid in small:
        nl = out.find(b"\n", pos)
        if nl < 0 or not out.startswith(oid + b" blob ", pos):
            raise PrivacyError("git cat-file --batch returned an unexpected answer")
        start = nl + 1
        data[oid] = out[start:start + sizes[oid]]
        pos = start + sizes[oid] + 1
    return {o: (sizes[o], data.get(o)) for o in uniq}


def _worktree_file(path):
    """(size, data) of a working-copy entry; symlinks yield their target; None when absent or not a file."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(path)
            return len(target), target
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > MAX_BYTES:
            return st.st_size, None
        with open(path, "rb") as f:
            return st.st_size, f.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise PrivacyError(f"cannot read {os.fsdecode(path)}: {e.strerror}") from e


def _scan_repo(repo, terms, staged):
    top = os.fsdecode(_git(os.fspath(repo), "rev-parse", "--show-toplevel").rstrip(b"\n"))
    index = _index(top)
    run = _Run(terms)
    if staged:
        out = _git(top, "diff", "--cached", "--name-only", "--no-renames", "--diff-filter=ACMRT", "-z")
        names = [n for n in out.split(b"\0") if n]
        missing = [n for n in names if n not in index]
        if missing:
            raise PrivacyError(f"staged path not in index: {os.fsdecode(missing[0])}")
        for name in names:
            run.name(os.fsdecode(name))
        picked = [(n, index[n][1]) for n in names if index[n][0] != b"160000"]
        blobs = _blobs(top, [oid for _, oid in picked])
        for name, oid in picked:
            size, data = blobs[oid]
            run.blob(os.fsdecode(name), data, size)
        return run
    root = os.fsencode(top)
    for name, (mode, _) in index.items():
        run.name(os.fsdecode(name))
        if mode == b"160000":
            continue
        got = _worktree_file(os.path.join(root, name))
        if got is not None:
            run.blob(os.fsdecode(name), got[1], got[0])
    return run


def scan_repo(repo, deny_terms=(), staged=False) -> list[Finding]:
    """Findings for tracked files (working copy), or for staged index blobs when `staged`."""
    return _scan_repo(repo, _terms(deny_terms), staged).findings


def _scan_paths(paths, terms):
    run = _Run(terms)
    for p in paths:
        try:
            st = os.stat(p)
            if not stat.S_ISREG(st.st_mode):
                raise UsageError(f"not a regular file: {p}")
            data = None
            if st.st_size <= MAX_BYTES:
                with open(p, "rb") as f:
                    data = f.read()
        except OSError as e:
            raise UsageError(f"cannot read {p}: {e.strerror}") from e
        run.blob(p, data, st.st_size)
    return run


def _scan_message(file, name, terms):
    """All of a commit message file: git keeps text below a scissors line unless -v, which a hook cannot see."""
    try:
        if file in ("/dev/stdin", "/proc/self/fd/0"):
            data = sys.stdin.buffer.read()
        else:
            with open(file, "rb") as f:
                data = f.read()
    except OSError as e:
        raise UsageError(f"cannot read {file}: {e.strerror}") from e
    run = _Run(terms)
    run.blob(name, data)
    return run


def _tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if home not in ("", "/") and path.startswith(home + os.sep) else path


def _parse(argv):
    p = argparse.ArgumentParser(prog="privacy.py", description="Scan for private strings before they reach a public repo.")
    p.add_argument("--repo", default=".", help="git work tree (default: current directory)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan index blobs of staged changes")
    mode.add_argument("--paths", nargs="+", metavar="F", help="scan these files")
    mode.add_argument("--file", metavar="F", help="scan one file (a commit message), reported as --path")
    p.add_argument("--path", metavar="NAME", help="name to report for --file")
    p.add_argument("--deny", metavar="FILE", help="deny terms, one per line (default: $RH_HOME/privacy-deny.txt)")
    a = p.parse_args(argv)
    if (a.file is None) != (a.path is None):
        p.error("--file and --path go together")
    return a


def main(argv=None) -> int:
    try:
        a = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="backslashreplace")
    deny = default_deny_path() if a.deny is None else a.deny
    try:
        if a.deny is None and not os.path.exists(deny):
            print(f"privacy: note: no deny file at {_tilde(deny)}; deny-term rule off", file=sys.stderr)
            terms = []
        else:
            try:
                terms = _terms(load_deny(deny))
            except OSError as e:
                raise UsageError(f"cannot read deny file {_tilde(deny)}: {e.strerror}") from e
        if a.paths:
            run = _scan_paths(a.paths, terms)
        elif a.file is not None:
            run = _scan_message(a.file, a.path, terms)
        else:
            run = _scan_repo(a.repo, terms, a.staged)
    except PrivacyError as e:
        print(f"privacy: {e}", file=sys.stderr)
        return 2 if isinstance(e, UsageError) else 1
    for f in run.findings:
        print(f)
    if run.binary:
        print(f"privacy: note: skipped {run.binary} binary file(s)", file=sys.stderr)
    if run.findings:
        print(f"PRIVACY FAIL findings={len(run.findings)} files={run.files}")
        return 1
    print(f"PRIVACY CLEAN files={run.files}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
