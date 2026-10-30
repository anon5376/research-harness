"""A static HTML presentation of the ledger view produced by ``rh status``."""
from html import escape


_STYLE = """
:root {
  color-scheme: light dark;
  --paper: #f7f8fa;
  --card: #ffffff;
  --ink: #182331;
  --muted: #526173;
  --line: #cbd2dc;
  --pause: #725000;
  --pause-bg: #fff0bc;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #111820;
    --card: #1b2530;
    --ink: #edf2f7;
    --muted: #b7c4d3;
    --line: #445467;
    --pause: #ffe3a3;
    --pause-bg: #49370d;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font: 1rem/1.6 system-ui, sans-serif;
  overflow-wrap: anywhere;
}
main { max-width: 64rem; margin: 0 auto; padding: 1.25rem; }
header { margin-bottom: 2rem; }
h1 { font-size: 1.8rem; line-height: 1.2; margin-bottom: .4rem; }
h2 { font-size: 1.1rem; margin: 0 0 .75rem; }
section { border-top: 1px solid var(--line); padding: 1.25rem 0; }
p { margin: .35rem 0; }
ul { list-style: none; margin: 0; padding: 0; }
li {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: .35rem;
  padding: .8rem;
  margin-bottom: .65rem;
  min-width: 0;
}
.job-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: .4rem .8rem; }
.job-id { font-family: ui-monospace, monospace; }
.detail { white-space: pre-wrap; }
.muted, .time { color: var(--muted); }
.paused { color: var(--pause); background: var(--pause-bg); padding: .1rem .4rem; font-weight: 700; }
"""


def _text(value):
    return escape(str(value), quote=True)


def _relative(timestamp, now):
    seconds = round(now - timestamp)
    return f"{seconds} s ago" if seconds >= 0 else f"in {-seconds} s"


def _jobs(rows):
    items = []
    for row in rows:
        paused = '<span class="paused">PAUSED</span>' if row.get("paused") else ""
        external = (f'<p class="muted">Worker: {_text(row["external_id"])}</p>'
                    if row.get("external_id") is not None else "")
        items.append(
            '<li><div class="job-head">'
            f'<strong class="job-id">{_text(row["id"])}</strong>'
            f'<span>{_text(row["state"])}</span>{paused}</div>'
            f'<p class="detail">{_text(row["detail"])}</p>{external}</li>'
        )
    return "".join(items)


def render(view: dict, now: float) -> str:
    """Return one self-contained HTML5 snapshot, escaping all supplied text."""
    sections = []

    def section(key, heading, content, *, items=True):
        body = f"<ul>{content}</ul>" if items and content else content
        if not body:
            body = '<p class="muted">none</p>'
        sections.append(f'<section aria-labelledby="{key}"><h2 id="{key}">{heading}</h2>{body}</section>')

    supervisor = view.get("supervisor")
    content = ""
    if supervisor:
        content = (f'<p>pid {_text(supervisor["pid"])}, last tick '
                   f'<span class="time">{_text(supervisor["age_s"])} s ago</span></p>')
    section("supervisor", "Supervisor", content, items=False)

    stops = []
    for stop in view.get("stops", []):
        created = (f'<p class="time">{_relative(stop["created"], now)}</p>'
                   if stop.get("created") is not None else "")
        stops.append(f'<li><p><strong>{_text(stop["kind"])}</strong> {_text(stop["scope"])} '
                     f'by {_text(stop["owner"])}</p><p class="detail">{_text(stop["reason"])}</p>'
                     f'{created}</li>')
    section("stops", "Stops", "".join(stops))

    routes = []
    for route in view.get("routes", []):
        updated = (f'<p class="time">Updated {_relative(route["updated"], now)}</p>'
                   if route.get("updated") is not None else "")
        routes.append(f'<li><p><strong>ROUTE DOWN {_text(route["route"])}</strong></p>'
                      f'<p class="detail">{_text(route["last_status"])}</p>'
                      f'<p>failures {_text(route["failures"])}, '
                      f'next try {_relative(route["next_try"], now)}</p>{updated}</li>')
    section("routes", "Routes", "".join(routes))

    section("live", "Live", _jobs(view.get("live", [])))
    section("not_started", "Not started", _jobs(view.get("not_started", [])))

    inbox = []
    for item in view.get("inbox", []):
        job = f' (job {_text(item["job"])})' if item.get("job") else ""
        detail = f'<p class="detail">{_text(item["detail"])}</p>' if item.get("detail") else ""
        created = (f'<p class="time">{_relative(item["created"], now)}</p>'
                   if item.get("created") is not None else "")
        inbox.append(f'<li><p>#{_text(item["id"])} [{_text(item["kind"])}] '
                     f'{_text(item["action"])}{job}</p>{detail}{created}</li>')
    section("inbox", "Inbox", "".join(inbox))

    counts = "".join(f'<li>{_text(state)} {_text(count)}</li>'
                     for state, count in sorted(view.get("last_24h", {}).items()))
    section("last_24h", "Last 24h", counts or '<p class="muted">nothing finished</p>', items=bool(counts))
    section("recent", "Recent", _jobs(view.get("recent", [])))

    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<title>Research harness status</title>\n'
            f'<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n'
            '<header><h1>Research harness</h1><p class="muted">Status snapshot</p></header>\n'
            + "\n".join(sections) + '\n</main>\n</body>\n</html>\n')
