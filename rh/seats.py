"""Standing role cards and their most recently attached cloud tasks."""
from __future__ import annotations

import tomllib
from pathlib import Path

from rh import protocol
from rh.ledger import TERMINAL

_KEYS = frozenset({"role", "route", "model", "effort", "owns", "done"})


def load(path) -> dict[str, dict]:
    """Read strict seat cards and reject ownership shared by different seats."""
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if set(raw) != {"seat"}:
        raise ValueError(f"seats.toml: missing keys {sorted({'seat'} - set(raw))}; "
                         f"unknown keys {sorted(set(raw) - {'seat'})}")
    cards = raw["seat"]
    if not isinstance(cards, dict):
        raise ValueError("seats.toml: seat must be a table")
    owned = []
    for name, card in cards.items():
        if not isinstance(card, dict):
            raise ValueError(f"seat {name}: card must be a table")
        missing, unknown = _KEYS - card.keys(), card.keys() - _KEYS
        if missing or unknown:
            raise ValueError(f"seat {name}: missing keys {sorted(missing)}; unknown keys {sorted(unknown)}")
        for key in _KEYS - {"owns"}:
            if not isinstance(card[key], str) or not card[key].strip():
                raise ValueError(f"seat {name}: {key} must be a non-empty string")
        if card["route"] not in ("codex-cloud", "claude"):
            raise ValueError(f"seat {name}: route must be codex-cloud or claude")
        if not isinstance(card["owns"], list) or not all(protocol._rel_ok(p) for p in card["owns"]):
            raise ValueError(f"seat {name}: owns must be a list of plain repo-relative paths")
        for path in card["owns"]:
            normal = path.rstrip("/") + "/"
            for other, theirs in owned:
                if other != name and (normal.startswith(theirs) or theirs.startswith(normal)):
                    raise ValueError(f"seats {other} and {name} overlap owns: {theirs.rstrip('/')} and {path}")
            owned.append((name, normal))
    return cards


def holds(spec: dict, seats: dict) -> list[str]:
    """Return every route, model, effort or ownership mismatch with a job's seat."""
    if "seat" not in spec:
        return []
    name = spec["seat"]
    if not isinstance(name, str) or name not in seats:
        return [f"unknown seat {name!r}"]
    card, out = seats[name], []
    if card["route"] != spec.get("kind"):
        out.append(f"seat {name}: route {card['route']} differs from job kind {spec.get('kind')}")
    model = spec.get("model") or {}
    for key, actual in (("model", model.get("requested")), ("effort", model.get("effort"))):
        if actual != card[key]:
            out.append(f"seat {name}: {key} {actual!r} must be {card[key]!r}")
    for path in spec.get("write_paths", []):
        if not protocol._rel_ok(path) or not any(
                path.startswith(own) if own.endswith("/") else path == own for own in card["owns"]):
            out.append(f"seat {name} does not own write path {path!r}")
    return out


def task_of(led, seat: str) -> tuple[str | None, str | None]:
    """The last attached cloud task and its job while nonterminal; heartbeats do not change attachment order."""
    jobs = [j for j in led.jobs() if j["spec"].get("seat") == seat and
            j["spec"]["kind"] == "codex-cloud" and j.get("external_id")]
    if not jobs:
        return None, None
    latest = max(jobs, key=lambda j: (j["started"] if j.get("started") is not None else j["created"],
                                      j["created"], j["id"]))
    return latest["external_id"], latest["id"] if latest["state"] not in TERMINAL else None
