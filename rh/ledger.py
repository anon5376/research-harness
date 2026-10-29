"""One SQLite ledger: jobs with typed states, append-only events, receipts, owned stops, the operator inbox and
route backoff. It is the only status source; `rh status` reads nothing else."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

TERMINAL = frozenset({"done", "failed", "blocked", "capacity", "truncated", "aborted", "no_result", "cancelled"})
_PRE = {"ready", "waiting", "held", "cancelled"}
TRANSITIONS = {
    "draft": _PRE,
    "waiting": _PRE,
    "held": _PRE,
    "ready": _PRE | {"launching"},
    "launching": {"running", "ready", "held", "handoff", "failed", "blocked", "cancelled"},
    "handoff": {"launching", "cancelled"},  # a person or browser session must launch it (web route), then attach
    "running": {"running", "stalled", "collecting"} | TERMINAL,
    "stalled": {"running", "stalled", "collecting"} | TERMINAL,
    "collecting": {"collecting"} | TERMINAL,
}
STATES = frozenset(TRANSITIONS) | TERMINAL
LIVE = frozenset({"launching", "handoff", "running", "stalled", "collecting"})
RECEIPT_KEYS = ("route", "checks", "model_requested", "model_served", "ended")
BACKOFF = (60, 120, 240, 300)  # seconds after the 1st..4th consecutive route failure, then every 5 min

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, spec TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1, write_root TEXT, route TEXT, external_id TEXT, external_url TEXT,
  pinned_sha TEXT, detail TEXT NOT NULL DEFAULT '', heartbeat REAL, started REAL, created REAL NOT NULL,
  updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, job TEXT, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(job TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stops(
  id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('stop', 'pause')),
  owner TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL, lifted REAL, lifted_by TEXT);
CREATE TABLE IF NOT EXISTS inbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, job TEXT, action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', created REAL NOT NULL, resolved REAL, resolution TEXT);
CREATE TABLE IF NOT EXISTS routes(
  route TEXT PRIMARY KEY, healthy INTEGER NOT NULL, failures INTEGER NOT NULL, next_try REAL NOT NULL,
  last_status TEXT NOT NULL, retry_after REAL, updated REAL NOT NULL);
"""
_JOB_FIELDS = {"route", "external_id", "external_url", "pinned_sha", "heartbeat", "started"}


class LedgerError(Exception):
    pass


