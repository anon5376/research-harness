"""The app. With a token it is the live page `rh serve` sends, in Linear's frame: a labelled sidebar on the dark window
ground (the pages, This Mac and the check loop) beside one inset panel. Today puts the composer in a band of ASCII water,
with a jellyfish drawn in characters for each seat, above the jobs grouped by what you do next, and who is working on
the right. A job's page has the work on the left and its properties, receipt, pauses and stops, and timeline on the
right. The Inbox is a list and the item it shows; ⌘K opens a palette. One same-origin script, SCRIPT (rh/web/app.js,
served at /app.js), keeps it live, moves between pages without reloading and turns submits into in-place updates;
without it every link and form still works. Without a token it is the static snapshot `rh status --html` writes: the
jobs, what needs a person (with the command that settles it) and the day, with no script and no controls."""
import json
import re
import shlex
import sys
import time
import zlib
from html import escape
from pathlib import Path
from urllib.parse import quote

# The look and the client ship beside this module; rh serve sends the fonts from rh/web/fonts (see FONTS).
_WEB = Path(__file__).with_name("web")
STYLE = (_WEB / "rh.css").read_text(encoding="utf-8")
SCRIPT = (_WEB / "app.js").read_text(encoding="utf-8")
FONTS = ("GeistSerif-Square.woff2", "GeistMono-VF.woff2")  # the only files GET /fonts/<name> serves
# The live page's faces. The snapshot leaves them out: it is one file and loads nothing, so it falls back to the
# system's serif and monospace.
_FACES = ('@font-face{font-family:"Geist Serif";src:url(/fonts/GeistSerif-Square.woff2) format("woff2");'
          'font-weight:400;font-display:block}'
          '@font-face{font-family:"Geist Mono";src:url(/fonts/GeistMono-VF.woff2) format("woff2");'
          'font-weight:100 900;font-display:block}')

# Plain words for each job state and the tone of its dot. An unknown state is shown as the harness names it.
_STATES = {
    "draft": ("New", "neutral"), "ready": ("Ready", "neutral"), "waiting": ("Waiting", "neutral"),
    "held": ("On hold", "warn"), "launching": ("Starting", "live"), "handoff": ("Launch by hand", "warn"),
    "running": ("Running", "live"), "stalled": ("Past its time limit", "warn"),
    "collecting": ("Checking the result", "live"), "done": ("Done", "good"), "failed": ("Failed", "bad"),
    "blocked": ("Blocked", "warn"), "capacity": ("No capacity", "warn"), "truncated": ("Cut short", "bad"),
    "aborted": ("Ended early", "bad"), "no_result": ("No result", "bad"), "cancelled": ("Cancelled", "neutral"),
    # not a ledger state: a ready cloud job while the codex CLI cannot see its environment, named once for every place
    "env": ("Waiting for environment", "warn"),
}
_PRE = ("draft", "ready", "waiting", "held", "handoff")  # states rh job cancel accepts
_LIVE = ("launching", "handoff", "running", "stalled", "collecting")
_FAILED = ("failed", "blocked", "capacity", "truncated", "aborted", "no_result")  # a finished job that needs a person
_FAILURE_WINDOW = 86400  # a failure stays in Needs you this long, unless it was run again
# What happens next to a job the harness is moving, so a quiet page still says what it is doing.
_NEXT = {
    "draft": "Checking it now.",
    "ready": "It starts as soon as a slot {place} is free.",
    "waiting": "It starts by itself once what it waits for is ready.",
    "held": "It is rechecked on every pass. Fix what it needs, or cancel it.",
    "launching": "Starting it {place}.",
    "handoff": "It waits for you to launch it in Codex.",
    "stalled": "It has run past its time limit; it is still being checked.",
    "collecting": "Fetching and checking its result.",
}
# The machine rh serve runs on has one name in the app, as its owner calls it.
HERE = {"darwin": "This Mac", "win32": "This PC"}.get(sys.platform, "This computer")
# Platforms (routes and job kinds in the CLI) as people name them; anything else is shown as the harness names it.
_PLATFORMS = {"codex-cloud": "Codex cloud", "codex-cloud-cli": "Codex cloud", "codex-cloud-web": "Codex cloud",
              "local-shell": HERE}
_PLACES = {"codex-cloud": "in Codex cloud", "local-shell": f"on {HERE}"}
_WHERE = {"local-shell": "mac", "codex-cloud": "cloud"}  # what the list's tabs filter on
_NEEDS = ("net", "python", "git", "loopback", "browser", "gpu")  # capabilities the composer offers
_STALE = 300  # seconds without a check before the status line says the checks may have stopped
_TAIL = 12  # lines of a failed job's output shown open, as where it stopped
_CODEX = "https://chatgpt.com/codex"  # the one page outside the app a live page links to, for a hand launch
_LABEL = re.compile(r"^[^:]{1,24}:\s+")  # a seat role's leading label of up to 24 characters, such as "Testing: "
_SETUP = "create and publish the cloud environment"  # how the supervisor's Codex cloud setup step begins
_NATIVE = "make the Codex cloud environment "  # how its step begins while the codex CLI cannot see the environment
# Why a cloud job waits for a hand launch, from the reason the harness wrote before ": rh cloud prompt".
_WHY = ((r"web route", "Cloud jobs here are set to start by hand (cloud.route is web)."),
        (r"the codex CLI cannot see environment '([^']*)'",
         r"The codex CLI on this Mac cannot see environment \1, so the harness cannot start it."),
        (r"launched by hand on request", "You chose to start it by hand."),
        (r"(launch outcome unknown|supervisor restarted mid-launch).*",
         "Starting it from here gave no clear answer, so a task may already exist. Look in Codex before starting "
         "another."))
_KEYS = (("⌘K / Ctrl K", "Jump to a job, a page or an action"), ("j / ↓", "Next row"), ("k / ↑", "Previous row"),
         ("Enter", "Open it"), ("n", "New job"), ("i", "Open the Inbox"), ("r", "Answer or mark done"),
         ("a", "Link a Codex task"), ("h", "Paste a patch"), ("u", "Resume"), ("s", "Pause"),
         ("c", "Cancel a job that has not started"), ("l", "Save a result to a branch"), ("t", "Check now"),
         ("⌘ / Ctrl Enter", "Send the form you are typing in"), ("Esc", "Close or leave a field"), ("?", "This sheet"))
# What the palette can press on a page, by the key that already does it there.
_PRESS = (("t", "Check now", "the harness, once"), ("u", "Resume", "lift the app's pause"),
          ("s", "Pause this job", "nothing new starts for it"), ("c", "Cancel this job", "before it starts"),
          ("a", "Link the Codex task", "paste the task link"), ("h", "Paste the patch", "from the Codex web view"),
          ("r", "Answer or mark done", "what this page asks"), ("l", "Save to a branch", "the finished result"),
          ("x", "Dismiss", "close the item without doing it"))
_ENV = re.compile(r"in environment (\S+?),")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_PRESSURE = {1: "normal", 2: "warning", 4: "critical"}
# The composer's prompt: what it is for, not suggestions the harness never made.

# Icons, 24-unit strokes from the look's set.
_ICONS = {
    "today": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12 18.4 5.6"/>',
    "inbox": ('<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6'
              'l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>'),
    "new": ('<path d="M12 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-6"/>'
            '<path d="M17.6 3.4a1.9 1.9 0 0 1 2.9 2.7L12 14.6l-3.6.9.9-3.6z"/>'),
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.8-3.8"/>',
    "mac": '<path d="M5 16V7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v9"/><path d="M2.5 19h19"/><path d="M3.5 16h17l1 3h-19z"/>',
    "cloud": '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
    "chev": '<path d="m10 7 5 5-5 5"/>',
    "up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "launch": ('<path d="M14 4h6v6"/><path d="M10 14 20 4"/>'
               '<path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>'),
    "question": ('<path d="M20 12a8 8 0 0 1-11.6 7.1L4 20l.9-4.4A8 8 0 1 1 20 12z"/>'
                 '<path d="M9.8 9.6a2.3 2.3 0 0 1 4.4.8c0 1.5-2.2 2-2.2 2"/><path d="M12 15.5h.01"/>'),
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "patch": '<path d="M12 4v11M7 10.5l5 5 5-5"/><path d="M4.5 19.5h15"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    "stop": '<circle cx="12" cy="12" r="9"/><path d="M8.6 15.4 15.4 8.6"/>',
    "step": '<path d="M4 6.5h16M4 12h16M4 17.5h9"/>',
    "again": '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 4.5v4h4"/>',
    "resume": '<path d="M8 5.5v13l10-6.5z"/>',
}
_RING = '<circle cx="7" cy="7" r="5.6" fill="none" stroke="currentColor" stroke-width="1.4"/>'
_SHAPES = {
    "draft": ('<circle cx="7" cy="7" r="5.6" fill="none" stroke="currentColor" stroke-width="1.4" '
              'stroke-dasharray="2.1 2.3"/>'),
    "empty": _RING,
    "wait": _RING + ('<path d="M7 4.4V7l1.7 1.1" fill="none" stroke="currentColor" stroke-width="1.3" '
                     'stroke-linecap="round"/>'),
    "held": _RING + '<path d="M5.7 5v4M8.3 5v4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
    "quarter": _RING + '<path d="M7 7V3.9A3.1 3.1 0 0 1 10.1 7Z" fill="currentColor"/>',
    "half": _RING + '<path d="M7 7V3.9A3.1 3.1 0 0 1 7 10.1Z" fill="currentColor"/>',
    "three": _RING + '<path d="M7 7V3.9A3.1 3.1 0 0 1 7 10.1A3.1 3.1 0 0 1 3.9 7Z" fill="currentColor"/>',
    "handoff": _RING + ('<path d="M5.1 8.9 8.7 5.3M5.9 5.2h2.9v2.9" fill="none" stroke="currentColor" '
                        'stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'),
    "done": ('<circle cx="7" cy="7" r="6.3" fill="currentColor"/><path class="k" d="m4.3 7.2 1.8 1.8 3.6-3.7" '
             'fill="none" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'),
    "failed": ('<circle cx="7" cy="7" r="6.3" fill="currentColor"/><path class="k" d="m4.9 4.9 4.2 4.2m0-4.2-4.2 4.2" '
               'fill="none" stroke-width="1.6" stroke-linecap="round"/>'),
    "cancelled": _RING + '<path d="M4.9 9.1 9.1 4.9" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
}
# A job's ring: dashed draft, empty ready, a clock while it waits, bars while on hold, a quarter while it starts, half
# while it runs, three quarters while its result is checked, filled when done. Amber marks a hand launch and a job past
# its limit; red, a finished job that needs a person.
_RINGS = {"draft": ("draft", "faint"), "ready": ("empty", "faint"), "waiting": ("wait", "faint"),
          "held": ("held", "muted"), "launching": ("quarter", "accent"), "handoff": ("handoff", "amber"),
          "running": ("half", "accent"), "stalled": ("half", "amber"), "collecting": ("three", "accent"),
          "done": ("done", "accent"), "cancelled": ("cancelled", "faint"), "env": ("wait", "amber"),
          **{s: ("failed", "red") for s in _FAILED}}
# The sea: where a seat's jellyfish sits in a small tank, as fractions of it, and how fast each swims.
_SLOTS = ((0.1, 0.3), (0.9, 0.22), (0.03, 0.9), (0.79, 0.76), (0.33, 0.93), (0.985, 0.86), (0.64, 0.07))
_SPEEDS = (0.8, 0.95, 1.1)
_MARK = ((2, 3, 4), (1, 2, 3, 4, 5), (0, 1, 2, 3, 4, 5, 6), (0, 2, 4, 6), (0, 2, 4, 6), (1, 4), (2, 5))


def _text(value):
    return escape(str(value), quote=True)


def _sig(*fields):
    return format(zlib.crc32(repr(fields).encode()), "08x")


def _still(html):
    """The markup without its ticking times, so a signature changes only when what the markup says changes."""
    return re.sub(r'<span class="when"[^>]*>[^<]*</span>', "", html)


def _m(text):
    """Machine words inside a sentence (a duration, a path, a count) in the mono face, already escaped."""
    return f'<span class="mono">{_text(text)}</span>'


def _span(seconds):
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _relative(timestamp, now):
    seconds = round(now - timestamp)
    return f"{_span(seconds)} ago" if seconds >= 0 else f"in {_span(-seconds)}"


def _when(timestamp, now, key, pre="", due=None, short=False):
    """A relative time the live page keeps ticking; data-tk lets it update a row that did not otherwise change.
    `due` replaces a future time once it has passed ("retrying on the next check" rather than "8 s ago"); `short`
    gives just the span ("12 s")."""
    marks = (f' data-pre="{_text(pre)}"' if pre else "") + (f' data-due="{_text(due)}"' if due else "")
    if short:
        marks += ' data-short="1"'
        text = _span(max(0, round(now - timestamp)))
    else:
        text = due if due and now >= timestamp else pre + _relative(timestamp, now)
    return f'<span class="when" data-t="{_text(timestamp)}" data-tk="{_text(key)}"{marks}>{_text(text)}</span>'


def _clock(timestamp, now):
    """A time of day for today, else the day: what the list and the timeline show, so they never tick."""
    t, n = time.localtime(timestamp), time.localtime(now)
    if (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday):
        return f"{t.tm_hour:02d}:{t.tm_min:02d}"
    return f"{t.tm_mday} {_MONTHS[t.tm_mon - 1]}"


def _date(now):
    t = time.localtime(now)
    return f"{_DAYS[t.tm_wday]} {t.tm_mday} {_MONTHS[t.tm_mon - 1]} · {t.tm_hour:02d}:{t.tm_min:02d}"


def _midnight(now):
    t = time.localtime(now)
    return now - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec) - now % 1


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dot(tone, pulse=False):
    return f'<i class="dot {tone}{" pulse" if pulse else ""}" aria-hidden="true"></i>'


def _ic(name, small=False):
    return f'<svg class="ic{" sm" if small else ""}" viewBox="0 0 24 24" aria-hidden="true">{_ICONS[name]}</svg>'


def _mark(size=18):
    step, cell = size / 7, size * 0.0983
    rects = "".join(f'<rect x="{0.4 + i * step:.2f}" y="{0.4 + j * step:.2f}" width="{cell:.2f}" height="{cell:.2f}" '
                    'rx="0.4"/>' for j, row in enumerate(_MARK) for i in row)
    return (f'<svg class="mark" viewBox="0 0 {size} {size}" width="{size}" height="{size}" aria-hidden="true">'
            f'{rects}</svg>')


def _ring(state, hue=None):
    """A job's state as one ring, named for a screen reader; `hue` overrides its colour (amber in Needs you)."""
    shape, tone = _RINGS.get(state, ("empty", "faint"))
    tone = hue or tone
    word = _STATES.get(state, (state,))[0]
    return (f'<svg class="st t-{tone}" viewBox="0 0 14 14" width="14" height="14" role="img" '
            f'aria-label="{_text(word)}">{_SHAPES[shape]}</svg>')


