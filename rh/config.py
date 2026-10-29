"""Local configuration in $RH_HOME/config.toml (never in the repo). Flat tables of strings, numbers, booleans
and string lists, so the small writer below is enough."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "cloud": {"env_id": "", "branch": "main", "codex": "codex", "route": "cli", "launches_per_tick": 1,
              "probe_timeout": 60},
    "lease": {"lock": "", "max_load": 10.0, "max_wait": 600},
    "notify": {"command": []},
    "supervisor": {"interval": 60},
    "repo": {"path": str(REPO)},
}


def home():
    return Path(os.environ.get("RH_HOME") or Path.home() / ".research-harness").expanduser().resolve()


def load(rh_home=None):
    h = Path(rh_home).expanduser().resolve() if rh_home else home()
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    p = h / "config.toml"
    if p.is_file():
        for table, values in tomllib.loads(p.read_text()).items():
            if isinstance(values, dict):
                cfg.setdefault(table, {}).update(values)
    if not cfg["lease"]["lock"]:
        cfg["lease"]["lock"] = str(h / "leases" / "heavy.lock")
    cfg["home"] = str(h)
    return cfg


def set_value(key, raw, rh_home=None):
    """Set table.key from a CLI string: true/false, numbers and comma lists are converted."""
    h = Path(rh_home) if rh_home else home()
    table, _, name = key.partition(".")
    if not table or not name:
        raise ValueError("key must look like table.name, for example cloud.env_id")
    p = h / "config.toml"
    data = tomllib.loads(p.read_text()) if p.is_file() else {}
    data.setdefault(table, {})[name] = _parse(raw, DEFAULTS.get(table, {}).get(name))
    h.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(dumps(data))
    tmp.replace(p)
    return data[table][name]


def _parse(raw, like):
    if isinstance(like, bool) or raw in ("true", "false"):
        return raw == "true"
    if isinstance(like, list):
        return [x.strip() for x in raw.split(",") if x.strip()] if not raw.startswith("[") else tomllib.loads(
            f"v = {raw}")["v"]
    if isinstance(like, (int, float)) and not isinstance(like, bool):
        try:
            return int(raw)
        except ValueError:
            return float(raw)
    return raw


def dumps(data):
    out = []
    for table, values in data.items():
        out.append(f"[{table}]")
        for k, v in values.items():
            out.append(f"{k} = {_toml(v)}")
        out.append("")
    return "\n".join(out)


def _toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'