class Ledger:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(_SCHEMA)

    def close(self):
        self.db.close()

    def _tx(self):
        return _Tx(self.db)

    # jobs
    def add_job(self, spec, pinned_sha=None):
        now = self.clock()
        with self._tx():
            if self.db.execute("SELECT 1 FROM jobs WHERE id=?", (spec["id"],)).fetchone():
                raise LedgerError(f"job {spec['id']} already exists; a new attempt needs a new revision id")
            self.db.execute(
                "INSERT INTO jobs(id, kind, state, spec, revision, write_root, pinned_sha, created, updated)"
                " VALUES(?, ?, 'draft', ?, ?, ?, ?, ?, ?)",
                (spec["id"], spec["kind"], json.dumps(spec, sort_keys=True), spec.get("revision", 1),
                 spec.get("write_root"), pinned_sha, now, now))
            self._event("job-added", spec["id"], {"kind": spec["kind"]}, now)

    def job(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job(row) if row else None

    def jobs(self, states=None):
        rows = self.db.execute("SELECT * FROM jobs ORDER BY created, id").fetchall()
        return [_job(r) for r in rows if states is None or r["state"] in states]

    def set_state(self, job_id, state, detail="", **fields):
        if state in TERMINAL:
            raise LedgerError(f"{state} is terminal: use finish() with a receipt")
        with self._tx():
            self._move(job_id, state, detail, fields)

    def finish(self, job_id, state, receipt):
        if state not in TERMINAL:
            raise LedgerError(f"{state} is not a terminal state")
        missing = [k for k in RECEIPT_KEYS if k not in receipt]
        if missing:
            raise LedgerError(f"receipt for {job_id} lacks {', '.join(missing)}")
        with self._tx():
            self._move(job_id, state, receipt.get("detail", ""), {})
            self.db.execute("INSERT INTO receipts(job, state, data, ts) VALUES(?, ?, ?, ?)",
                            (job_id, state, json.dumps(receipt, sort_keys=True), self.clock()))

    def receipt(self, job_id):
        row = self.db.execute("SELECT data FROM receipts WHERE job=?", (job_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def heartbeat(self, job_id):
        now = self.clock()
        self.db.execute("UPDATE jobs SET heartbeat=?, updated=? WHERE id=?", (now, now, job_id))

    def _move(self, job_id, state, detail, fields):
        row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no job {job_id}")
        old = row["state"]
        if state not in TRANSITIONS.get(old, ()):
            raise LedgerError(f"{job_id}: {old} -> {state} is not allowed")
        bad = set(fields) - _JOB_FIELDS
        if bad:
            raise LedgerError(f"unknown job fields {sorted(bad)}")
        now = self.clock()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE jobs SET state=?, detail=?, updated=?{', ' + sets if sets else ''} WHERE id=?",
                        (state, detail, now, *fields.values(), job_id))
        if old != state:
            self._event("state", job_id, {"from": old, "to": state, "detail": detail}, now)

    # events
    def event(self, kind, job=None, **data):
        self._event(kind, job, data, self.clock())

    def _event(self, kind, job, data, ts):
        self.db.execute("INSERT INTO events(ts, job, kind, data) VALUES(?, ?, ?, ?)",
                        (ts, job, kind, json.dumps(data, sort_keys=True)))

    def events(self, job=None, since=0, limit=200):
        q, args = "SELECT * FROM events WHERE seq>?", [since]
        if job is not None:
            q, args = q + " AND job=?", args + [job]
        rows = self.db.execute(q + " ORDER BY seq LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    # owned stops and pauses: scope is "*", "job:<id>" or "route:<name>"; only the owner lifts its own entries
    def stop(self, owner, reason, scope="*", kind="stop"):
        if not owner.strip() or not reason.strip():
            raise LedgerError("a stop or pause needs an owner and a reason")
        now = self.clock()
        with self._tx():
            cur = self.db.execute("INSERT INTO stops(scope, kind, owner, reason, created) VALUES(?, ?, ?, ?, ?)",
                                  (scope, kind, owner, reason, now))
            self._event(kind, None, {"scope": scope, "owner": owner, "reason": reason}, now)
        return cur.lastrowid

    def lift(self, owner, scope="*", kind=None):
        now = self.clock()
        q = "UPDATE stops SET lifted=?, lifted_by=? WHERE lifted IS NULL AND owner=? AND scope=?"
        args = [now, owner, owner, scope]
        if kind:
            q, args = q + " AND kind=?", args + [kind]
        with self._tx():
            n = self.db.execute(q, args).rowcount
            if n:
                self._event("lift", None, {"scope": scope, "owner": owner, "count": n}, now)
        return n

    def active_stops(self):
        rows = self.db.execute("SELECT * FROM stops WHERE lifted IS NULL ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def blocking(self, kind, job=None, route=None):
        """The first active stop or pause of `kind` that applies globally, to the job, or to its route."""
        scopes = {"*"} | ({f"job:{job}"} if job else set()) | ({f"route:{route}"} if route else set())
        for s in self.active_stops():
            if s["kind"] == kind and s["scope"] in scopes:
                return s
        return None

    # operator inbox
    def inbox_add(self, kind, action, job=None, detail=""):
        """Return (id, created). An open item with the same kind, action and job is reused, not duplicated."""
        with self._tx():
            row = self.db.execute(
                "SELECT id FROM inbox WHERE resolved IS NULL AND kind=? AND action=? AND job IS ?",
                (kind, action, job)).fetchone()
            if row:
                return row["id"], False
            now = self.clock()
            cur = self.db.execute("INSERT INTO inbox(kind, job, action, detail, created) VALUES(?, ?, ?, ?, ?)",
                                  (kind, job, action, detail, now))
            self._event("inbox", job, {"kind": kind, "action": action}, now)
        return cur.lastrowid, True

    def inbox(self, open_only=True):
        q = "SELECT * FROM inbox" + (" WHERE resolved IS NULL" if open_only else "") + " ORDER BY id"
        return [dict(r) for r in self.db.execute(q).fetchall()]

    def inbox_resolve(self, item_id, resolution):
        with self._tx():
            n = self.db.execute("UPDATE inbox SET resolved=?, resolution=? WHERE id=? AND resolved IS NULL",
                                (self.clock(), resolution, item_id)).rowcount
            if not n:
                raise LedgerError(f"no open inbox item {item_id}")
            self._event("inbox-resolved", None, {"id": item_id, "resolution": resolution}, self.clock())

    # routes: backoff and limits; an event only when health flips, so one failure episode is one line
    def route(self, name):
        row = self.db.execute("SELECT * FROM routes WHERE route=?", (name,)).fetchone()
        return dict(row) if row else {"route": name, "healthy": 1, "failures": 0, "next_try": 0.0,
                                      "last_status": "", "retry_after": None, "updated": 0.0}

    def route_fail(self, name, status, retry_after=None):
        now = self.clock()
        with self._tx():
            r = self.route(name)
            failures = r["failures"] + 1
            wait = BACKOFF[min(failures, len(BACKOFF)) - 1]
            if retry_after:
                wait = max(wait, float(retry_after))
            self.db.execute(
                "INSERT OR REPLACE INTO routes(route, healthy, failures, next_try, last_status, retry_after, updated)"
                " VALUES(?, 0, ?, ?, ?, ?, ?)", (name, failures, now + wait, status, retry_after, now))
            if r["healthy"]:
                self._event("route-down", None, {"route": name, "status": status}, now)
        return self.route(name)

    def route_ok(self, name):
        now = self.clock()
        with self._tx():
            r = self.route(name)
            self.db.execute(
                "INSERT OR REPLACE INTO routes(route, healthy, failures, next_try, last_status, retry_after, updated)"
                " VALUES(?, 1, 0, 0, 'ok', NULL, ?)", (name, now))
            if not r["healthy"]:
                self._event("route-up", None, {"route": name, "after_failures": r["failures"]}, now)

    def routes(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM routes ORDER BY route").fetchall()]


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, *_):
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")


def _job(row):
    d = dict(row)
    d["spec"] = json.loads(d["spec"])
    return d