def _where(kind):
    """Where a job runs, as the tabs' icon, named for a screen reader."""
    name, icon = (HERE, "mac") if kind == "local-shell" else ("Codex cloud", "cloud") if kind == "codex-cloud" else (
        _platform(kind), "")
    return (f'<span class="where" role="img" aria-label="{_text(name)}">{_ic(icon, True)}</span>' if icon
            else f'<span class="where mono">{_text(str(kind)[:6])}</span>')


def _cells(filled, total=20, tone=""):
    """A meter as a row of cells on the tank's grid."""
    filled = max(0, min(total, filled))
    cells = "".join('<i class="on"></i>' if i < filled else "<i></i>" for i in range(total))
    return f'<span class="cells{" " + tone if tone else ""}" aria-hidden="true">{cells}</span>'


def _creature(name, tone, k, busy=1, at=None):
    """One seat's jellyfish for the sea: teal while it works (pulsing faster while it runs more than one job), amber
    while it waits on you, dim while idle. `at` places it in a small tank, as fractions of its width and height."""
    fx, fy = at or (_SLOTS[k] if k < len(_SLOTS) else ((0.5 + k * 0.618) % 1, (0.2 + k * 0.381) % 1))
    return {"name": name, "tone": tone, "fx": fx, "fy": fy,
            "sp": round(_SPEEDS[k % len(_SPEEDS)] * (1.25 if busy > 1 else 1), 2)}


def _tank(creatures, key, fauna=None):
    """The canvas the client draws the sea on, in dots; it draws nothing without the script. `fauna` is the rest of the
    sea life (see _fauna)."""
    data = _text(json.dumps(creatures, separators=(",", ":")))
    life = f' data-fauna="{_text(json.dumps(fauna, separators=(",", ":")))}"' if fauna else ""
    return (f'<canvas class="tank-c" data-tank data-creatures="{data}"{life} data-region="{key}" data-still '
            f'data-sig="{_sig(data + life)}" aria-hidden="true"></canvas>')


_SCHOOL, _GERMS, _BLOOM = 9, 7, 12  # at most this many fish, bacteria colonies and plankton clouds in the sea


def _fauna(jobs, view, ctx):
    """The sea life besides the seats' jellyfish, each a count the page already shows, up to a cap: a fish for each
    job live (at most 9), a school of up to 9 small fish for the jobs done today, a colony of bacteria for each job
    that failed or stopped today (at most 7), a cloud of plankton for each job up next (at most 12), the eel while
    something checks the harness, and the octopus on the sea floor, amber while anything needs you."""
    ended = [j for j in jobs if j.where == "recent" and j.group in ("ended", "needs")]
    sup = view.get("supervisor") if isinstance(view.get("supervisor"), dict) else {}
    loop = view.get("checks") if isinstance(view.get("checks"), dict) else {}
    fresh = _number(sup.get("age_s")) and sup["age_s"] <= _STALE
    return {"fish": min(_SCHOOL, sum(j.group == "live" for j in jobs)),
            "school": min(_SCHOOL, sum(j.state == "done" for j in ended)),
            "bacteria": min(_GERMS, sum(j.state in _FAILED for j in ended)),
            "plankton": min(_BLOOM, sum(j.group == "next" for j in jobs)),
            "eel": bool(fresh and loop.get("mode") != "paused"),
            "octopus": {"tone": "amber" if ctx["needs"] else "faint"}, "floor": True}


def _state(state):
    word, tone = _STATES.get(state, (state, "neutral"))
    return f'<span class="state">{_dot(tone, state == "running")}{_text(word)}</span>', tone


def _platform(route):
    name, _, env = str(route).partition(":")
    return f"Cloud environment {env}" if name == "cli-env" and env else _PLATFORMS.get(str(route), str(route))


def _sentence(text):
    text = str(text).strip()
    return text[:1].upper() + text[1:]


def _clause(text):
    return str(text).strip().rstrip(".")


def _plain(detail):
    """A state's detail as the harness wrote it, in the app's words: process ids and CLI hints left out."""
    text = str(detail or "").strip()
    rules = ((r"pid \d+", ""), (r"launched task_\w+", ""), (r"attached task_\w+", "Linked to its Codex task"),
             (r"recovered after restart: pid \d+", "Picked up again after a restart"),
             (r"harvested patch; .*", "Patch saved; checking it"),
             (r"task completed; collecting", "Finished in Codex; collecting the result"),
             (r"gate waiting for the heavy lease", "Waiting its turn to run the check"),
             (r"command and gate exited 0", "The command and its check passed"),
             (r"command exited (\d+)", r"The command exited with code \1"),
             (r"acceptance gate exited (\d+)", r"The check exited with code \1"),
             (r"launch deferred: the codex CLI cannot see the environment", _STATES["env"][0]),
             (r"launch deferred: (.*)", r"Waiting to start: \1"),
             (r"no finish after (\d+) min", r"Past its \1-minute limit"),
             (r"attached from the web composer", ""),
             (r".*rh cloud prompt .*", "Launch it in Codex by hand"))
    text = text.replace(" (operator step)", "")
    for pattern, words in rules:
        if re.fullmatch(pattern, text):
            return re.sub(pattern, words, text)
    return _sentence(re.sub(r"\s*\(?pid \d+\)?", "", text))


_NOT_AN_END = ("e.g.", "i.e.", "etc.", "vs.", "cf.")  # a sentence does not end on these; "etc." does before a capital


def _lead(meta):
    """A brief as its job's page shows it: the first sentence of its first paragraph, whole, for the heading, with the
    paragraph's line breaks read as spaces (briefs are often hard-wrapped), and the rest under it (see _reflow). The
    paragraph ends at a blank line or a list item, and a number such as the "1." of a first list item ends no
    sentence."""
    text = str((meta or {}).get("brief") or "").strip()
    if not text:
        return "", ""
    stop = re.search(r"\n\s*\n|\n[ \t]*(?:[-*•]|\d+[.)])\s", text)
    para = text[:stop.start()] if stop else text
    end = len(para)
    for found in re.finditer(r"[.!?](?=\s|$)", para):
        word = para[:found.end()].split()[-1].lstrip("(\"'").lower()
        if word[:-1].isdigit():
            continue
        if word not in _NOT_AN_END or word == "etc." and para[found.end():].lstrip()[:1].isupper():
            end = found.end()
            break
    head = " ".join(para[:end].split())
    return (head if head.lower().endswith(_NOT_AN_END) else head.rstrip(".")), text[end:].strip()


_ITEM = re.compile(r"[ \t]*(?:[-*•]|\d+[.)])\s")


def _reflow(text):
    """A brief's text as prose: blank lines and list items keep their own lines, a line inside a ``` fence stays as
    written, and every other line break is read as a space, since briefs are often hard-wrapped."""
    out, fenced = [], False
    for line in str(text or "").splitlines():
        fence = line.lstrip().startswith("```")
        if fenced or fence or not line.strip() or _ITEM.match(line) or not out or not out[-1].strip() or (
                out[-1].lstrip().startswith("```")):
            out.append(line if fenced or fence else line.rstrip())
        else:
            out[-1] = out[-1].rstrip() + " " + line.strip()
        fenced ^= fence
    return "\n".join(out)


def _title(meta, jid):
    """A job's name in the list and the palette: the first sentence of its brief, cut to at most 98 characters and an
    ellipsis when it runs past 100, else its id."""
    line = _lead(meta)[0]
    if len(line) > 100:
        line = line[:98].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return line or str(jid)


def _size(n):
    if not _number(n):
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _duration(seconds):
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s" if seconds < 600 else f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def _hidden(ctx, fields):
    back = [("next", ctx["back"])] if ctx.get("back") else []
    return "".join(f'<input type="hidden" name="{name}" value="{_text(value)}">'
                   for name, value in (("t", ctx["token"]), *fields, *back))


def _cls(kind):
    return f"btn {kind}" if kind else "btn"


def _url(ctx, path):
    return _text(f"{path}?t={ctx['token']}")


def _button(ctx, route, fields, label, key="", kind="", say=None):
    """A one-step action."""
    k = f' data-k="{key}"' if key else ""
    return (f'<form class="act" method="post" action="{route}" data-act data-label="{_text(say or label)}">'
            f'{_hidden(ctx, fields)}<button class="{_cls(kind)}"{k}>{label}</button></form>')


def _popover(ctx, route, fields, label, key, inputs, submit, kind="", note="", where=""):
    """An action that opens its form in place: the inputs, what submitting does, and the button that confirms."""
    confirm = "btn danger fill" if "danger" in kind.split() else "btn primary"
    note = f'<p class="note">{note}</p>' if note else ""
    k = f' data-k="{key}"' if key else ""
    return (f'<details class="act"><summary class="{_cls(kind)}"{k}>{label}…</summary>'
            f'<form class="pop{" " + where if where else ""}" method="post" action="{route}" data-act '
            f'data-label="{_text(submit)}">{_hidden(ctx, fields)}{inputs}{note}<button class="{confirm}">{submit}</button>'
            '</form></details>')


def _field(label, name, *, required=False, placeholder="", key="", mono=False, rows=0, value=""):
    attrs = (f' name="{name}"' + (" required" if required else "") + (f' data-k="{key}"' if key else "")
             + (f' placeholder="{_text(placeholder)}"' if placeholder else "") + (' class="mono" spellcheck="false"'
                                                                                 if mono else ""))
    box = (f'<textarea{attrs} rows="{rows}">{_text(value)}</textarea>' if rows
           else f'<input type="text"{attrs} autocomplete="off" value="{_text(value)}">')
    return f"<label>{label}{box}</label>"


def _acts(items):
    return f'<div class="acts">{"".join(items)}</div>' if items else ""


def _row(key, inner, cls="row", attrs=""):
    return f'<li class="{cls}" data-key="{_text(key)}" data-sig="{_sig(_still(inner))}"{attrs}>{inner}</li>'


def _codex_link(label="Open Codex"):
    return (f'<a class="btn" href="{_CODEX}" target="_blank" rel="noopener noreferrer">{label}'
            '<span aria-hidden="true">↗</span><span class="sr"> (opens a new tab)</span></a>')


def _copy(target, label):
    return f'<button type="button" class="btn small" data-copy="{_text(target)}" hidden>{label}</button>'


def _setup_item(item, ready):
    return (item.get("kind") == "operator-step" and not item.get("job") and not ready
            and str(item.get("action") or "").startswith(_SETUP))


def _native_item(item):
    return item.get("kind") == "operator-step" and not item.get("job") and str(item.get("action") or "").startswith(
        _NATIVE)


def _env_of(item):
    found = re.search(r"'([^']+)'", str(item.get("action") or ""))
    return found.group(1) if found else "?"


def _why(detail):
    """Why a job waits for a hand launch, in the app's words; empty when the harness gave no reason."""
    reason = str(detail or "").split(": rh cloud prompt", 1)[0].strip() if ": rh cloud prompt" in str(detail) else ""
    for pattern, words in _WHY:
        if re.fullmatch(pattern, reason):
            return re.sub(pattern, words, reason)
    return _sentence(reason) + "." if reason else ""


def _ask_line(item):
    """(what to do, detail) for one inbox item, in plain words."""
    job, kind = item.get("job"), item.get("kind")
    action, detail = str(item.get("action") or ""), str(item.get("detail") or "")
    if kind == "web-launch" and job:
        return "Launch it in Codex, then link the task", ""
    if kind == "harvest" and job:
        if "--bundle" in action:
            return "Paste the patch from Codex", ("It ran in the Codex web app, which the codex CLI cannot read, and "
                                                  "its Changes view may have expired. Send the bundle-export follow-up "
                                                  "in its task, then paste the reply.")
        return "Paste the patch from Codex", (f"The codex CLI could not export its diff ({_clause(detail)}). Save the "
                                              "staged diff from the Codex web view and paste it.")
    if _native_item(item):
        return ("Choose an environment the codex CLI lists",
                "Cloud jobs wait for it and start by themselves once the CLI lists it.")
    if kind == "operator-step" and action.startswith(_SETUP):
        return "Set up Codex cloud", "Cloud jobs wait until Codex has an environment for this repository."
    if kind == "refusal":
        return f"The job stopped at a refusal: {_clause(action)}", detail
    return _sentence(action or kind), detail


def _ask_kind(item):
    """Which Inbox tab an item belongs under, and its icon."""
    kind = item.get("kind")
    if kind == "decision":
        return "question", "question"
    if kind == "web-launch":
        return "launch", "launch"
    if kind == "harvest":
        return "patch", "patch"
    return "other", "stop" if kind == "refusal" else "step"


class _Job:
    """Everything the app says about one job, worked out once: which group it belongs in, what it needs from a
    person, the pauses on it, and the actions it offers."""

    def __init__(self, row, where, ctx):
        self.row, self.where, self.ctx = row, where, ctx
        self.id, self.state = row["id"], row["state"]
        self.meta = ctx["meta"].get(self.id) or {}
        self.kind = self.meta.get("kind", "")
        self.seat = str(self.meta.get("seat") or "")
        self.word, self.tone = _STATES.get(self.state, (self.state, "neutral"))
        self.stops = ctx["scopes"].get(f"job:{self.id}", [])
        self.items = ctx["asks"].get(self.id, [])
        self.handoff = self.state == "handoff" and not any(i.get("kind") == "web-launch" for i in self.items)
        self.mine = [s for s in self.stops if s.get("owner") == "app"]
        ended = self.meta.get("updated")
        self.brief = str(self.meta.get("brief") or "").strip()
        made = self.meta.get("created")
        rerun = ctx["rerun"].get(self.brief) if self.brief else None
        self.failure = (self.state in _FAILED and where == "recent" and self.id not in ctx["followed"]
                        and not (_number(rerun) and _number(made) and made <= rerun)
                        and (not _number(ended) or ctx["now"] - ended < _FAILURE_WINDOW))
        self.held = self.state == "held" and not self.items
        self.setup = bool(ctx["setup"]) and self.kind == "codex-cloud" and self.state in _PRE
        self.native = bool(ctx.get("native")) and self.kind == "codex-cloud" and self.state == "ready"
        if self.native:
            self.word, self.tone = _STATES["env"]
        self.count = len(self.items) + self.handoff + len(self.mine) + self.failure + self.held
        if self.count:
            self.group = "needs"
        elif where == "live" or self.state == "draft" or (self.state == "ready" and not row.get("detail")
                                                           and not self.native):
            self.group = "live"
        elif where == "not_started":
            self.group = "next"
        else:
            self.group = "earlier" if _number(ended) and ended < ctx["midnight"] else "ended"
        times = [i["created"] for i in self.items if _number(i.get("created"))]
        times += [s["created"] for s in self.mine if _number(s.get("created"))]
        self.first = min(times, default=float("inf"))
        self.title = _title(self.meta, self.id)
        self.place = _WHERE.get(self.kind, str(self.kind)[:6])

    def headline(self, runs=1):
        """The one line the list shows beside the title: what it needs when it needs you, else its state; for a
        failure repeated by `runs` runs of its brief, how many times."""
        if self.items:
            return _ask_line(self.items[0])[0]
        if self.handoff:
            return "Launch it in Codex"
        if self.mine:
            return "Paused from the app"
        detail = _plain(self.row.get("detail"))
        parts = []
        held = [s for s in self.stops if s.get("owner") != "app"]
        if held:
            parts.append(f"{'Paused' if held[0].get('kind') == 'pause' else 'Stopped'} by {held[0]['owner']}")
        elif self.row.get("paused"):
            parts.append("Paused")
        if self.failure or self.held:
            word = f"{self.word} {runs} times" if runs > 1 else self.word
            return " · ".join(parts + [f"{word}: {detail}" if detail else word])
        if self.setup:
            return " · ".join(parts + ["Waits for Codex cloud to be set up"])
        if self.native:
            return " · ".join(parts + [self.word])
        if self.group == "live" and self.state in ("draft", "ready", "launching"):
            parts.append("Starting")
        elif self.state == "running" and self.kind in _PLACES:
            parts.append(f"Running {_PLACES[self.kind]}")
        else:
            parts.append(self.word)
        if detail and self.state not in ("draft", "ready", "launching"):
            parts.append(detail)
        return " · ".join(parts)

    def since(self):
        """The time the list shows: when it started waiting on you, started running, or finished."""
        if self.first != float("inf"):
            return self.first
        key = {"live": "started", "ended": "updated", "earlier": "updated", "needs": "updated"}.get(self.group,
                                                                                                    "created")
        stamp = self.meta.get(key) or self.meta.get("created")
        return stamp if _number(stamp) else None

    def act(self):
        """The one control a Needs you row offers beside its line: a one-tap action, or the way to where it is done."""
        ctx = self.ctx
        if not ctx["live"] or self.group != "needs":
            return ""
        page = _url(ctx, "/job/" + self.id)

        def go(href, label, icon, key):
            return f'<a class="btn gbtn" href="{href}" data-nav data-k="{key}">{_ic(icon, True)}{label}</a>'
        if self.items:
            item = self.items[0]
            kind = item.get("kind")
            if kind == "decision":
                return go(_url(ctx, f"/inbox/{item['id']}"), "Answer", "question", "r")
            if kind == "web-launch":
                return go(page, "Launch by hand", "launch", "a")
            if kind == "harvest":
                return go(page, "Paste patch", "patch", "h")
            return _button(ctx, "/inbox/resolve", [("id", item["id"]), ("note", "")], f'{_ic("check", True)}Mark done',
                           "r", "gbtn", "Mark done")
        if self.handoff:
            return go(page, "Launch by hand", "launch", "a")
        if self.mine:
            fields = [("job", self.id)] + ([("pause", "on")] if self.mine[0].get("kind") == "pause" else [])
            return _button(ctx, "/resume", fields, f'{_ic("resume", True)}Resume', "u", "gbtn", "Resume this job")
        if self.failure:
            return _button(ctx, "/again", [("job", self.id), ("changed", "")], f'{_ic("again", True)}Run again', "r",
                           "gbtn fail", "Run again")
        if self.setup:
            return go(page, "Set up Codex cloud", "cloud", "r")
        return ""

    def item(self, runs=1):
        """The list entry: the title over the line that says why, a link to the job's page across the whole row,
        and the one control it offers. In Needs you the ring is amber, or red for a failure; a failure repeated by
        `runs` runs of its brief is one row, for its newest run."""
        ctx = self.ctx
        since = self.since()
        hue = ("red" if self.state in _FAILED else "amber") if self.group == "needs" else None
        act = self.act()
        cells = (f'{_ring("env" if self.native else self.state, hue)}<span class="name">{_text(self.title)}</span>'
                 f'<span class="sub">{_text(self.headline(runs))}</span>{_where(self.kind)}'
                 f'<span class="time mono num">{_clock(since, ctx["now"]) if since is not None else ""}</span>')
        link = (f'<a class="item" href="{_url(ctx, "/job/" + self.id)}" data-nav>{cells}</a>' if ctx["live"]
                else f'<div class="item">{cells}</div>')
        do = f'<span class="do">{act}</span>' if act else ""
        return _row(f"job:{self.id}", link + do, attrs=f' data-where="{_text(self.place)}" data-tone="{self.tone}"')


def _need_row(ctx, key, ask, detail, when, cli, job=None, title=None, status=""):
    """The snapshot's printed card for one thing a person can settle, with the command that settles it."""
    about = ""
    if job:
        tail = f" · {_text(status)}" if status else ""
        about = f'<p class="for">For {_text(title or job)}{tail}</p>'
    say = (f'<p class="ask-t">{ask}</p>' + (f'<p class="detail">{detail}</p>' if detail else "") + about
           + (f'<p class="for">{when}</p>' if when else "") + f'<p class="cli"><code>{_text(cli)}</code></p>')
    return _row(key, say)


def _setup_card(ctx, view, lead=True):
    """Codex cloud set up from the app: the settings, a link to Codex, and the environment ID back."""
    spec = str(view.get("env_spec") or "").strip()
    first = "primary" if lead else ""
    if not ctx["live"]:
        return ('<div class="ask-card"><p class="label">' + _dot("warn") + 'Needs you</p><h3>Set up Codex cloud</h3>'
                '<p class="detail">Cloud jobs wait until Codex has an environment for this repository.</p>'
                '<p class="cli"><code>rh env spec</code> · <code>rh config set cloud.env_id &lt;ID&gt;</code></p></div>')
    settings = (f'<pre class="envspec" id="envspec" tabindex="0">{_text(spec)}</pre>{_copy("envspec", "Copy settings")}'
                if spec else '<p class="detail">The settings could not be read here; <code>rh env spec</code> prints '
                             'them.</p>')
    form = (f'<form method="post" action="/setup" data-act data-label="Save environment">{_hidden(ctx, [])}'
            '<label>Environment ID<input type="text" name="env_id" required autocomplete="off" spellcheck="false" '
            'class="mono" pattern="[A-Za-z0-9_.:\\-]{1,128}" data-k="r"></label>'
            f'<button class="{_cls(first)}">Save and start cloud jobs</button></form>')
    return ('<div class="ask-card setup"><p class="label">' + _dot("warn") + 'Needs you</p><h3>Set up Codex cloud</h3>'
            '<p class="detail">Cloud jobs wait until Codex has an environment for this repository. You do this once.'
            '</p><ol class="steps">'
            f'<li><div><p>Create an environment in Codex with these settings.</p>{settings}</div></li>'
            f'<li><div><p>Open Codex, create and publish it, then copy its ID.</p>{_codex_link()}</div></li>'
            f'<li><div>{form}</div></li></ol></div>')


def _native_card(ctx, item, jid=None, lead=True):
    """While the codex CLI cannot see the environment: why cloud jobs wait, and the one thing to do about it, in order
    (see what the CLI lists, name one, or make one and check it with rh doctor); on a job's page, why it waits and the
    hand launch as the fallback."""
    env = _text(_env_of(item))
    said = str(item.get("detail") or "").split(". Cloud jobs wait")[0]
    why = (f'<p class="detail">This Mac starts cloud jobs with the codex CLI, and the CLI does not list environment '
           f'<code>{env}</code>. So far, environments made under Settings → Codex Cloud → Environments are not in '
           f'its list. Each waiting job starts by itself once it is (checked every 10 minutes).</p>'
           + (f'<p class="said mono">{_text(said)}</p>' if said else ""))
    if jid:
        choose = (f'<a class="btn primary" href="{_url(ctx, "/inbox/" + str(item["id"]))}" data-nav data-k="r">'
                  f'{_ic("cloud", True)}Choose environment</a>')
        act = (_acts([choose, _button(ctx, "/handoff", [("job", jid)], "Launch by hand instead", "a", "",
                                      "Launch by hand")])
               if ctx["live"] else f'<p class="cli"><code>rh config set cloud.env_id &lt;name or ID&gt;</code> · '
               f'<code>rh cloud handoff {_text(jid)}</code></p>')
        return (f'<div class="ask-card"><p class="label">{_ring("env")}{_STATES["env"][0]}</p>'
                f'<h3>The codex CLI does not list environment {env}</h3>{why}{act}</div>')
    if not ctx["live"]:
        form = '<p class="cli"><code>rh config set cloud.env_id &lt;name or ID&gt;</code></p>'
    else:
        form = (f'<form method="post" action="/setup" data-act data-label="Use environment">{_hidden(ctx, [])}'
                '<label>Environment name or ID<input type="text" name="env_id" required autocomplete="off" '
                'spellcheck="false" class="mono" pattern="[A-Za-z0-9_.:\\-]{1,128}" placeholder="e.g. research-harness-cli" '
                'aria-describedby="env-hint" data-k="r"></label><p class="note" id="env-hint">As <code>codex cloud</code> '
                'prints it: letters, digits and <code>. _ : -</code>, up to 128.</p>'
                f'<button class="{_cls("primary" if lead else "")}">Use environment</button></form>')
    return (f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you</p>'
            f'<h3>Choose an environment the codex CLI lists</h3>{why}<ol class="steps">'
            '<li><div><p>See the environments it lists: run <code>codex cloud</code> in a terminal.</p></div></li>'
            f'<li><div>{form}</div></li>'
            '<li><div><p>None for this repository? Make one in Codex, then run <code>rh doctor</code>: it says whether '
            f'the CLI lists it.</p>{_codex_link()}</div></li></ol></div>')


def _launch_card(ctx, jid, opened, lead, foot=""):
    """A hand launch in order: copy the prompt, open Codex, paste the task link back."""
    first = "primary" if lead else ""
    if not ctx["live"]:
        return (f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you</p><h3>Launch it in Codex</h3>'
                f'<p class="cli"><code>rh cloud prompt {_text(jid)}</code> · <code>rh cloud attach {_text(jid)} '
                f'&lt;task id&gt;</code></p>{foot}</div>')
    prompt = opened.get("prompt")
    why = _why((opened.get("job") or {}).get("detail")) or "The harness cannot start this job from here."
    env = _ENV.search(str((opened.get("job") or {}).get("detail") or ""))
    where = f" in environment <code>{_text(env.group(1))}</code>" if env else ""
    pid = f"prompt-{jid}"
    # The prompt shows four whole lines with the last fading; the page's script drops the fade when they hold all of
    # it, and otherwise shows Show all.
    copy = (f'<div class="clip" data-clip><textarea class="prompt mono" id="{_text(pid)}" rows="4" readonly>'
            f'{_text(prompt)}</textarea></div><div class="clip-acts">{_copy(pid, "Copy prompt")}'
            '<button type="button" class="btn small quiet" data-more hidden>Show all</button></div>'
            if isinstance(prompt, str) and prompt.strip() else
            f'<p class="detail">The prompt could not be read here; <code>rh cloud prompt {_text(jid)}</code> prints it.'
            '</p>')
    attach = (f'<form method="post" action="/attach" data-act data-label="Link task">{_hidden(ctx, [("job", jid)])}'
              '<label>Paste the task link<input type="text" name="task_id" required autocomplete="off" '
              'spellcheck="false" class="mono" placeholder="https://chatgpt.com/codex/tasks/task_…" data-k="a">'
              f'</label><button class="{_cls(first)}">Link task</button></form>')
    return ('<div class="ask-card launch"><p class="label">' + _dot("warn") + 'Needs you</p>'
            f'<h3>Launch it in Codex</h3><p class="detail">{_text(why)} Start it in the Codex '
            'web app, then link the task so this page can follow it.</p><ol class="steps">'
            f'<li><div><p>Copy the prompt.</p>{copy}</div></li>'
            f'<li><div><p>Open Codex, start a task{where} and paste the prompt.</p>{_codex_link()}</div></li>'
            f'<li><div>{attach}</div></li></ol>{foot}</div>')


def _dismiss(ctx, iid):
    return _popover(ctx, "/inbox/resolve", [("id", iid)], "Dismiss", "x",
                    _field("Why dismiss it? (optional)", "note"), "Dismiss", "quiet",
                    "Closes this item without doing it.", "left up")


def _ask_form(ctx, item, lead):
    """The form that settles one inbox item that is not a launch or the setup."""
    iid, kind, jid = item["id"], item.get("kind"), item.get("job")
    first = "primary" if lead else ""
    if kind == "harvest" and jid:
        form = (f'<form method="post" action="/harvest" data-act data-label="Save patch">{_hidden(ctx, [("job", jid)])}'
                + _field("The patch or bundle reply, as copied", "text", required=True, key="h", mono=True, rows=8)
                + f'<button class="{_cls(first)}">Save patch</button></form>')
        return form + f'<div class="foot">{_dismiss(ctx, iid)}</div>'
    if kind == "decision":
        return (f'<form class="composer big" method="post" action="/inbox/resolve" data-act data-label="Record decision">'
                f'{_hidden(ctx, [("id", iid)])}<label class="sr" for="answer-{iid}">Your decision</label>'
                f'<textarea class="c-text" id="answer-{iid}" name="note" rows="3" required data-k="r" '
                'placeholder="Your decision, in your own words"></textarea><div class="c-row">'
                '<span class="c-hint mono">⌘↵ record</span>'
                f'<button class="{_cls(first)}">Record decision</button></div></form>')
    submit = "Mark handled" if kind == "refusal" else "Mark done"
    return (f'<form method="post" action="/inbox/resolve" data-act data-label="{submit}">'
            f'{_hidden(ctx, [("id", iid)])}{_field("What did you do? (optional)", "note", key="r")}'
            f'<button class="{_cls(first)}">{submit}</button></form>')


def _ask_card(ctx, item, opened, lead, view):
    """One inbox item on the job's page, with the form that settles it written out."""
    iid, kind, jid = item["id"], item.get("kind"), item.get("job")
    if not ctx["live"] and not _setup_item(item, ctx["ready"]):
        ask, detail = _ask_line(item)
        return (f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you</p><h3>{_text(ask)}</h3>'
                + (f'<p class="detail">{_text(detail)}</p>' if detail else "")
                + f'<p class="cli"><code>rh inbox resolve {_text(iid)} &lt;note&gt;</code></p></div>')
    if kind == "web-launch" and jid:
        return _launch_card(ctx, jid, opened, lead, f'<div class="foot">{_dismiss(ctx, iid)}</div>' if ctx["live"] else "")
    if _setup_item(item, ctx["ready"]):
        return _setup_card(ctx, view, lead)
    if _native_item(item):
        return _native_card(ctx, item, lead=lead)
    ask, detail = _ask_line(item)
    when = (_when(item["created"], ctx["now"], f"inbox:{iid}", "asked ") if _number(item.get("created")) else "")
    head = (f'<p class="label">{_dot("warn")}Needs you · {when}</p><h3>{_text(ask)}</h3>'
            + (f'<p class="detail">{_text(detail)}</p>' if detail else ""))
    return f'<div class="ask-card">{head}{_ask_form(ctx, item, lead)}</div>'


def _pause(ctx, jid):
    return _popover(ctx, "/stop", [("job", jid), ("pause", "on")], "Pause job", "s",
                    _field("Why? (optional)", "reason", placeholder="Paused from the app"), "Pause job",
                    "quiet danger", "Nothing new starts for it until you resume it. Work already running continues.")


def _cancel(ctx, jid):
    return _popover(ctx, "/job/cancel", [("job", jid)], "Cancel job", "c", _field("Why?", "reason", required=True),
                    "Cancel job", "quiet danger", "It will not start. You can run it again later.")


def _resume(ctx, stop, lead, what, fields):
    pause = stop.get("kind") == "pause"
    verb = "Paused" if pause else "Stopped"
    fields = fields + ([("pause", "on")] if pause else [])
    when = (_when(stop["created"], ctx["now"], f"stop:{stop.get('id', stop['scope'])}", verb.lower() + " ")
            if _number(stop.get("created")) else "")
    return (verb, f"Nothing new starts for {what} until you resume it.", when,
            [_button(ctx, "/resume", fields, "Resume", "u", "primary" if lead else "", f"Resume {what}")])


def _hold(stop, running):
    """A pause someone else owns: shown where it applies, with who can undo it, but no button."""
    verb, owner = "Paused" if stop.get("kind") == "pause" else "Stopped", _text(stop["owner"])
    tail = "It keeps running; " if running else "Nothing new starts for it; "
    return (f'<p class="hold"><b>{verb} by {owner}:</b> {_text(_clause(stop["reason"]))}. '
            f'<span class="soft">{tail}only {owner} can undo it.</span></p>')


def _checks(view, ctx):
    """The check loop at the foot of the sidebar: who checks the harness, when it last did, and Pause."""
    now, live = ctx["now"], ctx["live"]
    sup, loop = view.get("supervisor"), view.get("checks") if isinstance(view.get("checks"), dict) else None
    age = sup.get("age_s") if isinstance(sup, dict) else None
    counted = _number(age)
    quiet = counted and age > _STALE
    mode = loop.get("mode") if loop else None
    btns, more = [], ""
    inline = (' <span class="soft mono">' + _when(now - age, now, "tick", "") + '</span>') if counted else ""
    if mode == "on":
        word, dot = ("Checking, but not lately", "warn") if quiet else ("Checking automatically", "live")
        btns = [_button(ctx, "/checks", [("on", "0")], "Pause checks", "", "quiet small", "Pause checks"),
                _button(ctx, "/tick", [], "Check now", "t", "quiet small", "Check now")]
    elif mode == "paused":
        word, dot, more = "Checks paused", "warn", "The app starts and collects nothing until you resume or press Check once."
        btns = [_button(ctx, "/checks", [("on", "1")], "Resume", "", "small", "Resume checks"),
                _button(ctx, "/tick", [], "Check once", "t", "quiet small", "Check once")]
    elif mode == "other":
        word, dot = f"Checked by rh supervise (process {loop.get('pid')})", "live"
    elif sup:
        word, dot = "Harness", "warn" if quiet else "good"
        inline = inline or f" checked {_text(age)} s ago"
        more = f"Process {_text(sup.get('pid'))} may have stopped." if quiet else ""
        if live:
            btns = [_button(ctx, "/tick", [], "Check now", "t", "quiet small", "Check now")]
    else:
        word, dot, more = "The harness has not run yet.", "warn", ""
        if live:
            btns = [_button(ctx, "/tick", [], "Check now", "t", "small", "Check now")]
    tone = dot if dot in ("warn", "bad") else ""
    out = [f'<p class="line {tone}"><i class="dot {dot} beat" aria-hidden="true"></i><span>{_text(word)}{inline}'
           '</span></p>']
    if loop and loop.get("failed"):
        out.append('<p class="line bad"><i class="dot bad" aria-hidden="true"></i><span>The last check failed</span></p>')
        more = _text(str(loop["failed"])[:300])
    if more:
        out.append(f'<p class="more">{more}</p>')
    for route in view.get("routes", []):
        name = route["route"]
        if name.startswith("cli-env:") and ctx.get("native"):
            continue  # Needs you already says the CLI cannot see the environment, and why
        retry = _when(route["next_try"], now, f"route:{name}:next", "retrying ", "retrying on the next check")
        pause = ""
        if live and not any(s.get("owner") == "app" for s in ctx["scopes"].get(f"route:{name}", [])):
            pause = _popover(ctx, "/stop", [("route", name), ("pause", "on")], f"Pause {_text(_platform(name))}", "",
                             _field("Why? (optional)", "reason", placeholder="Paused from the app"),
                             f"Pause {_text(_platform(name))}", "quiet small danger",
                             f"No new job starts on {_text(_platform(name))} until you resume it.", "up left")
        out.append(f'<div class="route"><p>{_text(_platform(name))}: {_text(route["last_status"])} · {retry}'
                   f'</p>{pause}</div>')
    for stop in ctx["others"]:
        scope = str(stop["scope"])
        what = ("All launches" if scope == "*" else
                _text(_platform(scope[6:])) if scope.startswith("route:") else _text(scope))
        verb = "paused" if stop.get("kind") == "pause" else "stopped"
        out.append(f'<p class="route warn">{what} {verb} by {_text(stop["owner"])}: '
                   f'{_text(_clause(stop["reason"]))}</p>')
    if btns and live:
        out.append(f'<div class="btns">{"".join(btns)}</div>')
    body = "".join(out)
    return f'<div class="status" data-region="checks" data-sig="{_sig(_still(body))}">{body}</div>'


def _kv(key, value, tone=""):
    return f'<div class="kv{" " + tone if tone else ""}"><span>{key}</span><b>{value}</b></div>'


def _host(view, ctx):
    """This Mac at the sidebar foot: load and memory pressure as rh serve read them, and, only when they hold a heavy
    job back, that heavy jobs wait and that the heavy lease is in use."""
    host = view.get("host")
    if not isinstance(host, dict):
        return ""
    if host.get("error"):
        body = _kv("Load and memory", "not read", "warn") + f'<p class="why">{_text(str(host["error"])[:200])}</p>'
    else:
        load, level, limit = host.get("load1"), host.get("level"), host.get("max_load")
        over = _number(load) and _number(limit) and load > limit
        rows = [_kv("Load, 1 min", (f"{load:.2f}" if _number(load) else "?")
                    + (f" <em>of {limit:g}</em>" if _number(limit) else ""), "warn" if over else "")]
        if level is None:
            rows.append(_kv("Memory pressure", "not read here"))
        else:
            rows.append(_kv("Memory pressure", f'{_text(level)} <em>{_PRESSURE.get(level, "")}</em>',
                            "" if level == 1 else "bad" if _number(level) and level >= 4 else "warn"))
        # Heavy jobs and the heavy lease only when they hold something up; free and may-start go unsaid.
        ok = host.get("ok")
        if not ok:
            rows.append(_kv("Heavy jobs", "wait", "warn"))
        held = host.get("lease") if isinstance(host.get("lease"), dict) else {}
        if held.get("held"):
            age = held.get("age_s")
            rows.append(_kv("Heavy lease", "in use" + (f" · {_span(round(age))}" if _number(age) else "")))
        if not ok and host.get("reason"):
            rows.append(f'<p class="why">{_text(_sentence(host["reason"]))}.</p>')
        body = "".join(rows)
    body = f'<h2 class="side-h">This Mac</h2>{body}'
    return (f'<section class="mac" aria-label="This Mac" data-region="host" data-still data-sig="{_sig(body)}">'
            f'{body}</section>')


def _result(result):
    """The last action's result, as serve hands it over: {"code", "say", "output"}; a plain string is shown as is."""
    if isinstance(result, dict):
        code = result.get("code")
        say, output = str(result.get("say") or ""), str(result.get("output") or "")
    else:
        code, say, output = "", "", str(result)
    code = code if isinstance(code, int) and not isinstance(code, bool) else ""
    tone = "bad" if code not in ("", 0) else "good"
    shown = _text(say or ("Done." if code == 0 else "That did not work." if code != "" else "Last action"))
    details = f'<details><summary>Details</summary><pre>{_text(output)}</pre></details>' if output else ""
    return (f'<div id="result" class="result {tone}" role="status" data-code="{code}" data-say="{_text(say)}">'
            f'<p>{shown}</p>{details}</div>')


def _section(key, heading, body, head_extra="", cls="", hidden=False, h="h2"):
    """A keyed region the script reconciles: a head it can swap, and a body whose rows it can move."""
    head = f'<div class="head"><{h} id="{key}">{heading}</{h}>{head_extra}</div>'
    flag = " hidden" if hidden else ""
    return (f'<section class="s-{key}{" " + cls if cls else ""}" aria-labelledby="{key}" data-region="{key}" '
            f'data-key="section:{key}" data-sig="{_sig(key, hidden, _still(head + body))}"{flag}>{head}'
            f'<div class="body">{body}</div></section>')


def _rows(items, cls="rows"):
    return f'<ul class="{cls}">{"".join(items)}</ul>'


def _region(key, body, tag="div", cls="", attrs=""):
    """A region the script swaps whole when what it says changes."""
    return (f'<{tag} class="{cls or key}" data-region="{key}" data-sig="{_sig(_still(body))}"{attrs}>{body}</{tag}>')


def _kpill(ctx):
    if not ctx["live"]:
        return ""
    return (f'<button type="button" class="k-pill" data-pal aria-label="Jump to anything, ⌘K">{_ic("search", True)}'
            '<span>Jump to anything</span><kbd>⌘K</kbd></button>')


def _top(ctx, left, right=""):
    """The panel's top bar: what this page is on the left, its actions and ⌘K on the right."""
    body = f'{left}<div class="top-r">{right}{_kpill(ctx)}</div>'
    return f'<header class="top" data-region="top" data-still data-sig="{_sig(_still(body))}">{body}</header>'


def _reruns(meta):
    """For each brief that was run again, when the newest run that was run again was added: an older failed run of the
    same brief is settled by it too, so Run again on a repeated failure's row settles every run in it."""
    out = {}
    for m in meta.values():
        prior = meta.get(str(m.get("prior"))) if isinstance(m, dict) and m.get("prior") else None
        brief = str(prior.get("brief") or "").strip() if isinstance(prior, dict) else ""
        if brief and _number(prior.get("created")):
            out[brief] = max(out.get(brief, prior["created"]), prior["created"])
    return out


def _needs(jobs, inbox, stops, ctx):
    """Everything that waits on you, once each and oldest first: a job that needs you (with its open items), the
    app's own pause on no listed job, and an open item for no listed job. Runs of one brief that failed the same way
    and need nothing else are one entry, its runs newest first. Today's Needs you, the top bar, the Inbox badge and
    the Inbox all show this one list and its length, so they cannot disagree."""
    out, repeats = [], {}
    for job in jobs:
        if job.group != "needs":
            continue
        same = (job.brief, _plain(job.row.get("detail"))) if job.failure and job.count == 1 and job.brief else None
        if same in repeats:
            repeats[same]["runs"].append(job)
            continue
        item = job.items[0] if job.items else None
        tab, icon = (_ask_kind(item) if item else ("launch", "launch") if job.handoff
                     else ("failed", "stop") if job.failure else ("other", "resume" if job.mine else "step"))
        since = job.first if job.first != float("inf") else job.since()
        latest = max((i["created"] for i in job.items if _number(i.get("created"))), default=since)
        out.append({"key": f"job:{job.id}", "job": job, "item": item, "stop": None, "ask": job.headline(), "tab": tab,
                    "icon": icon, "jid": job.id, "first": since, "created": latest, "runs": [job]})
        if same:
            repeats[same] = out[-1]
    for entry in repeats.values():
        if len(entry["runs"]) > 1:
            runs = sorted(entry["runs"], key=lambda j: j.meta.get("updated") if _number(j.meta.get("updated")) else 0,
                          reverse=True)
            stamps = [j.since() for j in runs if _number(j.since())]
            entry.update(runs=runs, job=runs[0], jid=runs[0].id, key=f"job:{runs[0].id}", ask=runs[0].headline(len(runs)),
                         first=min(stamps, default=entry["first"]), created=max(stamps, default=entry["created"]))
    known = {j.id for j in jobs}
    attached = any(j.setup and j.group == "needs" for j in jobs)
    for item in inbox:
        if item.get("job") in known or (_setup_item(item, ctx["ready"]) and (attached or item is not ctx["setup"])):
            continue
        tab, icon = _ask_kind(item)
        out.append({"key": f"inbox:{item['id']}", "job": None, "item": item, "stop": None, "ask": _ask_line(item)[0],
                    "tab": tab, "icon": icon, "jid": item.get("job"), "first": item.get("created"),
                    "created": item.get("created")})
    for stop in stops:
        verb = "Paused" if stop.get("kind") == "pause" else "Stopped"
        out.append({"key": f"stop:{stop.get('id', stop['scope'])}", "job": None, "item": None, "stop": stop,
                    "ask": f"{verb} from the app: {_sentence(_clause(stop['reason']))}", "tab": "other",
                    "icon": "resume", "jid": None, "first": stop.get("created"), "created": stop.get("created")})
    return sorted(out, key=lambda e: e["first"] if _number(e["first"]) else float("inf"))


def _need_word(n):
    """The one way the app says how many things wait on you."""
    return f"{n} {'needs' if n == 1 else 'need'} you" if n else "nothing needs you"


def _item_row(ctx, item):
    """Today's row for an open item that belongs to no listed job: what it asks, and the way to it in the Inbox. The
    environment step says what to do, where it stands and the command that checks it, with the one button to it."""
    ask, _ = _ask_line(item)
    _, icon = _ask_kind(item)
    about, act = (f"For job {_text(item['job'])}" if item.get("job") else "From the harness"), ""
    href = _url(ctx, "/inbox/" + str(item["id"]))
    if _native_item(item):
        about = (f'It does not list {_text(_env_of(item))}, so cloud jobs wait · <span class="mono">codex cloud</span> '
                 'lists them, <span class="mono">rh doctor</span> checks')
        if ctx["live"]:
            act = (f'<span class="do"><a class="btn gbtn" href="{href}" data-nav data-k="r">{_ic("cloud", True)}'
                   'Choose…</a></span>')
    when = _clock(item["created"], ctx["now"]) if _number(item.get("created")) else ""
    cells = (f'<span class="st-ic" aria-hidden="true">{_ic(icon, True)}</span><span class="name">{_text(ask)}</span>'
             f'<span class="sub">{about}</span><span class="where"></span><span class="time mono num">{when}</span>')
    link = f'<a class="item" href="{href}" data-nav>{cells}</a>' if ctx["live"] else f'<div class="item">{cells}</div>'
    return _row(f"need:{item['id']}", link + act, attrs=' data-where="" data-tone="warn"')


def _job_list(jobs, stop_rows, ctx):
    """Today's jobs, grouped by what you do next: Needs you, Live, Up next, Ended today, Earlier."""
    groups = {"needs": [], "live": [], "next": [], "ended": [], "earlier": []}
    for job in jobs:
        if job.group != "needs":
            groups[job.group].append(job.item())
    for entry in ctx["needs"]:
        groups["needs"].append(entry["job"].item(len(entry["runs"])) if entry["job"] else stop_rows[entry["key"]] if entry["stop"]
                               else _item_row(ctx, entry["item"]))
    out = []
    for key, heading in (("needs", "Needs you"), ("live", "Live"), ("next", "Up next"), ("ended", "Ended today"),
                         ("earlier", "Earlier")):
        items = groups[key]
        extra = f'<span class="n num">{len(items)}</span>' if items else ""
        out.append(_section(f"g-{key}", heading, _rows(items), extra, "group", hidden=not items, h="h3"))
    none = not jobs and not stop_rows
    empty = (f'<p class="empty no-jobs" data-region="no-jobs" data-sig="{_sig(none)}"{"" if none else " hidden"}>'
             '<b>No jobs yet.</b>A job you start, or one added with rh job add, shows up here.</p>')
    return empty + "".join(out)


def _seat_state(name, jobs):
    """What a seat is doing, from the jobs assigned to it: (tone, word, the job it is on, how many it runs)."""
    mine = [j for j in jobs if j.seat == name]
    waiting = [j for j in mine if j.group == "needs"]
    working = [j for j in mine if j.group == "live"]
    if waiting:
        return "amber", "waiting on you", waiting[0], len(working)
    if working:
        return "accent", "working", working[0], len(working)
    return "faint", "idle", None, 0


def _creatures(jobs, view):
    out = []
    for k, seat in enumerate(s for s in view.get("seats", []) if isinstance(s, dict) and s.get("name")):
        tone, _, _, busy = _seat_state(str(seat["name"]), jobs)
        out.append(_creature(str(seat["name"]), tone, k, busy))
    return out


def _composer(ctx, draft, ready, first):
    """The one control that starts a job: what it should do, where it runs, its name, and the rest folded away."""
    draft = draft if isinstance(draft, dict) else {}
    value = lambda name: _text(draft.get(name, ""))  # noqa: E731
    kind = draft.get("kind") or ("codex-cloud" if ready else "local-shell")
    picked = {k: " checked" if kind == k else "" for k in ("local-shell", "codex-cloud")}
    have = set(draft.get("needs", [])) if isinstance(draft.get("needs"), list) else set()
    needs = "".join(f'<label><input type="checkbox" name="needs" value="{n}"{" checked" if n in have else ""}>{n}</label>'
                    for n in _NEEDS)
    timeout = value("timeout_min") or "120"
    later = "" if ready else "<small>set up first</small>"
    form = (f'<form class="compose composer" method="post" action="/job" data-act data-label="Start job">'
            f'{_hidden(ctx, [])}<label class="sr" for="brief">What should the job do?</label>'
            '<textarea class="c-text" id="brief" name="brief" rows="1" required placeholder="Describe a job: what to '
            f'find, write and check">{value("brief")}</textarea>'
            '<div class="cmd"><label for="command">Command</label><input type="text" id="command" name="command" '
            f'class="mono" autocomplete="off" spellcheck="false" placeholder="sh -c \'date &gt; today.txt\'" '
            f'value="{value("command")}"><small>Runs in runs/&lt;name&gt;/, so write plain file names. Pipes and &gt; '
            'need sh -c, as shown.</small></div>'
            '<div class="c-row"><fieldset class="seg where"><legend class="sr">Runs on</legend>'
            f'<label><input type="radio" name="kind" value="local-shell"{picked["local-shell"]}>{_ic("mac", True)}'
            f'{HERE}</label>'
            f'<label><input type="radio" name="kind" value="codex-cloud"{picked["codex-cloud"]}>{_ic("cloud", True)}'
            f'Codex cloud{later}</label></fieldset>'
            '<label class="name"><span class="mono">name</span><input type="text" name="id" required autocomplete="off" '
            'spellcheck="false" class="mono" pattern="[a-z0-9][a-z0-9._\\-]{0,62}" placeholder="todo-scan" '
            f'aria-label="Job name" value="{value("id")}"></label>'
            '<details class="opts"><summary class="chip">Options</summary><div class="opts-body">'
            '<label><span>Done when <span class="soft">· a command that exits 0 when the job worked</span></span>'
            f'<input type="text" name="accept" autocomplete="off" spellcheck="false" class="mono" '
            f'placeholder="test -s todo.md" value="{value("accept")}"></label>'
            f'<fieldset><legend>It needs</legend><div class="needs">{needs}</div></fieldset>'
            '<label>Time limit, minutes<input type="text" name="timeout_min" inputmode="numeric" '
            f'pattern="[0-9]{{1,5}}" value="{timeout}"></label></div></details>'
            '<span class="c-hint mono">⌘↵ start</span>'
            f'<button class="btn primary send" aria-label="Start job">{_ic("up")}</button></div></form>')
    if first:
        form += ('<div class="how"><ol class="steps">'
                 f'<li><div><p><b>Describe a job and start it.</b> A job on {HERE} runs one command in its own '
                 'folder.</p></div></li>'
                 '<li><div><p><b>Watch it here.</b> Its page shows the output as it runs and the files it made when '
                 'it is done.</p></div></li>'
                 '<li><div><p><b>Answer when it asks.</b> A question or a step only you can take shows up under '
                 'Needs you and in the Inbox.</p></div></li></ol></div>')
    body = f'<div class="body">{form}</div>'
    return (f'<div class="s-compose" data-region="compose" data-key="section:compose" '
            f'data-sig="{_sig("compose", body)}">{body}</div>')


def _tally(ctx, jobs):
    """The top bar's count: what needs you (the way to the Inbox) and what is live."""
    live, wait = sum(j.group == "live" for j in jobs), len(ctx["needs"])
    waits = (f'<a href="{_url(ctx, "/inbox")}" data-nav data-need="{wait}">{_need_word(wait)}</a>' if wait
             else _need_word(0))
    pulse = (f'{_dot("warn" if wait else "neutral")}{waits} <span class="sep">·</span> <span class="num">{live}</span> '
             f'{"job" if live == 1 else "jobs"} live')
    return f'<p class="tally" data-region="tally" data-still data-sig="{_sig(pulse)}">{pulse}</p>'


def _compose_strip(ctx, view, first):
    """Starting a job: the composer as one line over the list, opening up as you type."""
    return ('<section class="c-strip" aria-labelledby="compose-title"><h1 class="sr" id="compose-title" tabindex="-1">'
            'What should run?</h1>' + _composer(ctx, view.get("compose"), ctx["ready"], first) + '</section>')


def _reef(ctx, view, jobs):
    """The sea beside the list: a jellyfish for each seat, named under it, and the rest of the sea life, each a count
    the page already shows (see _fauna). A screen reader gets the seats as a list, with what each does."""
    said = []
    for seat in (s for s in view.get("seats", []) if isinstance(s, dict) and s.get("name")):
        _, word, job, _ = _seat_state(str(seat["name"]), jobs)
        role = _sentence(_LABEL.sub("", str(seat.get("role", ""))))
        said.append(f'<li>{_text(seat["name"])}, {word}{" on " + _text(job.id) if job else ""}: {_text(role)}</li>')
    seats = (f'<h2 class="sr">Seats</h2><ul class="sr">{"".join(said)}</ul>' if said
             else '<p class="sr">This repository has no seats.toml, so no seat is drawn in the sea.</p>')
    return ('<aside class="reef" aria-label="Who is working">'
            + _tank(_creatures(jobs, view), "water", _fauna(jobs, view, ctx)) + _region("seats", seats) + '</aside>')


def _snapshot_rail(jobs, loose, view, ctx):
    """The snapshot's right side: what needs a person, each with the command that settles it, then the day. `loose`
    maps each entry of the needs list that is not a job to its card."""
    rows = []
    for entry in ctx["needs"]:
        j = entry["job"]
        if j is None:
            rows.append(loose[entry["key"]])
            continue
        if j.items:
            item = j.items[0]
            ask, detail = _ask_line(item)
            when = (_when(item["created"], ctx["now"], f"inbox:{item['id']}", "asked ")
                    if _number(item.get("created")) else "")
            cli = {"web-launch": f"rh cloud attach {j.id} <task id>",
                   "harvest": f"rh cloud harvest {j.id} <saved file>"}.get(item.get("kind"),
                                                                           f"rh inbox resolve {item['id']} <note>")
            more = len(j.items) - 1
            if more:
                detail = (detail + " " if detail else "") + f"And {more} more on its page."
        elif j.handoff:
            ask, detail, when, cli = "Launch it in Codex, then link the task", "", "", f"rh cloud attach {j.id} <task id>"
        elif j.mine:
            verb, detail, when, _ = _resume(ctx, j.mine[0], False, "this job", [("job", j.id)])
            ask, cli = f"{verb} from the app: {_clause(j.mine[0]['reason'])}", f"rh resume --owner app --job {j.id}"
        elif j.failure:
            ask = f"{j.word}: {_plain(j.row.get('detail'))}" if j.row.get("detail") else j.word
            detail, when, cli = "", "", f"rh job show {j.id}"
        else:
            ask, detail, when, cli = f"On hold: {_plain(j.row.get('detail'))}", _NEXT["held"], "", f"rh job show {j.id}"
        holds = "".join(_hold(s, j.where == "live") for s in j.stops if s.get("owner") != "app")
        status = (" · ".join(x for x in ("Paused" if j.row.get("paused") else "", j.word, _plain(j.row.get("detail")))
                             if x) if j.items else "")
        rows.append(_need_row(ctx, f"card:{j.id}", _text(ask), _text(detail) + holds, when, cli, j.id, j.title, status))
    parts = []
    if rows:
        parts.append(_section("needs", "Needs you", _rows(rows, "rows cards"), f'<span class="n num">{len(rows)}</span>'))
    else:
        sup = view.get("supervisor")
        checked = (f'The harness checked {_when(ctx["now"] - sup["age_s"], ctx["now"], "tick:empty")}.'
                   if sup and _number(sup.get("age_s")) else "The harness has not run yet." if not sup else "")
        parts.append(_section("needs", "Needs you", f'<p class="soft">Nothing needs you. {checked}</p>'))
    day = ", ".join(f"{_text(n)} {_text(_STATES[s][0].lower() if s in _STATES else s)}"
                    for s, n in sorted(view.get("last_24h", {}).items()))
    counts = {g: sum(1 for j in jobs if j.group == g) for g in ("live", "next")}
    parts.append(_section("today", "Today", f'<p>{counts["live"]} live · {counts["next"]} up next · '
                                            f'{len(view.get("recent", []))} recently finished</p>'
                                            f'<p class="soft">Last 24 h: {day or "nothing finished"}</p>'))
    return f'<aside class="rail" aria-label="What needs you">{"".join(parts)}</aside>'


def _home(jobs, stop_rows, loose_cards, view, ctx, feedback):
    """Today: what needs you first, then the rest of the jobs, under the composer as one line; on the right, the sea,
    where each seat is a jellyfish. A printed snapshot has neither, and lists on the right what needs a person, each
    with the command that settles it."""
    when = (f'<h1 tabindex="-1">Today</h1><span class="top-meta mono"><span class="when" data-clock>{_date(ctx["now"])}'
            '</span></span>')
    listed = f'<div class="list">{_job_list(jobs, stop_rows, ctx)}</div>'
    if not ctx["live"]:
        jobs_head = '<div class="jobs-head"><h2 id="jobs-h">Jobs</h2></div>'
        return (_top(ctx, when) + feedback + f'<div class="page mc"><div class="center"><section class="jobs" '
                f'aria-labelledby="jobs-h">{jobs_head}{listed}</section></div>'
                f'{_snapshot_rail(jobs, loose_cards, view, ctx)}</div>')
    strip = _compose_strip(ctx, view, not jobs) if ctx["can_add"] else ""
    return (_top(ctx, when, _tally(ctx, jobs)) + feedback + f'<div class="page reef-page"><div class="center">{strip}'
            f'<section class="jobs" aria-label="Jobs">{listed}</section></div>{_reef(ctx, view, jobs)}</div>')


def _event(ev, now, kind):
    """One line of what happened to the job, in plain words, or None for a line that says nothing new."""
    what, data = str(ev.get("kind", "")), ev.get("data") if isinstance(ev.get("data"), dict) else {}
    tone = "neutral"
    if what == "state":
        to, detail = data.get("to"), _plain(data.get("detail"))
        word, tone = _STATES.get(to, (to, "neutral"))
        if to == "draft" or (to == "launching" and not detail):
            return None
        if to == "running":
            text = detail or f"Started {_PLACES.get(kind, '')}".strip()
        elif to == "ready":
            if not detail:
                return None
            text = detail if detail == _STATES["env"][0] else f"Ready: {detail}"
        elif to == "handoff":
            return None
        elif to == "collecting":
            text = detail or "Checking the result"
        elif to in _STATES and to not in _PRE and to not in _LIVE:
            text = str(word)
        else:
            text = f"{word}: {detail}" if detail else str(word)
        tone = "neutral" if tone == "live" and to != "running" else tone
    elif what == "job-added":
        text = f"Added to run {_PLACES.get(data.get('kind', ''), 'on ' + _platform(data.get('kind', '')))}"
    elif what == "inbox":
        text, tone = f"Asked you: {_sentence(_ask_line({**data, 'job': ev.get('job')})[0])}", "warn"
    elif what == "harvested":
        text = f"Patch saved from the {data.get('source', 'web view')}"
    elif what == "landed":
        text, tone = f"Saved to branch {data.get('branch', '')}", "good"
    elif what == "job-error":
        text, tone = f"Harness error: {data.get('error', '')}", "bad"
    elif what in ("prompt", "notify"):
        return None
    else:
        extra = ", ".join(f"{k} {v}" for k, v in sorted(data.items()) if isinstance(v, (str, int, float)))[:200]
        text = _sentence(what.replace("-", " ")) + (f": {extra}" if extra else "")
    when = f'<span class="when-c mono">{_clock(ev["ts"], now)}</span>' if _number(ev.get("ts")) else "<span></span>"
    inner = f'{_dot(tone)}<p>{_text(text)}</p>{when}'
    return text, _row(f"ev:{ev.get('seq', '')}", inner, attrs=f' data-tone="{tone}"')


_TEST = re.compile(r"""\s*(?:test|\[)\s+-([sfdex])\s+(?:'([^']+)'|"([^"]+)"|([^\s'"\]]+))\s*\]?\s*""")
_LACKS = {"s": "is missing or empty", "f": "is missing or not a file", "d": "is missing or not a folder",
          "e": "is missing", "x": "is missing or not executable"}


def _found(cmd, code):
    """What a failed check found, in words: a file test (test -s notes.md) names the file and what it lacked, and
    anything else its exit code."""
    m = _TEST.fullmatch(str(cmd))
    if m and str(code) == "1":
        return f"{m.group(2) or m.group(3) or m.group(4)} {_LACKS[m.group(1)]}"
    return f"it exited with code {code}"


def _checks_of(opened, spec, finished):
    """The receipt's checks: (passed, command, exit) as recorded, or what will run while the job is not finished."""
    receipt = opened.get("receipt") if isinstance(opened.get("receipt"), dict) else {}
    checks = [c for c in receipt.get("checks", []) if isinstance(c, dict)] if isinstance(receipt.get("checks"),
                                                                                          list) else []
    argv = spec.get("command")
    joined = shlex.join(argv) if isinstance(argv, list) and all(isinstance(a, str) for a in argv) else None
    if checks:
        # The harness records the command space-joined, which loses its quotes; show what can be pasted and run.
        return [(c.get("exit") == 0, joined if joined and c.get("cmd") == " ".join(argv) else str(c.get("cmd", "")),
                 c.get("exit")) for c in checks]
    if finished:
        return []
    pending = ([joined] if joined and spec.get("kind") == "local-shell" else []) + (
        [str(spec["accept"])] if spec.get("accept") else [])
    return [(None, cmd, None) for cmd in pending]


def _receipt(ctx, opened, job, spec, finished, lead):
    """The right side's receipt: the checks with their exit codes, the files the job made, its stamp, and Save to
    branch for a finished cloud job."""
    jid, state = job["id"], job.get("state")
    receipt = opened.get("receipt") if isinstance(opened.get("receipt"), dict) else {}
    parts = []
    checks = _checks_of(opened, spec, finished)
    if checks:
        lines = []
        for passed, cmd, code in checks:
            mark = ('<i class="pend" aria-label="not run yet"></i>' if passed is None else
                    f'<span class="{"ok" if passed else "bad"}" aria-hidden="true">{"✓" if passed else "✕"}</span>')
            exit_ = "—" if passed is None else f"exit {_text(code)}"
            lines.append(f'<li class="chk">{mark}<code>{_text(cmd)}</code><span class="exit">{exit_}</span></li>')
        parts.append(f'<div class="sub-h">Checks <span class="mono">exit code</span></div><ul>{"".join(lines)}</ul>')
    files = opened.get("files") if isinstance(opened.get("files"), list) else []
    where = str(spec.get("write_root") or "")
    if files:
        rows = []
        for f in files:
            path = str(f.get("path", ""))
            name = (f'<a href="{_text("/job/" + jid + "/file?p=" + quote(path) + "&t=" + ctx["token"])}" '
                    f'target="_blank" rel="noopener">{_text(path)}</a>' if ctx["live"] else _text(path))
            rows.append(f'<li class="fl">{_ic("file", True)}<span class="mono">{name}</span>'
                        f'<span class="size mono">{_text(_size(f.get("size")))}</span></li>')
        parts.append(f'<div class="sub-h"><span>Files in {_m(where)}</span></div><ul class="files">{"".join(rows)}</ul>')
    elif finished and state == "done" and spec.get("kind") == "local-shell":
        parts.append(f'<p class="soft">It left no files in {_m(where)}.</p>')
    if finished:
        stamp = [_platform(receipt.get("route") or job.get("kind") or spec.get("kind", ""))]
        if job.get("pinned_sha"):
            stamp.append(str(job["pinned_sha"])[:7])
        if receipt.get("model_served"):
            stamp.append(f"model {receipt['model_served']}")
        if receipt.get("diff_sha256"):
            stamp.append(f"diff {str(receipt['diff_sha256'])[:12]}")
        stamp.append(jid)
        parts.append(f'<p class="stamp mono">{" · ".join(_text(s) for s in stamp)}</p>')
    if ctx["live"] and state == "done" and spec.get("kind") == "codex-cloud":
        parts.append(_acts([_button(ctx, "/land", [("job", jid)], "Save to branch", "l", "primary" if lead else "",
                                    "Save to branch")]))
    if not parts:
        parts.append('<p class="soft">Its checks and the files it makes show here.</p>')
    body = f'<h2>{"Receipt" if finished else "Receipt, so far"}</h2>' + "".join(parts)
    return _region("j-receipt", body, "section")


def _props(ctx, job, spec, entry, view):
    """The right side's properties: what the job is, where and when it runs, and what it was asked to do."""
    now, kind = ctx["now"], job.get("kind") or spec.get("kind", "")
    shown = "env" if entry.native else job.get("state")
    word = _STATES.get(shown, (shown,))[0]
    rows = [("State", f'{_ring(shown)}<span>{_text(word)}</span>'),
            ("Runs on", f'{_ic("mac" if kind == "local-shell" else "cloud", True)}<span>{_text(_platform(kind))}</span>')]
    seat = str(spec.get("seat") or "")
    if seat:
        card = next((s for s in view.get("seats", []) if isinstance(s, dict) and s.get("name") == seat), {})
        rows.append(("Seat", f'<span class="mono">{_text(seat)}</span>'
                     + (f'<span class="mono dim">{_text(card["model"])}</span>' if card.get("model") else "")))
    for key, label in (("created", "Added"), ("started", "Started"), ("updated", "Updated")):
        if _number(job.get(key)) and not (key == "updated" and entry.where != "recent"):
            rows.append(("Finished" if key == "updated" else label,
                         f'<span class="mono num">{_clock(job[key], now)}</span>'))
    if spec.get("prior"):
        prior = _text(spec["prior"])
        rows.append(("Runs again after", f'<a class="mono" href="{_url(ctx, "/job/" + str(spec["prior"]))}" data-nav>'
                                         f'{prior}</a>' if ctx["live"] else f'<span class="mono">{prior}</span>'))
    if spec.get("changed") and spec.get("prior"):
        rows.append(("Changed", f"<span>{_text(spec['changed'])}</span>"))
    if spec.get("command"):
        rows.append(("Command", f'<code>{_text(shlex.join(map(str, spec["command"])))}</code>'))
    if spec.get("accept"):
        rows.append(("Done when", f'<code>{_text(spec["accept"])}</code>'))
    if spec.get("needs"):
        rows.append(("Needs", f'<span class="mono">{_text(", ".join(map(str, spec["needs"])))}</span>'))
    if spec.get("write_root"):
        rows.append(("Writes to", f'<code>{_text(spec["write_root"])}</code>'))
    if spec.get("timeout_min"):
        rows.append(("Time limit", _m(f"{spec['timeout_min']} min")))
    body = '<h2>Properties</h2>' + "".join(f'<div class="prop"><span class="pk">{k}</span><span class="pv">{v}</span>'
                                           '</div>' for k, v in rows)
    return _region("j-props", body, "section")


def _stops(ctx, entry):
    """Pauses and stops on this job: the app's own (its Resume is under Needs you) and anyone else's, which only they
    can lift. Nothing when there are none."""
    parts = []
    for stop in entry.mine:
        verb = "Paused" if stop.get("kind") == "pause" else "Stopped"
        when = (_when(stop["created"], ctx["now"], f"stop:{stop.get('id', stop['scope'])}:side", verb.lower() + " ")
                if _number(stop.get("created")) else "")
        parts.append(f'<div class="pause-row">{when}<span><b>{verb} from the app:</b> '
                     f'{_text(_clause(stop["reason"]))}.</span></div>')
    for stop in entry.stops:
        if stop.get("owner") != "app":
            parts.append(_hold(stop, entry.where == "live"))
    if entry.row.get("paused") and not any(s.get("kind") == "pause" for s in entry.stops):
        parts.append('<p class="hold"><b>Paused:</b> its platform or all launches are paused.</p>')
    if not parts:
        return ""
    return _region("j-stops", '<h2>Pauses and stops</h2>' + "".join(parts), "section")


def _job_head(ctx, opened, job, spec, entry, finished, creature):
    """The job's band: its id, title and state over its own patch of water, and how far it has got."""
    now, state, jid = ctx["now"], job.get("state"), job["id"]
    kind = job.get("kind") or spec.get("kind", "")
    receipt = opened.get("receipt") if isinstance(opened.get("receipt"), dict) else {}
    started = receipt.get("started") or job.get("started")
    ended = receipt.get("ended") or job.get("updated")
    shown = "env" if entry.native else state
    word, tone = _STATES.get(shown, (shown, "neutral"))
    line = [_ring(shown)]
    if finished:
        took = (f" in {_m(_duration(ended - started))}" if state == "done"
                else f" after {_m(_duration(ended - started))}") if _number(started) and _number(ended) else ""
        line.append(f"<b>{_text(word)}{took}.</b>")
        why = _plain(receipt.get("detail") or job.get("detail"))
        if why and why != word:
            line.append(f'<span class="sep">·</span><span>{_text(why)}.</span>')
        if _number(ended):
            line.append(f'<span class="sep">·</span>{_when(ended, now, "j:ended", "finished ")}')
    else:
        doing = f"Running {_PLACES[kind]}" if state == "running" and kind in _PLACES else word
        line.append(f"<b>{_text(doing)}</b>")
        if _number(job.get("started")):
            line.append(f'<span class="sep">·</span>{_when(job["started"], now, "j:started", "started ")}')
        elif _number(job.get("created")):
            line.append(f'<span class="sep">·</span>{_when(job["created"], now, "j:added", "added ")}')
    if entry.seat:
        line.append(f'<span class="sep">·</span><span>seat <span class="mono">{_text(entry.seat)}</span></span>')
    prog = []
    limit = spec.get("timeout_min")
    if _number(started) and _number(limit) and limit > 0:
        used = (ended if finished and _number(ended) else now) - started
        took = (f'<span class="num">{_text(_span(max(0, round(used))))}</span>' if finished
                else _when(started, now, "j:used", short=True))
        prog.append(f'<div class="pg wide"><span class="pg-k">Time used</span><b>{took}<em> / {_text(limit)} min</em></b>'
                    f'{_cells(-(-used * 20 // (limit * 60)), 20, "amber" if state == "stalled" else "muted")}</div>')
    log = opened.get("log")
    if isinstance(log, str) and log.strip() and not finished:  # once it ends, its output is folded below, with its count
        n = len(log.rstrip("\n").splitlines())
        # The page holds only the end of a long log (serve's last 200 lines or 64 KB), so a cut one is "last".
        cut = finished or opened.get("log_cut")
        prog.append(f'<div class="pg"><span class="pg-k">Output</span><b class="num">{n}</b>'
                    f'<span class="pg-s">{"last lines" if cut else "lines so far"}</span></div>')
    checks = _checks_of(opened, spec, finished) if finished or state not in _PRE else []
    if checks:
        passed = sum(1 for p, _, _ in checks if p)
        sub = "run when it ends" if any(p is None for p, _, _ in checks) else "passed"
        prog.append(f'<div class="pg"><span class="pg-k">Checks</span><b class="num">{passed}<em> / {len(checks)}</em>'
                    f'</b><span class="pg-s">{sub}</span></div>')
    files = opened.get("files") if isinstance(opened.get("files"), list) else []
    if files:
        prog.append(f'<div class="pg"><span class="pg-k">Files</span><b class="num">{len(files)}</b>'
                    f'<span class="pg-s">in {_m(spec["write_root"]) if spec.get("write_root") else "its folder"}'
                    '</span></div>')
    body = (_tank([creature], "j-water") + '<div class="jd-head-in">'
            f'<p class="jd-id mono" data-clear>{_text(jid)} <span class="sep">·</span> {_text(_platform(kind))}</p>'
            f'<h1 tabindex="-1" data-clear>{_text(_lead(spec)[0] or jid)}</h1>'
            f'<p class="jd-state" data-clear>{"".join(line)}</p>'
            + (f'<div class="jd-prog" data-clear>{"".join(prog)}</div>' if prog else "") + '</div>')
    return _region("j-head", body, "section", "jd-head")


def _job_pane(opened, jobs, ctx, feedback, view):
    """A job's page: the work on the left (what it was asked, what it needs from you, its output, and Run again), and
    its properties, receipt, pauses and stops, and timeline on the right."""
    now = ctx["now"]
    job = opened.get("job") if isinstance(opened.get("job"), dict) else {}
    jid = str(job.get("id") or opened.get("id") or "")
    crumbs = (f'<nav class="crumbs" aria-label="Breadcrumb"><a href="{_url(ctx, "/")}" data-nav>Today</a>'
              if ctx["live"] else '<nav class="crumbs" aria-label="Breadcrumb"><span>Today</span>')
    if not job:
        top = _top(ctx, crumbs + f'{_ic("chev", True)}<span class="mono">{_text(jid)}</span></nav>')
        body = (f'<div class="missing" data-region="j-missing" data-sig="{_sig(jid)}"><h1 tabindex="-1">No job named '
                f'{_text(jid)}</h1><p class="soft">It may have been a typo in the address. The jobs the harness knows '
                'are on Today.</p></div>')
        return "job:" + jid, top + feedback + f'<div class="page nw">{body}</div>', jid
    spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
    kind = job.get("kind") or spec.get("kind", "")
    entry = next((j for j in jobs if j.id == jid), None)
    if entry is None:
        row = {"id": jid, "state": job.get("state"), "paused": False, "detail": job.get("detail", ""),
               "external_id": job.get("external_id")}
        where = ("live" if job.get("state") in _LIVE else "not_started" if job.get("state") in _PRE else "recent")
        entry = _Job(row, where, ctx)
    state = job.get("state")
    finished = entry.where == "recent" or state in ("done", "cancelled") or state in _FAILED
    title = _title(spec, jid)
    group = {"needs": "Needs you", "live": "Live", "next": "Up next", "ended": "Ended today",
             "earlier": "Earlier"}[entry.group]
    acts = []
    if ctx["live"] and not finished and not entry.mine:
        acts.append(_pause(ctx, jid))
    if ctx["live"] and state in _PRE and not finished:
        acts.append(_cancel(ctx, jid))
    top = _top(ctx, crumbs + f'{_ic("chev", True)}<span>{group}</span>{_ic("chev", True)}'
                             f'<span class="mono">{_text(jid)}</span></nav>', "".join(acts))
    # Its seat's jellyfish: dim once the job has ended, whatever the ending, since nothing works on it any more.
    tone = "faint" if finished else {"needs": "amber", "live": "accent"}.get(entry.group, "faint")
    creature = _creature(entry.seat or _platform(kind), tone, 0, 1, (0.88, 0.62))
    main = [_job_head(ctx, opened, job, spec, entry, finished, creature), feedback]
    work = []
    failed = finished and state != "done" and state != "cancelled"
    # A failed job leads with the check that failed: what it ran and its exit code, before anything else.
    bad = next(((cmd, code) for passed, cmd, code in _checks_of(opened, spec, True) if passed is False), None)
    runs = next((e["runs"] for e in ctx["needs"] if e.get("job") and jid in [j.id for j in e["runs"]]), [])
    others = ""
    if len(runs) > 1:
        named = [f'<b class="mono">{_text(j.id)}</b>' if j.id == jid else
                 f'<a class="mono" href="{_url(ctx, "/job/" + j.id)}" data-nav>{_text(j.id)}</a>' for j in runs]
        others = f'<p class="fail-runs">Failed the same way {len(runs)} times: {" · ".join(named)}</p>'
    if failed and (bad or others):
        said_fail = ""
        if bad:
            what = "The done-when check" if spec.get("accept") and bad[0] == str(spec["accept"]) else "The command"
            said_fail = (f'<p class="fail-k">{_ring(state)}{what} failed: {_text(_found(bad[0], bad[1]))}</p>'
                         f'<p class="fail-c"><code>{_text(bad[0])}</code><span class="exit mono">exit '
                         f'{_text(bad[1])}</span></p>')
        work.append(_region("j-fail", said_fail + others, "section", "s-j-fail", ' aria-label="What failed"'))
    said = _lead(spec)[1]
    if said:
        work.append(_region("j-brief", f'<div class="bubble">{_text(_reflow(said))}</div>', "section", "s-j-brief",
                            ' aria-label="What it was asked"'))
    # What it needs from you, each with the form that settles it.
    cards, lead = [], True
    for item in entry.items:
        cards.append(_ask_card(ctx, item, opened, lead, view))
        lead = False
    if entry.handoff:
        cards.append(_launch_card(ctx, jid, opened, lead))
        lead = False
    if entry.setup and not any(_setup_item(i, ctx["ready"]) for i in entry.items):
        cards.append(_setup_card(ctx, view, lead))
        lead = False
    if entry.native:
        cards.append(_native_card(ctx, ctx["native"], jid, lead))
        lead = False
    if entry.mine:
        stop = entry.mine[0]
        verb, detail, when, act = _resume(ctx, stop, lead, "this job", [("job", jid)])
        cards.append(f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you · {when}</p>'
                     f'<h3>{verb} from the app: {_text(_clause(stop["reason"]))}</h3><p class="detail">{detail}</p>'
                     f'{_acts(act) if ctx["live"] else ""}</div>')
        lead = False
    if entry.held:
        cards.append(f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you</p>'
                     f'<h3>On hold: {_text(_plain(job.get("detail")))}</h3>'
                     f'<p class="detail">{_NEXT["held"]}</p></div>')
    if cards:
        work.append(_region("j-asks", "".join(cards), "section", "s-j-asks", ' aria-label="Needs you"'))
    if not finished:
        loop = view.get("checks") if isinstance(view.get("checks"), dict) else None
        doing = (f"Running {_PLACES[kind]}" if state == "running" and kind in _PLACES else entry.word)
        next_line = ("It starts once Codex cloud is set up." if entry.setup
                     else "" if entry.native else _NEXT.get(state, "").replace("{place}", _PLACES.get(kind, "on its platform")))
        if loop and loop.get("mode") == "paused":
            next_line = (next_line + " Automatic checks are paused: the app won't launch or collect it until you "
                         "resume them or press Check once.").strip()
        detail = _plain(job.get("detail")) if state not in ("held", "handoff") and not entry.native else ""
        now_body = (f'<p class="now">{_dot(entry.tone, state == "running")}<span class="what">{_text(doing)}</span>'
                    + (f'<span class="soft">{_text(detail)}</span>' if detail and detail != doing else "")
                    + (f'<span class="soft">{_text(next_line)}</span>' if next_line else "") + "</p>")
        # The head already gives the state, so this line is there only when it adds what comes next or why.
        if next_line or (detail and detail != doing):
            work.append(_region("j-now", now_body, "section", "s-j-now", ' aria-label="Now"'))
    # A running job shows its output as it comes. A finished one leads with what it made, or, when it failed, with
    # where it stopped and the box to fix it right under that; its output (the last 200 lines) is folded away below.
    log = opened.get("log")
    log = log if isinstance(log, str) and log.strip() else ""
    lines = log.rstrip("\n").splitlines()
    if log and not finished:
        tag = f'<span class="livetag">{_dot("live", True)}live</span>' if state == "running" else ""
        last = "last " if opened.get("log_cut") else ""
        head = f'{tag}<span class="lines mono">{last}{len(lines)} {"line" if len(lines) == 1 else "lines"}</span>'
        work.append('<div class="out">' + _section("j-output", "Output", f'<pre class="log" tabindex="0">{_text(log)}'
                                                   '</pre>', head) + '</div>')
    if failed and log:
        tail = "\n".join(lines[-_TAIL:])
        code = next((c for p, _, c in _checks_of(opened, spec, True) if p is False), None)
        body = (f'<div class="head"><h2>Where it stopped</h2><span class="lines mono">'
                f'{"exit " + _text(code) if code is not None else "last lines"}</span></div>'
                f'<pre class="log ended err" tabindex="0">{_text(tail)}</pre>')
        work.append(_region("j-error", body, "section", "out", ' aria-label="Where it stopped"'))
    if finished and ctx["live"]:
        again = state != "done" or spec.get("kind") != "codex-cloud"
        form = (f'<form class="reply composer" method="post" action="/again" data-act data-label="Run again">'
                f'{_hidden(ctx, [("job", jid)])}<label class="sr" for="changed">What should change?</label>'
                '<textarea class="c-text" id="changed" name="changed" rows="1" placeholder="Ask for a change, or leave '
                'this empty to run it again as it was"></textarea><div class="c-row"><span class="c-hint mono">⌘↵ run'
                f'</span><button class="{_cls("primary" if lead and again and state != "done" else "")}">Run again'
                '</button></div></form>')
        reply = _region("j-reply", form, "section", "jd-compose", ' aria-label="Run again"')
    else:
        reply = ""
    if failed:
        work.append(reply)
    peek = opened.get("peek") if isinstance(opened.get("peek"), dict) else None
    if finished and peek:
        more = '\n…' if peek.get("more") else ""
        body = (f'<div class="head"><h2>{_text(peek.get("path", ""))}</h2><span class="lines mono">what it made</span>'
                f'</div><pre class="peek" tabindex="0">{_text(peek.get("text", ""))}{more}</pre>')
        work.append(_region("j-peek", body, "section", "out", ' aria-label="A file it made"'))
    if finished and log and (not failed or len(lines) > _TAIL):
        body = (f'<details class="fold"><summary><span class="fold-t">Output</span><span class="lines mono">last '
                f'{len(lines)} {"line" if len(lines) == 1 else "lines"}</span></summary>'
                f'<pre class="log ended" tabindex="0">{_text(log)}</pre></details>')
        work.append(_region("j-output", body, "section", "out"))
    if not failed:
        work.append(reply)
    main.append(f'<div class="jd-body">{"".join(work)}</div>')
    lines, seen = [], None
    for ev in [e for e in opened.get("events", []) if isinstance(e, dict)][-60:]:
        made = _event(ev, now, kind)
        if made and made[0] != seen:
            seen = made[0]
            lines.append(made[1])
    # Before it starts, its checks are its Done when and Command in the properties, so there is no receipt yet.
    started = finished or state not in _PRE
    side = [_props(ctx, job, spec, entry, view),
            _receipt(ctx, opened, job, spec, finished, lead and finished) if started else "", _stops(ctx, entry)]
    if lines:
        side.append(_section("j-timeline", "Timeline", _rows(lines, "rows timeline")))
    page = (f'<div class="page jd"><div class="jd-main">{"".join(main)}</div>'
            f'<aside class="jd-side" aria-label="Job details">{"".join(side)}</aside></div>')
    return "job:" + jid, top + page, title


def _ib_row(ctx, entry, picked):
    """One thing that waits on you: an item opens here beside the list; a job with nothing to answer opens its page,
    and the app's own pause opens Today, where its Resume is."""
    job, jid = entry["job"], entry["jid"]
    href = (_url(ctx, f"/inbox/{entry['item']['id']}") if entry["item"] else _url(ctx, f"/job/{jid}") if job
            else _url(ctx, "/"))
    created = entry["created"]
    age = _when(created, ctx["now"], f"age:{entry['key']}", short=True) if _number(created) else ""
    head = job.title if job else entry["ask"]
    sub = (f'{_text(entry["ask"])} · <span class="mono">{_text(jid)}</span>' if job else
           f'<span class="mono">{_text(jid)}</span> · a job not listed here' if jid
           else "The app's own pause" if entry["stop"] else "The harness")
    current = ' aria-current="true"' if picked else ""
    # a job's entry wears the ring it wears on Today, so a state has one mark in both places
    mark = _ring(job.state, "red" if job.state in _FAILED else "amber") if job else _ic(entry["icon"])
    inner = (f'<span class="ib-ic">{mark}</span><span class="ib-t">{_text(head)}</span>'
             f'<span class="ib-age mono num">{age}</span><span class="ib-s">{sub}</span>')
    link = (f'<a class="ib" href="{href}" data-nav{current}>{inner}</a>' if ctx["live"]
            else f'<div class="ib">{inner}</div>')
    return _row(entry["key"], link, "ib-li", f' data-kind="{entry["tab"]}"')


def _settled_row(ctx, item, picked):
    ask, _ = _ask_line(item)
    jid = item.get("job")
    note = str(item.get("resolution") or "").strip()
    sub = " · ".join(x for x in ((f'<span class="mono">{_text(jid)}</span>' if jid else ""), _text(note)) if x)
    current = ' aria-current="true"' if picked else ""
    inner = (f'<span class="ib-ic">{_ic("check")}</span><span class="ib-t">{_text(ask)}</span>'
             f'<span class="ib-age mono num">{_clock(item["resolved"], ctx["now"])}</span>'
             f'<span class="ib-s">{sub or "Settled"}</span>')
    link = (f'<a class="ib done" href="{_url(ctx, "/inbox/" + str(item["id"]))}" data-nav{current}>{inner}</a>'
            if ctx["live"] else f'<div class="ib done">{inner}</div>')
    return _row(f"settled:{item['id']}", link, "ib-li settled")


def _ib_detail(ctx, entry, settled, view, missing):
    """The item the Inbox shows: who asks and about what, over a patch of water, then the form that settles it."""
    if missing is not None:
        body = (f'<div class="q-body"><h2 class="q-title" id="q-h">No inbox item {_text(missing)}</h2>'
                '<p class="soft">It may have been settled long ago, or the address has a typo.</p></div>')
        return _region("ib-detail", body, "section", attrs=' aria-labelledby="q-h"')
    if entry is None and settled is None:
        sup = view.get("supervisor")
        checked = (f' The harness checked {_when(ctx["now"] - sup["age_s"], ctx["now"], "tick:inbox")}.'
                   if isinstance(sup, dict) and _number(sup.get("age_s")) else "")
        n = len(ctx["needs"])
        if n:  # nothing to answer here, but jobs wait on you on their own pages
            body = (f'<div class="q-body"><h2 class="q-title" id="q-h">No questions to answer.</h2><p class="soft">'
                    f'{_need_word(n).capitalize()} on {"its" if n == 1 else "their"} own '
                    f'{"page" if n == 1 else "pages"}: open {"it" if n == 1 else "one"} from the list.</p></div>')
        else:
            body = (f'<div class="q-body"><h2 class="q-title" id="q-h">Nothing needs you.</h2><p class="soft">Questions, '
                    f'hand launches and patches to paste land here when a job needs you.{checked}</p></div>')
        return _region("ib-detail", body, "section", attrs=' aria-labelledby="q-h"')
    item = (entry.get("pick") or entry["item"]) if entry else settled
    jid = item.get("job")
    job = entry["job"] if entry else next((j for j in ctx["jobs"] if j.id == jid), None)
    ask, detail = _ask_line(item)
    who = [f'<span class="mono">{_text(jid) if jid else "harness"}</span>']
    if job:
        who.append(f'<span class="sep">·</span><span>{_text(_platform(job.kind))}</span>')
    if _number(item.get("created")):
        who.append(f'<span class="sep">·</span><span>asked at <span class="mono num">{_clock(item["created"], ctx["now"])}'
                   f'</span>' + (f', waiting {_when(item["created"], ctx["now"], "q:age", short=True)}' if entry else "")
                   + '</span>')
    about = ""
    if job:
        link = (f'<a href="{_url(ctx, "/job/" + job.id)}" data-nav>{_text(job.title)}</a>' if ctx["live"]
                else _text(job.title))
        about = (f'<p class="q-job" data-clear>{_dot("warn" if entry else "neutral")}<span>On “{link}”</span>'
                 f'<span class="sep">·</span><span>{_text(job.word)}</span></p>')
    tone = "amber" if entry and not (job and job.state in _FAILED) else "faint"
    # An item on a job is its seat's jellyfish; one from the harness itself is the octopus.
    water = (_tank([_creature(job.seat or "harness", tone, 1, 1, (0.9, 0.55))], "q-water") if job else
             _tank([], "q-water", {"octopus": {"tone": tone, "at": [0.92, 0.5]}}))
    head = (f'<div class="q-head">{water}<div class="q-head-in">'
            f'<p class="q-who" data-clear>{"".join(who)}</p><h2 class="q-title" id="q-h" data-clear>{_text(ask)}</h2>'
            f'{about}</div></div>')
    parts = [f'<div class="q-msg">{_text(detail)}</div>'] if detail else []
    if entry is None:
        note = str(item.get("resolution") or "").strip()
        parts.append(f'<div class="settled-note"><p>Settled at <span class="mono">{_clock(item["resolved"], ctx["now"])}'
                     f'</span>.</p>' + (f'<p>Your note: <b>{_text(note)}</b></p>' if note else "") + '</div>')
    elif ctx["live"]:
        kind = item.get("kind")
        if kind == "web-launch" and jid:
            opened = {"prompt": view.get("prompt"), "job": {"detail": job.row.get("detail") if job else ""}}
            parts.append(_launch_card(ctx, jid, opened, True, f'<div class="foot">{_dismiss(ctx, item["id"])}</div>'))
        elif _setup_item(item, ctx["ready"]):
            parts.append(_setup_card(ctx, view, True))
        elif _native_item(item):
            parts.append(_native_card(ctx, item))
        else:
            parts.append(_ask_form(ctx, item, True))
    else:
        parts.append(f'<p class="cli"><code>rh inbox resolve {_text(item["id"])} &lt;note&gt;</code></p>')
    others = [i for i in (job.items if entry and job else []) if i is not item]
    if others and ctx["live"]:
        links = ", ".join(f'<a href="{_url(ctx, "/inbox/" + str(i["id"]))}" data-nav>{_text(_ask_line(i)[0])}</a>'
                          for i in others)
        parts.append(f'<p class="q-more soft">This job also asks: {links}.</p>')
    body = head + f'<div class="q-body">{"".join(parts)}</div>'
    return _region("ib-detail", body, "section", attrs=' aria-labelledby="q-h"')


def _inbox_pane(view, ctx, feedback):
    """The Inbox: what waits on you, newest first, and what was settled today, beside the item it shows."""
    waiting = sorted(ctx["needs"], key=lambda e: (-(e["created"] if _number(e["created"]) else 0),
                                                   -(e["item"]["id"] if e["item"] and _number(e["item"].get("id"))
                                                     else 0)))
    midnight = ctx["midnight"]
    settled = sorted((i for i in view.get("settled", []) if isinstance(i, dict) and _number(i.get("resolved"))
                      and i["resolved"] >= midnight), key=lambda i: -i["resolved"])
    pick = view.get("pick")
    entry = settled_item = missing = None
    if pick is None:
        entry = next((e for e in waiting if e["item"]), None)
    else:
        for e in waiting:
            hit = next((i for i in (e["job"].items if e["job"] else [e["item"]] if e["item"] else [])
                        if i["id"] == pick), None)
            if hit:
                entry = {**e, "pick": hit}
                break
        else:  # an open item the list shows through its job's row, such as the Codex setup step on a waiting job
            hit = next((i for i in view.get("inbox", []) if i.get("id") == pick), None)
            if hit:
                entry = {"key": f"inbox:{pick}", "job": None, "item": hit, "stop": None}
        if entry is None:
            picked = view.get("picked") if isinstance(view.get("picked"), dict) else None
            settled_item = next((i for i in settled if i["id"] == pick), None) or (
                picked if picked and _number(picked.get("resolved")) else None)
            if settled_item is None:
                missing = pick
    rows = [_ib_row(ctx, e, entry is not None and e["key"] == entry["key"]) for e in waiting]
    done = [_settled_row(ctx, i, settled_item is not None and i["id"] == settled_item["id"]) for i in settled]
    counts = {}
    for e in waiting:
        counts[e["tab"]] = counts.get(e["tab"], 0) + 1
    tabs = [f'<button type="button" role="tab" aria-selected="true" data-kind="">All <span class="num">'
            f'{len(waiting)}</span></button>']
    for tab, word in (("question", "Questions"), ("launch", "Launches"), ("patch", "Patches"), ("failed", "Failed"),
                      ("other", "Other")):
        if counts.get(tab):
            tabs.append(f'<button type="button" role="tab" aria-selected="false" data-kind="{tab}">{word} '
                        f'<span class="num">{counts[tab]}</span></button>')
    tabbar = (f'<div class="tabs" role="tablist" aria-label="Filter">{"".join(tabs)}</div>' if ctx["live"] else "")
    top = _top(ctx, '<h1 tabindex="-1">Inbox</h1>'
                    f'<span class="top-meta mono"><span data-need="{len(waiting)}">{_need_word(len(waiting))}</span> · '
                    f'<span class="num">{len(settled)}</span> settled today</span>')
    lists = (_section("ib-wait", "Needs you", _rows(rows) if rows else '<p class="empty">Nothing needs you.</p>',
                      f'<span class="n num">{len(waiting)}</span><span class="ib-gh-r mono">newest first</span>{tabbar}')
             + _section("ib-settled", "Settled today", _rows(done), f'<span class="n num">{len(settled)}</span>',
                        hidden=not done))
    ctx["back"] = "/inbox"
    detail = _ib_detail(ctx, entry, settled_item, view, missing)
    page = (f'<div class="page ibx"><div class="ib-list" aria-label="Waiting on you">{lists}</div>{detail}</div>')
    return top + feedback + page


def _palette(jobs, ctx, keys):
    """⌘K: every job, the pages, and what this page can do, filtered as you type."""
    order = {"needs": 0, "live": 1, "next": 2, "ended": 3, "earlier": 4}
    rows = ['<p class="p-sec">Jobs</p>'] if jobs else []
    for j in sorted(jobs, key=lambda j: order[j.group]):
        rows.append(f'<a class="p-row" role="option" href="{_url(ctx, "/job/" + j.id)}" data-nav>'
                    f'<span class="p-ic">{_ring(j.state)}</span><span class="p-t">{_text(j.title)}</span>'
                    f'<span class="p-s"><span class="mono">{_text(j.id)}</span><span>{_text(j.word.lower())}</span></span>'
                    f'<span class="p-a">{_where(j.kind)}</span></a>')
    pages = [("today", "Today", "the jobs and who is working", "/"), ("inbox", "Inbox", "what waits on you", "/inbox")]
    if ctx["can_add"]:
        pages.append(("new", "New job", "describe it, pick where it runs", "/new"))
    pages.append(("step", "Every action as a plain form", "for when the script is off", "/actions"))
    rows.append('<p class="p-sec">Go to</p>')
    rows += [f'<a class="p-row" role="option" href="{_url(ctx, path)}"{"" if path == "/actions" else " data-nav"}>'
             f'<span class="p-ic">{_ic(icon)}</span><span class="p-t">{word}</span><span class="p-s">{sub}</span>'
             '<span class="p-a"></span></a>' for icon, word, sub, path in pages]
    acts = [(k, word, sub) for k, word, sub in _PRESS if k in keys]
    if acts:
        rows.append('<p class="p-sec">On this page</p>')
        rows += [f'<button type="button" class="p-row" role="option" data-press="{k}"><span class="p-ic">'
                 f'{_ic("chev")}</span><span class="p-t">{word}</span><span class="p-s">{sub}</span>'
                 f'<span class="p-a"><kbd>{k}</kbd></span></button>' for k, word, sub in acts]
    body = (f'<div class="p-in">{_ic("search")}<input class="p-q" type="text" aria-label="Jump to a job, a page or an '
            'action" placeholder="Jump to a job, a page or an action" autocomplete="off" spellcheck="false">'
            '<kbd>esc</kbd></div>'
            f'<div class="p-list" role="listbox" aria-label="Results">{"".join(rows)}'
            '<p class="p-none" hidden>Nothing matches.</p></div>'
            f'<div class="p-foot">{_mark(16)}<span>research-harness</span><span class="p-f-r"><span>Open</span>'
            '<kbd>↵</kbd><span class="bar"></span><span>Close</span><kbd>esc</kbd></span></div>')
    return (f'<dialog class="pal" id="pal" aria-label="Jump to anything" data-region="pal" '
            f'data-sig="{_sig(_still(body))}">{body}</dialog>')


def _sidebar(ctx, view, page, wait):
    live = ctx["live"]
    if live:
        new = (f'<a class="icon-btn" href="{_url(ctx, "/new")}" data-nav data-k="n" aria-label="New job, N">{_ic("new")}</a>'
               if ctx["can_add"] else "")
        ws = (f'<a class="ws-btn" href="{_url(ctx, "/")}" data-nav>{_mark()}<span>research-harness</span></a>'
              f'<button type="button" class="icon-btn" data-pal aria-label="Jump to anything, ⌘K">{_ic("search")}'
              f'</button>{new}')
        links = [("today", "Today", "/", page == "home"), ("inbox", "Inbox", "/inbox", page.startswith("inbox"))]
        if ctx["can_add"]:
            links.append(("new", "New job", "/new", page == "new"))
        current = ' aria-current="page"'
        nav = "".join(f'<a href="{_url(ctx, path)}" data-nav{current if here else ""}>{_ic(icon)}<span>{word}</span>'
                      + (f'<b class="count num" data-need="{wait}" aria-label="{_need_word(wait)}">{wait}</b>'
                         if icon == "inbox" and wait else "") + '</a>' for icon, word, path, here in links)
        nav = f'<nav class="nav" aria-label="Main" data-region="nav" data-sig="{_sig(nav)}">{nav}</nav>'
        tools = ('<div class="tools"><button type="button" class="btn quiet small" data-keys hidden>Shortcuts '
                 '<kbd>?</kbd></button><span class="conn" data-live data-state="on" role="status"></span></div>')
    else:
        ws = f'<p class="ws-btn">{_mark()}<span>research-harness</span></p>'
        nav = ""
        tools = '<div class="tools"><span class="soft">Snapshot</span></div>'
    top = f'<div class="ws" data-region="side-top" data-sig="{_sig(ws)}">{ws}</div>'
    foot = (f'<div class="side-foot">{_host(view, ctx)}<footer class="checks"><h2 class="side-h">Check loop</h2>'
            f'{_checks(view, ctx)}{tools}</footer></div>')
    return f'<aside class="side" aria-label="research-harness">{top}{nav}{foot}</aside>'


def render(view: dict, now: float, token: str | None = None, result=None) -> str:
    """Return one self-contained HTML5 page, escaping all supplied text. Without a token it is the static snapshot:
    no script and no controls. With one it is the live app, with forms and <script src="/app.js">. The view may also
    carry "open" (the job `rh job show` returned, plus "log", its output tail, "log_cut" when that tail is not the
    whole log, "prompt" for a hand launch, and "files" and "peek" for a finished job's folder; or {"id", "job": None}
    for a job the harness does not know),
    "inbox" plus "pick" (the Inbox, and the item it shows; "picked" is that item from the ledger, open or settled,
    and "prompt" its hand-launch prompt), "settled" (inbox items resolved lately), "can_add" (the server takes
    POST /job), "new" (the composer on its own page), "compose" (a draft to refill it with), "meta" (each job's brief,
    kind, seat and times), "seats" (the cards in seats.toml), "host" (this machine's load, memory pressure and heavy
    lease), "checks" (the app's check loop: mode, pid, failed), "cloud_ready" and "env_spec" (for Codex cloud setup).
    `result` is serve's {"code", "say", "output"}."""
    live = token is not None
    opened = view.get("open") if isinstance(view.get("open"), dict) else None
    can_add = live and bool(view.get("can_add"))
    box = live and bool(view.get("box")) and not opened
    compose = can_add and bool(view.get("new")) and not opened and not box
    scopes, inbox = {}, view.get("inbox", [])
    for stop in view.get("stops", []):
        scopes.setdefault(str(stop["scope"]), []).append(stop)
    rows = [(row, where) for where in ("live", "not_started", "recent") for row in view.get(where, [])]
    known = {row["id"] for row, _ in rows}
    meta = view.get("meta") if isinstance(view.get("meta"), dict) else {}
    ready = bool(view.get("cloud_ready"))
    asks, setup, native = {}, None, None
    for item in inbox:
        if _setup_item(item, ready):
            setup = setup or item
        elif _native_item(item):
            native = native or item
        elif item.get("job") in known:
            asks.setdefault(item["job"], []).append(item)
    # Every form on a job's page, or on the New job page, returns there; on the Inbox, to the Inbox; elsewhere home.
    named = opened["job"]["id"] if opened and isinstance(opened.get("job"), dict) else None
    back = f"/job/{named}" if named else "/new" if compose else None
    ctx = {"token": token, "live": live, "now": now, "scopes": scopes, "asks": asks, "can_add": can_add,
           "back": back, "meta": meta, "ready": ready, "setup": setup, "native": native, "midnight": _midnight(now),
           "followed": {str(m.get("prior")) for m in meta.values() if isinstance(m, dict) and m.get("prior")},
           "rerun": _reruns(meta),
           "others": [s for s in view.get("stops", []) if s.get("owner") != "app"
                      and not str(s["scope"]).startswith("job:")]}
    jobs = [_Job(row, where, ctx) for row, where in rows]
    ctx["jobs"] = jobs
    loose_stops = [s for s in view.get("stops", []) if s.get("owner") == "app"
                   and not (str(s["scope"]).startswith("job:") and str(s["scope"])[4:] in known)]
    ctx["needs"] = _needs(jobs, inbox, loose_stops, ctx)
    # The app's own pauses on a platform, on every launch, or on a job no longer listed: rows under Needs you with
    # Resume. The snapshot prints every loose need, inbox items included, as a card with its command.
    stop_rows, loose = {}, {}
    for stop in loose_stops:
        scope = str(stop["scope"])
        name, _, value = scope.partition(":")
        if scope == "*":
            what, fields = "any job", []
        elif name == "route":
            what, fields = _platform(value), [("route", value)]
        else:
            what, fields = value or name, [(name, value)] if value else []
        verb, detail, when, acts = _resume(ctx, stop, False, _text(what), fields)
        created = stop.get("created") if _number(stop.get("created")) else float("inf")
        cli = f"rh resume --owner app --{name} {value}".strip()
        key = f"stop:{stop.get('id', scope)}"
        loose[key] = _need_row(ctx, "card:" + key, f"{verb} from the app: {_text(_clause(stop['reason']))}", detail,
                               when, cli)
        cells = (f'{_ring("held", "amber")}<span class="name">{verb} from the app: '
                 f'{_text(_clause(stop["reason"]))}</span><span class="sub">{detail}</span><span class="where"></span>'
                 f'<span class="time mono num">{_clock(stop["created"], now) if _number(stop.get("created")) else ""}'
                 '</span>')
        act = _button(ctx, "/resume", [*fields, *([("pause", "on")] if stop.get("kind") == "pause" else [])],
                      f'{_ic("resume", True)}Resume', "u", "gbtn", f"Resume {_text(what)}") if live else ""
        stop_rows[key] = _row(key, f'<div class="item">{cells}</div>' + (f'<span class="do">{act}</span>' if act else ""),
                              attrs=' data-where="" data-tone="warn"')
    for entry in ctx["needs"]:
        item = entry["item"]
        if entry["job"] or item is None:
            continue
        when = (_when(item["created"], now, f"inbox:{item['id']}", "asked ") if _number(item.get("created")) else "")
        if _setup_item(item, ready):
            loose[entry["key"]] = _need_row(ctx, "card:" + entry["key"], "Set up Codex cloud",
                                            "Cloud jobs wait until Codex has an environment for this repository.",
                                            when, "rh env spec, then rh config set cloud.env_id <ID>")
        else:
            ask, detail = _ask_line(item)
            about = f" For job {_text(item['job'])}." if item.get("job") else ""
            loose[entry["key"]] = _need_row(ctx, "card:" + entry["key"], _text(ask), _text(detail) + about, when,
                                            f"rh inbox resolve {item['id']} <note>")
    feedback = _result(result) if live and result else ""
    if opened:
        page, pane, name = _job_pane(opened, jobs, ctx, feedback, view)
        title = f"{name} · Research harness"
    elif box:
        page, title = "inbox", "Inbox · Research harness"
        pane = _inbox_pane(view, ctx, feedback)
    elif compose:
        page, title = "new", "New job · Research harness"
        top = _top(ctx, '<h1 tabindex="-1">New job</h1>')
        pane = (top + feedback + '<div class="page reef-page new-page"><div class="center">'
                + _compose_strip(ctx, view, not jobs) + f'</div>{_reef(ctx, view, jobs)}</div>')
    else:
        page, title = "home", "Research harness"
        pane = _home(jobs, stop_rows, loose, view, ctx, feedback)
    wait = len(ctx["needs"])
    side = _sidebar(ctx, view, page, wait)
    fast = any(j.state in ("draft", "launching", "collecting") or (j.state == "running" and j.kind == "local-shell")
               or (j.state == "ready" and not j.row.get("detail")) for j in jobs)
    loop = view.get("checks") if isinstance(view.get("checks"), dict) else None
    pace = "fast" if fast and loop and loop.get("mode") == "on" else "calm"
    if live:
        head = ('<noscript><meta http-equiv="refresh" content="30"></noscript>\n'
                '<script src="/app.js" defer></script>\n')
        keys = "".join(f"<dt><kbd>{k}</kbd></dt><dd>{v}</dd>" for k, v in _KEYS)
        extra = ('<div class="toasts" aria-live="polite"></div>'
                 '<dialog id="keys" aria-label="Keyboard shortcuts">'
                 f'<p class="title" tabindex="-1" autofocus>Keyboard</p><dl>{keys}</dl>'
                 f'<div class="foot"><a href="{_url(ctx, "/actions")}">Every action as a plain form</a>'
                 '<form method="dialog"><button class="btn">Close</button></form></div></dialog>\n'
                 + _palette(jobs, ctx, set(re.findall(r'data-k="([a-z])"', pane + side))) + "\n")
        style = _FACES + STYLE
    else:
        head, extra, style = "", "", STYLE
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{_text(title)}</title>\n'
            f'{head}<style>{style}</style>\n</head>\n<body data-now="{now:.3f}" data-open="{_text(page)}" '
            f'data-pace="{pace}" data-jobs="{1 if jobs else 0}">\n'
            f'<div class="app">{side}<main class="pane" data-pane="{_text(page)}" tabindex="-1">{pane}</main></div>\n'
            f'{extra}</body>\n</html>\n')
