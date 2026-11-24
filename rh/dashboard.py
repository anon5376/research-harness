"""The app. With a token it is the live page `rh serve` sends, in Linear's frame: a labelled sidebar on the dark window
ground (the pages, This Mac and the check loop) beside one inset panel. Today puts the composer in a band of ASCII water,
with a jellyfish drawn in characters for each seat, above the jobs grouped by what you do next, and who is working on
the right. A job's page has the work on the left and its properties, receipt, pauses and stops, and timeline on the
right. The Inbox is a list and the item it shows; ⌘K opens a palette. One same-origin script, SCRIPT (rh/web/app.js,
served at /app.js), keeps it live, moves between pages without reloading and turns submits into in-place updates;
without it every link and form still works. Without a token it is the static snapshot `rh status --html` writes: the
jobs, what needs a person (with the command that settles it) and the day, with no script and no controls."""
import hashlib
import json
import re
import shlex
import sys
import time
import zlib
from html import escape
from pathlib import Path
from urllib.parse import quote

from rh import hierarchy

# The look and the client ship beside this module; rh serve sends the fonts from rh/web/fonts (see FONTS).
_WEB = Path(__file__).with_name("web")
STYLE = (_WEB / "rh.css").read_text(encoding="utf-8")
# bg.js draws the halftone field; it rides in front of app.js so /app.js stays the one script.
SCRIPT = "\n".join((_WEB / name).read_text(encoding="utf-8") for name in ("bg.js", "app.js", "team.js", "live.js"))
# A live page compares this with each page it fetches and reloads when they differ, so a restarted server never
# pours new markup into a page still wearing the old stylesheet and script.
BUILD = format(zlib.crc32((STYLE + SCRIPT).encode("utf-8")), "08x")
FONTS = ("GeistSerif-Square.woff2", "GeistMono-VF.woff2")  # the only files GET /fonts/<name> serves
BACKGROUNDS = ("halftone.webp", "clouds.webp")
PRESETS = ("beam", "arc", "clouds", "aurora", "dunes", "orbit", "quiet", "off")
TINTS = ("cobalt", "ice", "paper", "mono")  # ink tints of one halftone; no warm accent: amber is needs you, red failed
_LEGACY = {"halftone": "beam"}  # names saved by earlier versions
_SEP = " · "
_SEP_HTML = '<span class="sep">·</span>'
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
    "running": ("Running", "live"), "stalled": ("Past its time limit", "live"),
    "collecting": ("Checking the result", "live"), "done": ("Done", "good"), "failed": ("Failed", "bad"),
    "blocked": ("Blocked", "warn"), "capacity": ("No capacity", "warn"), "truncated": ("Cut short", "bad"),
    "aborted": ("Ended early", "bad"), "no_result": ("No result", "bad"), "cancelled": ("Cancelled", "neutral"),
    # not a ledger state: a ready cloud job while the codex CLI cannot see its environment, named once for every place
    "env": ("Waiting for environment", "warn"),
}
_PRE = ("draft", "ready", "waiting", "held", "handoff")
_CHAT_WAIT = ("Waiting for GPT Pro", "warn")  # a chat job's handoff: its prompt is out, its reply not back yet
_LIVE = ("launching", "handoff", "running", "stalled", "collecting")  # started and not yet finished
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
_PLATFORMS = {"gpt-chat": "GPT Pro", "codex-cloud": "Codex cloud", "codex-cloud-cli": "Codex cloud", "codex-cloud-web": "Codex cloud",
              "local-shell": HERE}
_PLACES = {"gpt-chat": "with GPT Pro", "codex-cloud": "in Codex cloud", "local-shell": f"on {HERE}"}
_WHERE = {"local-shell": "mac", "codex-cloud": "cloud", "gpt-chat": "chat"}  # what the list's tabs filter on
_WORKERS = (("chat", "GPT Pro"), ("cloud", "Codex cloud"), ("mac", HERE))
_NEEDS = ("net", "python", "git", "loopback", "browser", "gpu")  # capabilities the composer offers
# Reasoning effort levels, lowest first, named as Codex names them; the policy and the contract use the keys
_ANSWER_WORDS = (("chat", "In the chat"), ("file", "As answer.md"))
# Who an agent's answer is from, by the provider its command runs (rh/agent.py PROVIDER ...)
_AGENT_WHO = {"codex": ("Codex", "openai"), "claude": ("Claude", "claude"), "devin": ("Devin", "devin")}
_EFFORTS = (("light", "Light"), ("medium", "Medium"), ("high", "High"), ("extra-high", "Extra High"), ("max", "Max"),
            ("ultra", "Ultra"))
_NEED_WORDS = {"net": "Internet", "python": "Python", "git": "Git", "loopback": "Local ports", "browser": "Browser",
               "gpu": "GPU"}


def _minutes(m):
    """A time limit as people say it: 15 min, 2 h, 1 h 30 min."""
    h, rest = divmod(int(m), 60)
    return f"{h} h {rest} min" if h and rest else f"{h} h" if h else f"{rest} min"
_STALE = 300  # seconds without a check before the status line says the checks may have stopped
_TAIL = 12  # lines of a failed job's output shown open, as where it stopped
_CODEX = "https://chatgpt.com/codex"  # the pages outside the app a live page links to: a hand launch,
_CHATGPT = "https://chatgpt.com/"  # and a GPT Pro chat
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
         ("c", "Cancel a job that has not started"), ("l", "Save a result to a branch"), ("y", "Copy the answer"),
         ("w", "Swarm again"), ("t", "Check now"), ("⌘ / Ctrl Enter", "Send the form you are typing in"),
         ("Esc", "Close or leave a field"), ("?", "This sheet"))
# What the palette can press on a page, by the key that already does it there.
_PRESS = (("t", "Check now", "the harness, once"), ("u", "Resume", "lift the app's pause"),
          ("s", "Pause this job", "nothing new starts for it"), ("c", "Cancel this job", "before it starts"),
          ("a", "Link the Codex task", "paste the task link"), ("h", "Paste the patch", "from the Codex web view"),
          ("r", "Answer or mark done", "what this page asks"), ("l", "Save to a branch", "the finished result"),
          ("y", "Copy the answer", "its Markdown, as written"), ("w", "Swarm again", "the same brief, every model"),
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
    # a queued message's Edit and Remove (the running job's composer otherwise uses the app's own stop, up and clock)
    "pencil": '<path d="M17 3a2.85 2.85 0 0 1 4 4L7.5 20.5 2 22l1.5-5.5z"/><path d="m15 5 4 4"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "patch": '<path d="M12 4v11M7 10.5l5 5 5-5"/><path d="M4.5 19.5h15"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    "stop": '<circle cx="12" cy="12" r="9"/><path d="M8.6 15.4 15.4 8.6"/>',
    "step": '<path d="M4 6.5h16M4 12h16M4 17.5h9"/>',
    "again": '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 4.5v4h4"/>',
    "resume": '<path d="M8 5.5v13l10-6.5z"/>',
    # many into one: three runs in a row over the one the judge keeps (a Y of joined nodes read as git's fork)
    "swarm": ''.join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="currentColor" stroke="none"/>'
                     for x, y, r in ((5, 7.5, 2.25), (12, 7.5, 2.25), (19, 7.5, 2.25), (12, 17, 3))),
    "chat": '<path d="M5 5.5h14a1.5 1.5 0 0 1 1.5 1.5v8.5a1.5 1.5 0 0 1-1.5 1.5h-8l-4.5 3.5V17H5a1.5 1.5 0 0 1-1.5-1.5V7A1.5 1.5 0 0 1 5 5.5z"/>',
    "jobs": '<path d="M4 6.5h16M4 12h16M4 17.5h16"/>',
    "inspector": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M14 5v14"/>',
    "sidebar": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 5v14"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "effort": '<path d="M5 18v-2.5M9.67 18v-5M14.33 18v-7.5M19 18V8"/>',
    # Claude's mark, from Simple Icons (CC0), filled like OpenAI's
    "claude": '<path fill="currentColor" stroke="none" d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/>',
    # Devin's mark from the ACP registry (cdn.agentclientprotocol.com, as T3 Code shows it), and Cognition's from
    # cognition.ai/icon.svg without its 5% inner shadow; both scaled into the 24 box
    "devin": '<g fill="currentColor" stroke="none" transform="translate(1.480 0) scale(0.0800)"><path d="M1.84577e-10 100.797V38.6224C1.88813e-05 35.8899 1.45777 33.365 3.82414 31.9988L57.6539 0.920185C60.0203 -0.446051 62.9358 -0.446051 65.3022 0.920185L119.132 31.9988C121.498 33.3651 122.956 35.8899 122.956 38.6224V70.375C123.192 80.9262 128.769 91.103 138.577 96.7655C148.384 102.428 159.986 102.169 169.242 97.0979L196.74 81.2217C199.107 79.8554 202.022 79.8554 204.388 81.2217L258.218 112.3C260.585 113.667 262.042 116.191 262.042 118.924V181.081C262.042 183.814 260.585 186.339 258.218 187.705L204.388 218.783C202.022 220.15 199.107 220.15 196.74 218.783L169.464 203.036C160.173 197.847 148.465 197.536 138.579 203.244C128.772 208.906 123.195 219.083 122.959 229.634V261.378C122.959 264.111 121.501 266.635 119.135 268.002L65.3048 299.08C62.9384 300.447 60.0229 300.447 57.6566 299.08L3.82673 268.002C1.46036 266.635 0.00260562 264.111 0.00260562 261.378V199.221C0.0026245 196.488 1.46038 193.963 3.82675 192.597L57.6565 161.519C60.0229 160.152 62.9384 160.152 65.3048 161.519L92.8656 177.431C102.11 182.467 113.679 182.711 123.463 177.062C133.349 171.355 138.934 161.06 139.086 150.419C138.85 139.868 133.274 128.601 123.466 122.938C113.658 117.276 102.056 117.534 92.8007 122.606L65.1758 138.645C62.7991 140.025 59.8649 140.024 57.4894 138.642L3.80216 107.407C1.44816 106.038 -1.88809e-05 103.52 1.84577e-10 100.797Z"/></g>',
    "cognition": '<g fill="currentColor" stroke="none" transform="scale(1.11649) translate(0.747952 0.731469)"><path d="M13.2603 8.85657C13.6689 8.62171 14.174 8.62171 14.5826 8.85657L15.6386 9.46628C15.6723 9.48514 15.7089 9.5 15.746 9.51028C15.7534 9.51314 15.7609 9.51485 15.7694 9.516C15.806 9.52457 15.8431 9.52914 15.8797 9.53085H15.8854C15.89 9.53085 15.8929 9.53085 15.8974 9.52914C15.9311 9.52914 15.9649 9.52343 15.9986 9.516C16.0043 9.516 16.0106 9.51428 16.0163 9.51143C16.0517 9.50114 16.0866 9.48628 16.1191 9.46914C16.122 9.46743 16.1266 9.46628 16.1294 9.46457L18.2403 8.24514C18.3917 8.15828 18.4843 7.99714 18.4843 7.82228V5.384C18.4843 5.20914 18.3917 5.04742 18.2403 4.96114L16.1294 3.74171C15.978 3.65485 15.7917 3.65485 15.6403 3.74171L13.5294 4.96114C13.5294 4.96114 13.5237 4.96571 13.5209 4.96685C13.4889 4.98571 13.4574 5.00971 13.4311 5.036C13.4266 5.04057 13.4237 5.04457 13.4191 5.04914C13.3957 5.07428 13.3751 5.10228 13.3563 5.13142C13.3534 5.136 13.3506 5.14 13.3477 5.14628C13.33 5.17885 13.3151 5.21257 13.3049 5.24914C13.302 5.25657 13.3003 5.264 13.2991 5.27257C13.2906 5.30914 13.2843 5.34742 13.2843 5.38742V6.60685C13.2843 7.07714 13.0317 7.516 12.6231 7.75085C12.2146 7.98571 11.7094 7.98571 11.3009 7.75085L10.2449 7.14114C10.2111 7.12228 10.1746 7.10742 10.1374 7.09714C10.13 7.09428 10.1226 7.09257 10.114 7.09142C10.0774 7.08285 10.0403 7.07828 10.0037 7.07657H9.98771C9.95228 7.07657 9.91857 7.08228 9.88485 7.08971C9.87914 7.08971 9.87285 7.09142 9.86885 7.09257C9.83228 7.10285 9.79857 7.11771 9.76428 7.13542C9.76142 7.13714 9.75685 7.13828 9.75399 7.14L7.64314 8.35942C7.49171 8.44628 7.39914 8.60742 7.39914 8.78228V11.2206C7.39914 11.3954 7.49171 11.5571 7.64314 11.6434L9.75399 12.8629C9.75399 12.8629 9.76142 12.8657 9.76428 12.8674C9.79799 12.8851 9.83171 12.9 9.86885 12.9103C9.87457 12.912 9.88085 12.9131 9.88657 12.9149C9.92028 12.9223 9.95399 12.9269 9.98771 12.928C9.99228 12.928 9.99514 12.9297 9.99971 12.9297H10.0054C10.042 12.9297 10.0791 12.924 10.1157 12.9149C10.1231 12.9131 10.1306 12.9103 10.1391 12.9091C10.1757 12.8989 10.2111 12.884 10.2466 12.8651L11.3026 12.2554C11.7111 12.0206 12.2163 12.0206 12.6243 12.2554C13.0311 12.4903 13.2854 12.9297 13.2854 13.4V14.6194C13.2854 14.6589 13.2911 14.6971 13.3003 14.7337C13.302 14.7411 13.3031 14.7497 13.306 14.7571C13.3163 14.7926 13.3311 14.8274 13.3483 14.86C13.3511 14.8646 13.354 14.8686 13.3574 14.8749C13.3751 14.904 13.3957 14.932 13.4209 14.9571C13.4254 14.9617 13.4283 14.9657 13.4329 14.9703C13.4591 14.9966 13.49 15.0189 13.5226 15.0394C13.5254 15.0411 13.5283 15.044 13.5311 15.0451L15.642 16.2646C15.7169 16.3086 15.802 16.3309 15.886 16.3309C15.97 16.3309 16.0551 16.3086 16.13 16.2646L18.2409 15.0451C18.3923 14.9583 18.4849 14.7971 18.4849 14.6223V12.184C18.4849 12.0091 18.3923 11.8474 18.2409 11.7611L16.13 10.5417C16.13 10.5417 16.1226 10.5389 16.1197 10.5371C16.086 10.5194 16.0523 10.5046 16.0157 10.4943C16.01 10.4931 16.0054 10.4931 15.9997 10.4914C15.966 10.4829 15.9306 10.4783 15.8969 10.4783H15.8809C15.8443 10.4783 15.8077 10.484 15.7706 10.4931C15.7631 10.4949 15.7557 10.4977 15.7489 10.4989C15.7123 10.5091 15.6769 10.524 15.6414 10.5429L14.5854 11.1526C14.1786 11.3874 13.6717 11.3874 13.2637 11.1526C12.8569 10.9177 12.6026 10.4783 12.6026 10.008C12.6026 9.53771 12.8551 9.09885 13.2637 8.86343L13.262 8.85771L13.2603 8.85657Z"/><path d="M1.75971 8.24743L3.87057 9.46685C3.94542 9.51085 4.03057 9.53314 4.11457 9.53314C4.19857 9.53314 4.28371 9.51085 4.35857 9.46685L6.46942 8.24743C6.46942 8.24743 6.47514 8.24285 6.47799 8.24171C6.51057 8.22285 6.54085 8.19885 6.56771 8.17257C6.57228 8.168 6.57514 8.164 6.57971 8.15942C6.60314 8.13428 6.62371 8.10628 6.64314 8.07714C6.64599 8.07257 6.64885 8.06857 6.65171 8.06228C6.66942 8.02971 6.68428 7.996 6.69457 7.95942C6.69742 7.952 6.69914 7.94457 6.70028 7.936C6.70885 7.89943 6.71514 7.86114 6.71514 7.82171V6.60228C6.71514 6.132 6.96771 5.69314 7.37628 5.45828C7.78485 5.22342 8.28999 5.22342 8.69857 5.45828L9.75457 6.068C9.78828 6.08685 9.82485 6.10171 9.86199 6.112C9.86942 6.11485 9.87685 6.11657 9.88542 6.11771C9.92199 6.12628 9.95742 6.13085 9.99571 6.13257H10.0014C10.006 6.13257 10.0089 6.13085 10.0134 6.13085C10.0471 6.13085 10.0809 6.12514 10.1146 6.11771C10.1203 6.11771 10.1266 6.116 10.1323 6.11314C10.1689 6.10285 10.2026 6.088 10.2369 6.07028C10.2397 6.06914 10.2443 6.06742 10.2471 6.06571L12.358 4.84628C12.5094 4.75942 12.602 4.59828 12.602 4.42342V1.98514C12.602 1.81028 12.5094 1.64857 12.358 1.56228L10.2471 0.342853C10.0957 0.255996 9.90942 0.255996 9.75799 0.342853L7.64714 1.56228C7.64714 1.56228 7.64142 1.56685 7.63857 1.568C7.60657 1.58685 7.57571 1.61085 7.54885 1.63714C7.54428 1.64171 7.54142 1.64571 7.53685 1.65028C7.51342 1.67542 7.49285 1.70342 7.47342 1.73257C7.47057 1.73714 7.46771 1.74114 7.46485 1.74742C7.44714 1.78 7.43228 1.81371 7.42199 1.85028C7.41914 1.85771 7.41742 1.86514 7.41628 1.87371C7.40714 1.91028 7.40142 1.94857 7.40142 1.98857V3.208C7.40142 3.67828 7.14885 4.11714 6.74028 4.35257C6.33342 4.58742 5.82657 4.58742 5.41799 4.35257L4.36199 3.74285C4.32828 3.724 4.29171 3.70914 4.25457 3.69885C4.24714 3.696 4.23971 3.69428 4.23114 3.69314C4.19457 3.68457 4.15742 3.68 4.12085 3.67828H4.10485C4.06942 3.67828 4.03571 3.684 4.00199 3.69142C3.99628 3.69142 3.98999 3.69314 3.98599 3.69428C3.94942 3.70457 3.91571 3.71942 3.88142 3.73714C3.87857 3.73885 3.87399 3.74 3.87114 3.74171L1.76028 4.96114C1.60885 5.048 1.51628 5.20914 1.51628 5.384V7.82228C1.51628 7.99714 1.60885 8.15885 1.76028 8.24514V8.248L1.75971 8.24743Z"/><path d="M12.3551 15.1514L10.2443 13.932C10.2443 13.932 10.2369 13.9291 10.234 13.9274C10.2003 13.9097 10.1666 13.8954 10.1294 13.8851C10.1237 13.8834 10.1174 13.8823 10.1117 13.8806C10.078 13.8731 10.0443 13.8674 10.0089 13.8674H9.99285C9.95628 13.8674 9.91914 13.8731 9.88257 13.8823C9.87514 13.884 9.86771 13.8869 9.86028 13.888C9.82371 13.8983 9.78828 13.9131 9.75285 13.932L8.69685 14.5417C8.28999 14.7766 7.78314 14.7766 7.37628 14.5417C6.96771 14.3069 6.71514 13.8674 6.71514 13.3971V12.1777C6.71514 12.1383 6.70942 12.1 6.70028 12.0629C6.69914 12.0554 6.69742 12.0469 6.69457 12.0394C6.68428 12.004 6.66942 11.9691 6.65171 11.9366C6.64885 11.932 6.64599 11.928 6.64314 11.9217C6.62542 11.8926 6.60485 11.8646 6.57971 11.8394C6.57514 11.8349 6.57228 11.8309 6.56828 11.8263C6.54199 11.8 6.51114 11.7777 6.47857 11.7571C6.47571 11.7554 6.47285 11.7526 6.46999 11.7514L4.35914 10.532C4.20771 10.4451 4.02142 10.4451 3.86999 10.532L1.75914 11.7514C1.60771 11.8383 1.51514 11.9994 1.51514 12.1743V14.6126C1.51514 14.7874 1.60771 14.9491 1.75914 15.0354L3.86999 16.2549C3.86999 16.2549 3.87742 16.2577 3.88028 16.2594C3.91399 16.2771 3.94771 16.2914 3.98314 16.3017C3.98885 16.3034 3.99514 16.3046 4.00085 16.3063C4.03457 16.3137 4.06714 16.3183 4.10199 16.3194C4.10657 16.3194 4.11057 16.3206 4.11399 16.3206H4.11971C4.15628 16.3206 4.19342 16.3149 4.22828 16.3057C4.23571 16.304 4.24428 16.3011 4.25171 16.3C4.28828 16.2897 4.32371 16.2749 4.35914 16.256L5.41514 15.6463C5.82371 15.4114 6.32885 15.4114 6.73742 15.6463C7.14428 15.8811 7.39857 16.3206 7.39857 16.7909V18.0103C7.39857 18.0497 7.40428 18.088 7.41342 18.1251C7.41514 18.1326 7.41628 18.1411 7.41914 18.1486C7.42942 18.184 7.44428 18.2189 7.46199 18.2514C7.46485 18.256 7.46771 18.26 7.47057 18.2663C7.48828 18.2954 7.50885 18.3234 7.53399 18.3486C7.53857 18.3531 7.54142 18.3571 7.54542 18.3617C7.57171 18.388 7.60257 18.4103 7.63514 18.4309C7.63799 18.4326 7.64085 18.4354 7.64428 18.4366L9.75514 19.656C9.82999 19.7 9.91514 19.7223 9.99914 19.7223C10.0831 19.7223 10.1683 19.7 10.2431 19.656L12.354 18.4366C12.5054 18.3497 12.598 18.1886 12.598 18.0137V15.5754C12.598 15.4006 12.5054 15.2389 12.354 15.1526L12.3551 15.1514Z"/></g>',
    # OpenAI's mark, from Simple Icons (CC0), filled rather than stroked like the rest
    "openai": '<path fill="currentColor" stroke="none" d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "copy": ('<rect x="8.5" y="8.5" width="11" height="11" rx="2"/>'
             '<path d="M15.5 8.5V6A1.5 1.5 0 0 0 14 4.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>'),
    "tag": '<path d="M3.5 12.2V5a1.5 1.5 0 0 1 1.5-1.5h7.2l8.3 8.3a1.5 1.5 0 0 1 0 2.1l-7 7a1.5 1.5 0 0 1-2.1 0z"/><circle cx="8" cy="8" r="1.3"/>',
    "flask": ('<path d="M9.5 3.5V9L5.5 18.9a1.5 1.5 0 0 0 1.4 1.6h10.2a1.5 1.5 0 0 0 1.4-1.6L14.5 9V3.5"/>'
              '<path d="M8.5 3.5h7"/><path d="M7.5 15h9"/>'),
    "plug": '<path d="M9 3.5v5M15 3.5v5M6.5 8.5h11v3a5.5 5.5 0 0 1-11 0zM12 17v3.5"/>',
    "term": '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="m7.5 9.5 3 2.5-3 2.5M12.5 15h4"/>',
    "folder": '<path d="M3.5 7a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "branch": ('<circle cx="6.5" cy="5.5" r="2"/><circle cx="6.5" cy="18.5" r="2"/><circle cx="17.5" cy="7.5" r="2"/>'
               '<path d="M6.5 7.5v9M17.5 9.5c0 4-11 3-11 7"/>'),
    "gear": ('<circle cx="12" cy="12" r="3"/><path d="m9.2 4-.6 2.1-1.8 1-2-.5-1.9 3.2 1.5 1.6v2.1l-1.5 1.6 1.9 3.2'
             ' 2-.5 1.8 1 .6 2.1h3.7l.6-2.1 1.8-1 2 .5 1.9-3.2-1.5-1.6v-2.1l1.5-1.6-1.9-3.2-2 .5-1.8-1-.6-2.1z"/>'),
    "coord": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
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
    # running past its limit: the running half inside a dashed ring
    "late": ('<circle cx="7" cy="7" r="5.6" fill="none" stroke="currentColor" stroke-width="1.4" '
             'stroke-dasharray="2.1 2.3"/>'
             '<path d="M7 7V3.9A3.1 3.1 0 0 1 7 10.1Z" fill="currentColor"/>'),
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
          "running": ("half", "accent"), "stalled": ("late", "accent"), "collecting": ("three", "accent"),
          "done": ("done", "accent"), "cancelled": ("cancelled", "faint"), "env": ("wait", "amber"),
          **{s: ("failed", "red") for s in _FAILED}}
# The sea: where a seat's jellyfish sits in a small tank, as fractions of it, and how fast each swims.
_SLOTS = ((0.1, 0.3), (0.9, 0.22), (0.03, 0.9), (0.79, 0.76), (0.33, 0.93), (0.985, 0.86), (0.64, 0.07))
_SPEEDS = (0.8, 0.95, 1.1)
_MARK = ((2, 3, 4), (1, 2, 3, 4, 5), (0, 1, 2, 3, 4, 5, 6), (0, 2, 4, 6), (0, 2, 4, 6), (1, 4), (2, 5))


def _text(value):
    return escape(str(value), quote=True)


# A line that says what went wrong, and the routine chatter around it.
_LOUD = re.compile(r"\b(WARN|WARNING|ERROR|FATAL|CRITICAL|PANIC)\b|^\s*Traceback\b|^\s*[\w.]*(Error|Exception):"
                   r"|^\s*(error|fatal)(\[\w+\])?:|: (No such file or directory|command not found|Permission denied)$")
_QUIET = re.compile(r"\b(INFO|DEBUG|TRACE)\b|^stopped: ")


def _ln(line):
    """One log line as its own block, so a wrapped line hangs under the line's start; a blank keeps its row. A warning
    or error stands out and routine INFO lines step back, so the cause is found without reading the trace."""
    tone = " loud" if _LOUD.search(line) else " quiet" if _QUIET.search(line) else ""
    return f'<span class="ln{tone}">{_text(line)}</span>'


_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")  # Markdown bold: no space just inside the markers


def _tail_lines(tail):
    """A kept tail as the lines the Inbox's stop card shows: blank lines at its ends dropped, so its first line
    sits where the same card on a job's page puts it, and each line's paired ** markers off, so an agent's
    markdown reads as words; a ** with no partner, or spaced as an operator (a ** b ** c), stays: logs hold code."""
    lines = str(tail or "").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return [_BOLD.sub(r"\1", line) for line in lines]


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def _diff_parse(text):
    """A unified diff as rows (kind, number, line) and its files as [path, added, removed]. kind is file, hunk, add,
    del or ctx; number is the line's number in the new file (None for a removed line, a file or a hunk). A hunk's
    body is read by its counts, so a removed line that starts '--' is not taken for a header. Git's header lines are
    dropped: the file row says what they said."""
    rows, files = [], []
    old_left = new_left = new_n = 0
    minus, pending = "", None  # the last '--- ' path; the row of a 'diff --git' file still waiting for '+++ '
    for line in text.splitlines():
        if (old_left > 0 or new_left > 0) and line[:1] not in ("", " ", "+", "-", "\\"):
            old_left = new_left = 0  # a body line starts with one of those: a hunk whose head over-counted has ended
        if old_left > 0 or new_left > 0:
            mark = line[:1]
            if mark == "\\":  # "\ No newline at end of file"
                continue
            if mark == "+":
                rows.append(("add", new_n, line))
                new_n, new_left = new_n + 1, new_left - 1
                files[-1][1] += 1
            elif mark == "-":
                rows.append(("del", None, line))
                old_left -= 1
                files[-1][2] += 1
            else:
                rows.append(("ctx", new_n, line))
                new_n, old_left, new_left = new_n + 1, old_left - 1, new_left - 1
            continue
        hunk = _HUNK.match(line)
        if hunk:
            if not files:
                files.append(["", 0, 0])
            old_left = int(hunk.group(1) or 1)
            new_n, new_left = int(hunk.group(2)), int(hunk.group(3) or 1)
            rows.append(("hunk", None, hunk.group(4)))
        elif line.startswith("diff --git "):
            files.append([line.rsplit(" b/", 1)[-1], 0, 0])
            rows.append(("file", None, files[-1][0]))
            pending = len(rows) - 1
        elif line.startswith("--- "):
            minus = line[4:].split("\t")[0].removeprefix("a/")
        elif line.startswith("+++ "):
            path = line[4:].split("\t")[0].removeprefix("b/")
            path = minus if path == "/dev/null" else path
            if pending is not None:
                files[-1][0] = path
                rows[pending] = ("file", None, path)
            else:
                files.append([path, 0, 0])
                rows.append(("file", None, path))
            pending = None
    return rows, files


def _diff_head(text, whole=True):
    """A patch preview's head: the file it changes (or how many) and its added and removed line counts. A preview
    cut short names only its first file, since its counts would undercount."""
    files = _diff_parse(text)[1]
    if not files:
        return ""
    if not whole:
        return f'<span class="mono">{_text(files[0][0] + (" and more" if len(files) > 1 else ""))}</span>'
    name = files[0][0] if len(files) == 1 else f"{len(files)} files"
    kind = " files" if len(files) > 1 else ""  # marks a count: a phone sheds a lone path but keeps 'N files'
    return f'<span class="mono{kind}">{_text(name)}</span>' + _counts(sum(f[1] for f in files), sum(f[2] for f in files))


def _counts(added, removed):
    zero = lambda n: "" if n else " zero"  # noqa: E731  (a zero count is quiet, not green or red)
    return f'<span class="n add{zero(added)}">+{added}</span><span class="n del{zero(removed)}">−{removed}</span>'


def _peek_html(path, text, whole=True):
    """A file's preview, escaped. A patch reads as a diff: each line its own block, marked added or removed and
    numbered as in the new file (the gutter draws it), a hunk headed by its function, and a wrapped line hung under
    its code; a patch of several files names each where it starts, with its own counts (not the last file's when the
    preview is cut short, as they would undercount)."""
    rows, files = _diff_parse(text) if path.endswith((".patch", ".diff")) else ([], [])
    if not files:
        return _text(text)
    out, seen = [], 0
    for kind, n, line in rows:
        if kind == "file":
            if len(files) > 1:
                f = files[seen]
                counts = _counts(f[1], f[2]) if whole or seen < len(files) - 1 else ""
                out.append(f'<span class="dl file">{_text(line)}{counts}</span>')
            seen += 1
            continue
        if kind == "hunk":
            out.append(f'<span class="dl hunk">{_text(line)}</span>')
            continue
        body = line[1:]
        indent = len(body[:len(body) - len(body.lstrip(" \t"))].expandtabs(4))
        out.append(f'<span class="dl{"" if kind == "ctx" else " " + kind}"'
                   + (f' data-n="{n}"' if n is not None else "")
                   + (f' style="--i:{indent + 1}"' if indent else "") + f'>{_text(line)}</span>')
    return "".join(out)


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
    return f"{_DAYS[t.tm_wday]} {t.tm_mday} {_MONTHS[t.tm_mon - 1]}{_SEP_HTML}{t.tm_hour:02d}:{t.tm_min:02d}"


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


def _ring(state, hue=None, kind="", past=False):
    """A job's state as one ring, named for a screen reader; `hue` overrides its colour (amber in Needs you), and
    `past` draws a failure already run again quieter."""
    shape, tone = _RINGS.get(state, ("empty", "faint"))
    tone = hue or tone
    word = _CHAT_WAIT[0] if kind == "gpt-chat" and state == "handoff" else _STATES.get(state, (state,))[0]
    cls = f'st t-{tone}{" past" if past else ""}'
    return (f'<svg class="{cls}" data-s="{shape}" viewBox="0 0 14 14" width="14" height="14" role="img" '
            f'aria-label="{_text(word)}">{_SHAPES[shape]}</svg>')


def _where(kind):
    """Where a job runs, as the tabs' icon, named for a screen reader."""
    name, icon = (HERE, "mac") if kind == "local-shell" else ("Codex cloud", "cloud") if kind == "codex-cloud" else (
        "GPT Pro", "chat") if kind == "gpt-chat" else (_platform(kind), "")
    return (f'<span class="where" role="img" aria-label="{_text(name)}" data-w="{icon}">{_ic(icon, True)}</span>' if icon
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
    """The canvas the client draws the sea on, in characters; it draws nothing without the script. `fauna` is the rest
    of the sea life (see _fauna)."""
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
             (r"runner ended without an exit record", "No exit record"),
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


_MD_INLINE = re.compile(r"`([^`\n]+)`|\*\*(.+?)\*\*|(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])|\[([^\]\n]+)\]\(([^)\s]+)\)")
_MD_HEAD = re.compile(r"(#{1,6})\s+(.*)")
_MD_ITEM = re.compile(r"([ \t]*)(?:([-*•])|(\d+)[.)])\s+(.*)")


def _breaks(code_html):
    """Long escaped code may wrap after _ / . and , so an identifier breaks at its own seams, never mid-word."""
    return re.sub(r"([_/.,])", r"\1<wbr>", code_html)


def _md_inline(text):
    """One line of Markdown as HTML: code, bold, italics and links; everything else escaped. A link opens only if it
    is https; a link to a local path is shown as its words, with the path on hover."""
    out, at = [], 0
    for m in _MD_INLINE.finditer(text):
        out.append(escape(text[at:m.start()]))
        code, bold, em, label, href = m.groups()
        if code is not None:
            # short code never wraps; a long identifier moves whole to the next line and wraps only when wider than
            # the column; a long phrase or path wraps at its own seams
            out.append(f'<code class="nw">{escape(code)}</code>' if len(code) <= 40 else
                       f'<code class="tok">{escape(code)}</code>' if not re.search(r"[\s/]", code) else
                       f"<code>{_breaks(escape(code))}</code>")
        elif bold is not None:
            out.append(f"<strong>{_md_inline(bold)}</strong>")
        elif em is not None:
            out.append(f"<em>{_md_inline(em)}</em>")
        elif href.startswith("https://"):
            out.append(f'<a href="{escape(href)}" target="_blank" rel="noopener noreferrer">{_md_inline(label)}</a>')
        else:
            out.append(f'<span class="md-path" title="{escape(href)}">{_md_inline(label)}</span>')
        at = m.end()
    out.append(escape(text[at:]))
    return "".join(out)


def _markdown(text):
    """An agent's answer, written in Markdown, as HTML: paragraphs (hard-wrapped lines joined), headings, bullet and
    numbered lists, ``` code blocks, and the inline marks of _md_inline. No raw HTML passes through."""
    blocks, para, items, kind, code = [], [], [], "", None

    def flush():
        nonlocal para, items, kind
        if para:
            joined = " ".join(para)
            # an answer that opens with one bold line opens with its title
            lead = not blocks and re.fullmatch(r"\*\*([^*]+)\*\*", joined)
            blocks.append(f'<p class="md-title">{_md_inline(lead.group(1))}</p>' if lead
                          else f"<p>{_md_inline(joined)}</p>")
        if items:
            blocks.append(f"<{kind}>" + "".join(f"<li>{_md_inline(i)}</li>" for i in items) + f"</{kind}>")
        para, items, kind = [], [], ""

    for line in str(text or "").splitlines():
        if code is not None:
            if line.lstrip().startswith("```"):
                blocks.append(f"<pre><code>{escape(chr(10).join(code))}</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.lstrip().startswith("```"):
            flush()
            code = []
        elif not line.strip():
            flush()
        elif head := _MD_HEAD.fullmatch(line.strip()):
            flush()
            blocks.append(f'<p class="md-h">{_md_inline(head.group(2))}</p>')
        elif item := _MD_ITEM.fullmatch(line):
            want = "ol" if item.group(3) else "ul"
            if para or (items and kind != want):
                flush()
            kind = want
            items.append(item.group(4))
        elif items and line.startswith((" ", "\t")):
            items[-1] += " " + line.strip()  # a list item's wrapped line
        else:
            if items:
                flush()
            para.append(line.strip())
    if code is not None:
        blocks.append(f"<pre><code>{escape(chr(10).join(code))}</code></pre>")
    flush()
    return "".join(blocks)


def agent_of(command):
    """(provider, model, effort) of a local agent job's command (rh/agent.py PROVIDER BINARY REPO BRIEF ...), or None
    for any other command. The effort is in the composer's words (light, extra-high), so a follow-up can keep it."""
    if not isinstance(command, list) or len(command) < 6 or not str(command[1]).endswith("agent.py"):
        return None
    tail = [str(part) for part in command[6:]]
    model = tail[tail.index("--model") + 1] if "--model" in tail[:-1] else ""
    effort = tail[tail.index("--effort") + 1] if "--effort" in tail[:-1] else ""
    for part in tail:
        if part.startswith("model_reasoning_effort="):
            effort = part.split("=", 1)[1].strip('"')
    return str(command[2]), model, {"low": "light", "xhigh": "extra-high"}.get(effort, effort)


def ask_of(spec):
    """What the person asked in this run: the brief for a first run, the follow-up for a later one ("" for a plain
    rerun)."""
    if spec.get("prior"):
        changed = str(spec.get("changed") or "")
        return "" if changed == "run again with no changes" or changed.startswith("added input ") else changed
    return str(spec.get("brief") or "").strip()


_STEP = re.compile(r"\b(\d+) of (\d+)\b")


def _progress(log):
    """How far a run says it is: (n, m) from the newest of its last five lines that reads "n of m", or None."""
    for line in reversed(str(log or "").rstrip().splitlines()[-5:]):
        if found := _STEP.search(line):
            n, total = int(found.group(1)), int(found.group(2))
            return (n, total) if 0 < n <= total and total > 1 else None
    return None


_STATUS_LEAD = re.compile(r"^(?:(?:done|finished|ok|okay|complete|completed|all done)(?:[.!]|\s*[—–:])?(?:\s+|$))+",
                          re.I)
_LINE_NO = re.compile(r"(\.[A-Za-z0-9]{1,6})(?::\d+(?:[-–]\d+)?)+\b")  # rh/serve.py:12:5 -> rh/serve.py
# A swarm's judge or run talking about its own review rather than the work: which run it chose, the applied diff or
# patch, how the tests or the test run went, or that everything verifies. Such a line never names the swarm.
_SELF_REMARK = re.compile(r"(I|We)\s+(chose|kept|picked|took|went with|combined)\b|The applied\b"
                          r"|The (\w+ )?(diff|patch) is\b"
                          r"|The (\w+ )?tests?( runs?)? (was|were|is|are|pass|passes|passed|fail|fails|failed"
                          r"|asserts?|covers?)\b|(Both|Neither|All) runs?\b"
                          r"|(Everything|All( (checks|tests))?) (verifies|verified|checks out|pass|passes|passed)\b")


# An agent's preamble about how it read the code, or a bare acknowledgement, before it says what it found or did.
# "Read X as bytes so …" is a change and still names the run; only a Read line that reports reading ("fully", "plus") is
# preamble.
_PREAMBLE = re.compile(r"(?:I(?:['’]ve| have)?\s+(?:read|reviewed|looked|went|traced|checked|inspected)\b"
                       r"|Read\b.*\b(?:fully|in full|plus)\b"
                       r"|(?:Fair enough|Sure|Got it|Understood|Alright|Right)(?:[.,!:;]|\s+[—–-]))", re.I)
_FINDING = ("bug", "issue", "problem", "finding", "cause", "root cause")  # a heading label for what a run found
_PATH_LEAD = re.compile(r"[\w.-]*/[\w./-]+|[\w-]+\.(?:py|js|ts|css|html|md|toml|json|sh|txt|ya?ml)\b")
_CODE = re.compile(r"[\w.\[\]]+\s*=[^=]|\w+\(")  # an assignment or a call: a line of code, not words
_NO_FINAL = "Devin ended its turn without a final message"  # rh/agent.py writes it, in italics, as the answer


def answer_head(text):
    """An agent's answer as a list label: its first line without Markdown marks, up to a dash or colon that starts an
    aside ("Fixed X — untested: …" names the run as "Fixed X"), at most 100 characters; "" when it has no words."""
    line, fenced = "", False
    for raw in str(text or "").splitlines():
        if raw.strip().startswith("```"):  # code never names a run, nor does anything inside a fence
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = re.fullmatch(r"\*\*.+\*\*", raw.strip()) is not None
        # boilerplate never names a run: a status word alone ("Done."), a leading one ("Done. Rounded the ETA"), or a
        # lead-in that ends in a colon ("Summary of the diff:") gives way to the next line
        x = re.sub(r"^#+\s*|^[-*]\s+", "", raw.strip()).replace("**", "").replace("`", "").replace("__", "")
        x = re.sub(r"^([_*])(?=\S)(.*\S)\1$", r"\2", x)  # a line in _italics_ or *italics* names a run without its marks
        if x.startswith(_NO_FINAL):  # the harness's notice that the agent said nothing is no answer to name it by
            return ""
        if _PREAMBLE.match(x):
            continue
        if heading and x.endswith(":") and re.search(r"\s[—–]\s|:\s", x):  # "**Bug — x.py:59, in f:**" is a heading
            x = x[:-1].rstrip()
        bare = _STATUS_LEAD.sub("", x).strip()
        if bare != x:  # what a status word leaves starts as a sentence: no stray mark, a capital
            bare = bare.lstrip("—–-:·•, ")
            bare = bare[:1].upper() + bare[1:]
        x = bare
        if x and not x.endswith(":"):
            line = x
            break
    head, *rest = re.split(r"\s+[—–]\s+|:\s", line, maxsplit=1)
    # "rh/dashboard.py — X did Y": a lone label; the words name it. "Bug: x.py:82 — the \\b in X": two labels in a row
    if rest and head.strip().lower() in _FINDING and _PATH_LEAD.match(rest[0].strip()):
        # "Bug: x.py:82 — what" names the run "Bug in x.py: what", so no name starts with a path (critic r26 F1)
        where, *what = re.split(r"\s+[—–]\s+|:\s", rest[0].strip(), maxsplit=1)
        where = re.sub(r",\s+in\s+", " in ", _LINE_NO.sub(r"\1", where).strip())
        head = f"{head.strip().capitalize()} in {where}"
        said = re.split(r"(?<=[.!?])\s", what[0].strip(), maxsplit=1)[0] if what else ""
        if said and not _CODE.match(said):  # a line of code after the path is no name
            head += ": " + said
        rest = []
    # A line of code after the label ("rh/oslock.py:20 — fd = os.open(…)") is no name: the label stays.
    while rest and not re.search(r"\s", head.strip()):
        nxt, *after = re.split(r"(?<=[.!?])\s|\s+[—–]\s+|:\s", rest[0].strip(), maxsplit=1)
        if _CODE.match(nxt.strip()):
            break
        head, rest = nxt, after
    # a label gets cut by the list's ellipsis, which would leave a false ':1…' of a line number: the number goes whole
    line = _LINE_NO.sub(r"\1", head).strip().rstrip(".")
    if re.match(r"[a-z]+(?:\s|$)", line):  # a plain lowercase word starts a sentence; a code name keeps its case
        line = line[0].upper() + line[1:]
    if len(line) > 100:
        line = line[:98].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return line


def _title(meta, jid):
    """A job's name in the list and the palette: the first sentence of its brief, cut to at most 98 characters and an
    ellipsis when it runs past 100, else its id."""
    line = _lead(meta)[0]
    if len(line) > 100:
        line = line[:98].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return line or str(jid)


def _chain(meta, jid, all_meta):
    """A job's place in its run chain: (the chain's root id, the said that names it). A follow-up (meta "prior") is
    named by the first run of its chain that left an answer's first line, walking back to the root — a later answer's
    line shows inside the thread, it does not rename it. A chain where no run has a said gets ""; a job the meta does
    not list or a "prior" loop ends the walk."""
    said, root, seen = "", jid, {jid}
    while isinstance(meta, dict):
        said = str(meta.get("said") or "") or said
        prior = str(meta.get("prior") or "")
        if not prior or prior in seen or not isinstance(all_meta.get(prior), dict):
            return root, said
        seen.add(prior)
        root, meta = prior, all_meta[prior]
    return root, said


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
    h, rest = seconds // 3600, seconds % 3600 // 60
    return f"{h} h {rest} min" if rest else f"{h} h"


def _left(seconds):
    """A guess at the time left: an hour or more rounds to whole hours ("~10 h left"); less keeps its minutes."""
    seconds = max(0, round(seconds))
    return f"~{(seconds + 1800) // 3600} h left" if seconds >= 3600 else f"~{_duration(seconds)} left"


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
    if kind == "chat" and job:
        return "Ask GPT Pro, then paste its reply", ""
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
    if kind == "chat":
        return "chat", "chat"
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
        self.handoff = (self.state == "handoff" and self.kind != "gpt-chat"
                        and not any(i.get("kind") == "web-launch" for i in self.items))
        self.mine = [s for s in self.stops if s.get("owner") == "app"]
        ended = self.meta.get("updated")
        self.brief = str(self.meta.get("brief") or "").strip()
        made = self.meta.get("created")
        rerun = ctx["rerun"].get(self.brief) if self.brief else None
        self.failure = (self.state in _FAILED and where == "recent" and self.id not in ctx["followed"]
                        and not (_number(rerun) and _number(made) and made <= rerun)
                        and (not _number(ended) or ctx["now"] - ended < _FAILURE_WINDOW))
        # a failure already run again (or past its window) keeps its ring's shape, not its urgency
        self.past = self.state in _FAILED and not self.failure
        self.held = self.state == "held" and not self.items
        self.setup = bool(ctx["setup"]) and self.kind == "codex-cloud" and self.state in _PRE
        self.native = bool(ctx.get("native")) and self.kind == "codex-cloud" and self.state == "ready"
        limit = self.meta.get("timeout_min")
        if self.state == "stalled" and _number(limit) and limit > 0:
            self.word = f"Past its {_minutes(limit)} limit"  # the limit the job page names, in every list
        if self.native:
            self.word, self.tone = _STATES["env"]
        elif self.kind == "gpt-chat" and self.state == "handoff":
            self.word, self.tone = _CHAT_WAIT
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
        self.root, self.said = _chain(self.meta, self.id, ctx["meta"])
        # The lists name a done job by what its agent said it did: one brief run many times reads as many runs.
        # A follow-up wears the first said of its chain, so its own answer's first line never renames the thread.
        self.label = (self.said if self.state == "done" else "") or self.title
        self.dup = False  # set once every job is known: another listed job has the same name
        self.place = _WHERE.get(self.kind, str(self.kind)[:6])

    def headline_parts(self, runs=1):
        """Text parts of the one-line list headline, keeping user text separate from authored separators."""
        if self.items:
            return [_ask_line(self.items[0])[0]]
        if self.handoff:
            return ["Launch it in Codex"]
        if self.mine:
            return ["Paused from the app"]
        detail = _plain(self.row.get("detail"))
        parts = []
        held = [s for s in self.stops if s.get("owner") != "app"]
        if held:
            parts.append(f"{'Paused' if held[0].get('kind') == 'pause' else 'Stopped'} by {held[0]['owner']}")
        elif self.row.get("paused"):
            parts.append("Paused")
        if self.failure or self.held:
            word = f"{self.word} {runs} times" if runs > 1 else self.word
            # a sentence after a colon runs on in lower case ("Failed: the command exited with code 1"); an
            # acronym or a name in capitals keeps them
            if detail[:1].isupper() and detail[1:2].islower():
                detail = detail[0].lower() + detail[1:]
            return parts + [f"{word}: {detail}" if detail else word]
        if self.setup:
            return parts + ["Waits for Codex cloud to be set up"]
        if self.native:
            return parts + [self.word]
        if self.group == "live" and self.state in ("draft", "ready", "launching"):
            parts.append("Starting")
        elif self.state == "running" and self.kind in _PLACES:
            parts.append(f"Running {_PLACES[self.kind]}")
        else:
            parts.append(self.word)
        if detail and self.state not in ("draft", "ready", "launching"):
            parts.append(detail)
        return parts

    def headline(self, runs=1):
        """The headline as plain text for data and non-HTML contexts."""
        return _SEP.join(self.headline_parts(runs))

    def headline_html(self, runs=1):
        """Render the harness-authored headline separators as spans, escaping every text part."""
        return _SEP_HTML.join(_text(part) for part in self.headline_parts(runs))

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
            if kind == "chat":
                return go(page, "Ask GPT Pro", "chat", "a")
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

    def name_html(self):
        """The job's name in a list; when another listed job shares it, its id (named for the job when it was added)
        tells the two apart, with the name on hover."""
        if not self.dup:
            return _text(self.label)
        return f'<span class="nm" title="{_text(self.label)}">{_text(self.id)}</span>'

    def item(self, runs=1):
        """The list entry: the title over the line that says why, a link to the job's page across the whole row,
        and the one control it offers. In Needs you the ring is amber, or red for a failure; a failure repeated by
        `runs` runs of its brief is one row, for its newest run, and one already run again draws quieter."""
        ctx = self.ctx
        since = self.since()
        hue = ("red" if self.state in _FAILED else "amber") if self.group == "needs" else None
        act = self.act()
        cells = (f'{_ring("env" if self.native else self.state, hue, self.kind, self.past)}'
                 f'<span class="name">{self.name_html()}</span>'
                 f'<span class="sub">{self.headline_html(runs)}</span>{_where(self.kind)}'
                 f'<span class="time mono num">{_clock(since, ctx["now"]) if since is not None else ""}</span>')
        link = (f'<a class="item" href="{_url(ctx, "/job/" + self.id)}" data-nav>{cells}</a>' if ctx["live"]
                else f'<div class="item">{cells}</div>')
        do = f'<span class="do">{act}</span>' if act else ""
        return _row(f"job:{self.id}", link + do, attrs=f' data-where="{_text(self.place)}" data-tone="{self.tone}"')


def _need_row(ctx, key, ask, detail, when, cli, job=None, title=None, status=""):
    """The snapshot's printed card for one thing a person can settle, with the command that settles it."""
    about = ""
    if job:
        tail = f"{_SEP_HTML}{status}" if status else ""
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
            '<button type="button" class="btn small quiet" data-more aria-expanded="false" hidden>Show all</button></div>'
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


def _chat_reply_form(ctx, jid, lead):
    message = (f'<label class="sr" for="reply-{_text(jid)}">The reply from ChatGPT</label>'
               f'<textarea class="c-text" id="reply-{_text(jid)}" name="text" rows="4" required '
               'placeholder="Paste the reply from ChatGPT"></textarea>')
    return _composer_form(ctx, "/reply", "Save reply", message, job=jid, controls=_worker_note("GPT Pro"),
                          variant="reply-box", primary=lead)


def _chat_card(ctx, jid, spec, item, lead, include_reply=True):
    """A GPT Pro chat waiting on you: the prompt to copy, the way to ChatGPT, and the box its reply goes in."""
    model = (spec.get("model") or {}).get("requested") if isinstance(spec.get("model"), dict) else None
    when = (_when(item["created"], ctx["now"], f"inbox:{item['id']}", "asked ")
            if item and _number(item.get("created")) else "")
    when_html = f'{_SEP_HTML}{when}' if when else ""
    out = [f'<p class="label">{_dot("warn")}Needs you{when_html}</p>'
           '<h3>Ask GPT Pro, then paste its reply</h3>'
           f'<p class="detail">Start a new chat with {_text(model or "GPT Pro")} in ChatGPT and paste this prompt. When '
           'it has answered, paste the whole reply below; the reply is the job\'s result.</p>'
           f'<pre class="prompt" id="prompt-{_text(jid)}" tabindex="0">{_text(spec.get("brief", ""))}</pre>']
    if not ctx["live"]:
        out.append(f'<p class="cli mono">rh chat prompt {_text(jid)} · rh chat reply {_text(jid)} &lt;file&gt;</p>')
        return f'<div class="ask-card chat-card">{"".join(out)}</div>'
    out.append(f'<div class="acts">{_copy("prompt-" + jid, "Copy prompt")}<a class="btn" href="{_CHATGPT}" '
               'target="_blank" rel="noopener noreferrer">Open ChatGPT<span aria-hidden="true">↗</span>'
               '<span class="sr"> (opens a new tab)</span></a></div>')
    if include_reply:
        out.append(_chat_reply_form(ctx, jid, lead))
    return f'<div class="ask-card chat-card">{"".join(out)}</div>'


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
    head = (f'<p class="label">{_dot("warn")}Needs you{_SEP_HTML}{when}</p><h3>{_text(ask)}</h3>'
            + (f'<p class="detail">{_text(detail)}</p>' if detail else ""))
    return f'<div class="ask-card">{head}{_ask_form(ctx, item, lead)}</div>'


def _pause(ctx, jid):
    return _popover(ctx, "/stop", [("job", jid), ("pause", "on")], "Pause job", "s",
                    _field("Why? (optional)", "reason", placeholder="Paused from the app"), "Pause job",
                    "quiet danger", "Nothing new starts for it until you resume it. Work already running continues.")


def _cancel(ctx, jid, stalled=False):
    # rh job cancel only marks a stalled job ended and stops nothing, so its note says so
    note = ("It ends here as cancelled. If its process still runs, end it yourself." if stalled else
            "It will not start. You can run it again later.")
    return _popover(ctx, "/job/cancel", [("job", jid)], "Cancel job", "c", _field("Why?", "reason", required=True),
                    "Cancel job", "quiet danger", note)


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
        out.append(f'<div class="route"><p>{_text(_platform(name))}: {_text(route["last_status"])}{_SEP_HTML}{retry}'
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
            rows.append(_kv("Heavy lease", "in use" + (f"{_SEP_HTML}{_span(round(age))}" if _number(age) else "")))
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


def _top(left, right=""):
    """The panel's title and page-specific actions."""
    body = left + (f'<div class="top-r">{right}</div>' if right else "")
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
    """Everything that waits on you, once each and newest first: a job that needs you (with its open items), the
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
    # newest first, as the Inbox lists it, so an entry holds one place in both
    return sorted(out, key=lambda e: (-(e["created"] if _number(e["created"]) else 0),
                                      -(e["item"]["id"] if e["item"] and _number(e["item"].get("id")) else 0)))


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
        about = (f'It does not list {_text(_env_of(item))}, so cloud jobs wait{_SEP_HTML}<span class="mono">codex cloud</span> '
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
        items = groups[key]  # a group shows all its rows, so its head counts nothing; the Inbox badge is the one count
        out.append(_section(f"g-{key}", heading, _rows(items), "", "group", hidden=not items, h="h3"))
    none = not jobs and not stop_rows
    empty = (f'<p class="empty no-jobs" data-region="no-jobs" data-sig="{_sig(none)}"{"" if none else " hidden"}>'
             '<b>No jobs yet.</b></p>')
    return empty + "".join(out)


def _lane(jobs, stop_rows, ctx, here=None):
    """The sidebar queue, grouped by next action and filterable by worker."""
    workers = "".join(f'<option value="{key}">{_text(name)}</option>' for key, name in _WORKERS)
    head = ('<div class="lane-head"><h2>Jobs</h2><select data-job-filter aria-label="Filter jobs by worker">'
            f'<option value="">All workers</option>{workers}</select></div>')
    listed = _job_list(jobs, stop_rows, ctx)
    if here:
        mark = f'data-key="job:{_text(here)}"'
        listed = listed.replace(mark, mark + ' aria-current="page"', 1)
    return f'<section class="jobs lane" aria-label="Jobs">{head}<div class="list">{listed}</div></section>'


def _coord(view, ctx, jobs):
    """The coordination system, out of sight: one line at the sidebar's foot that opens the check loop, Codex cloud's
    launches, this Mac's load and the heavy lease, and the seats."""
    loop = view.get("checks") if isinstance(view.get("checks"), dict) else None
    sup = view.get("supervisor") if isinstance(view.get("supervisor"), dict) else None
    mode = loop.get("mode") if loop else None
    quiet = bool(sup) and _number(sup.get("age_s")) and sup["age_s"] > _STALE
    if mode == "paused":
        word, dot = "Checks paused", "warn"
    elif mode in ("on", "other"):
        word, dot = "Checks on", "warn" if quiet else "live"
    else:
        word, dot = ("Checks", "warn" if quiet or not sup else "good")
    if loop and loop.get("failed"):
        dot = "bad"
    seats = [x for x in view.get("seats", []) if isinstance(x, dict) and x.get("name")]
    # the Live group above lists what runs, so the pill says only how the checks are
    said = f'{_dot(dot, dot == "live")}<span class="coord-status">{word}</span>'
    line = f'<span class="coord-line" data-region="coord" data-sig="{_sig(said)}">{said}</span>'
    parts = [f'<h2 class="side-h">Check loop</h2>{_checks(view, ctx)}']
    if ctx["live"]:
        paused = any(x.get("owner") == "app" for x in ctx["scopes"].get("route:codex-cloud", []))
        cloud = (_button(ctx, "/resume", [("route", "codex-cloud"), ("pause", "on")], "Resume Codex cloud", "",
                         "small", "Resume Codex cloud") if paused else
                 _popover(ctx, "/stop", [("route", "codex-cloud"), ("pause", "on")], "Pause Codex cloud", "",
                          _field("Why? (optional)", "reason", placeholder="Paused from the app"), "Pause Codex cloud",
                          "quiet small danger", "No new task starts in Codex cloud until you resume it.", "up left"))
        pace = view.get("pacing") if isinstance(view.get("pacing"), dict) else {}
        gap, hour = pace.get("min_gap_s", 60), pace.get("max_per_hour", 20)
        parts.append(f'<h2 class="side-h">Codex cloud</h2><p class="more">New tasks at least {_text(gap)} s apart, '
                     f'at most {_text(hour)} an hour.</p><div class="btns">{cloud}</div>')
    parts.append(_host(view, ctx))
    if seats:
        rows = []
        for seat in seats:
            tone, state, job, _ = _seat_state(str(seat["name"]), jobs)
            rows.append(f'<li>{_dot({"accent": "live", "amber": "warn"}.get(tone, "neutral"))}'
                        f'<span class="mono">{_text(seat["name"])}</span><span class="soft">{_text(state)}'
                        f'{_SEP_HTML + _text(job.id) if job else ""}</span></li>')
        parts.append(f'<h2 class="side-h">Seats</h2><ul class="seat-list">{"".join(rows)}</ul>')
    return (f'<details class="coord"><summary aria-label="Coordination: the check loop, Codex cloud, {HERE} and the '
            f'seats">{line}</summary><div class="coord-panel">{"".join(parts)}</div></details>')


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


def _worker_note(label, title="Worker"):
    return f'<span class="ghost model-note" title="{_text(title)}">{_text(label)}</span>'


def _repository_input(value=""):
    return ('<label>Repository input<input type="text" name="input_path" autocomplete="off" spellcheck="false" '
            f'class="mono" placeholder="notes/source.md" value="{value}"></label>')


def _composer_form(ctx, action, label, message, *, job=None, worker="", options="", opened="", controls="",
                   variant="", primary=True, followup=False, where="", files=False, picked=""):
    """Render the shared message field, controls and submit button for each supported action."""
    name = picked.rsplit("/", 1)[-1]
    add = (f'<button class="ghost add-input" type="button" data-add-input aria-label="Attach a repository file" '
           f'title="Attach a repository file" aria-haspopup="dialog">{_ic("plus", True)}</button>'
           f'<input type="hidden" name="input_path" value="{_text(picked)}">'
           f'<span class="file-chip" title="{_text(picked)}"{"" if picked else " hidden"}>{_ic("file", True)}'
           f'<span class="file-name">{_text(name)}</span><button type="button" class="file-x" data-clear-input '
           f'aria-label="Remove the attached file">×</button></span>') if files else ""
    opts = (f'<details class="opts"{opened}><summary class="ghost opts-btn" aria-label="Options" title="Options">'
            f'{_ic("coord", True)}</summary>'
            f'<div class="opts-body">{options}</div></details>') if options else ""
    send = ' data-followup' if followup else ''
    again = _ic("again", True).replace('class="ic sm"', 'class="ic sm again"') if followup else ""
    cls = _cls("primary send" if primary else "send")
    return (f'<form class="compose composer{(" " + variant) if variant else ""}" method="post" action="{action}" '
            f'data-act data-label="{label}">{_hidden(ctx, [("job", job)] if job else [])}{message}'
            f'<div class="c-row">{add}{worker}<div class="compose-tools">{controls}{opts}'
            f'<button class="{cls}" aria-label="{label}" title="{label}"{send}>{_ic("up")}{again}</button></div></div>'
            f'{where}</form>')


_MODEL_NAMES = {"gpt-6.1-sol": "GPT-6.1 Sol", "gpt-6.1-terra": "GPT-6.1 Terra",
                "gpt-6.1-sol-pro": "GPT-6.1 Sol Pro", "gpt-6-pro": "GPT-6 Pro",
                "claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5",
                "claude-haiku-5-5": "Claude Haiku 5.5", "claude-fable-5-2": "Claude Fable 5.2",
                "swe-2-high": "SWE-2 High", "swe-2-max": "SWE-2 Max", "swe-2-flash-high": "SWE-2 Flash",
                "fusion-claude-fable-5-2-medium-sidekick-swe-2-high": "Fable 5.2 Medium + SWE-2 High",
                "fable-5-2-medium": "Fable 5.2 Medium"}
# Devin's chooser groups its models (Fusion first, with what Fusion is) and tags each with its cost; so does this one.
_DEVIN_FUSION = "Pairs frontier intelligence with cost-efficient execution to deliver frontier-level results at lower cost."


def _devin_tags(key):
    if key.startswith("fusion-"):
        lead = key.removeprefix("fusion-").split("-sidekick-")
        row = " + ".join(_MODEL_NAMES.get(part.removeprefix("claude-"), part) for part in lead)  # lead + sidekick
        return (f' data-group="Fusion · API" data-group-tip="{_DEVIN_FUSION} Lead + sidekick. Billed at API pricing: the '
                f'lead per token, the SWE-2 sidekick free." data-row="{_text(row)}"')
    # each section says its cost once in its head, 'Fusion · API' and 'SWE-2 · Free', and no row carries a chip
    return ' data-group="SWE-2 · Free" data-group-tip="SWE-2 models use no quota"'


_STEER_TIPS = {"steer": "Steered the active turn",
               "queue": "Originally queued, then promoted to steer the active turn"}


def _talk_said(talk):
    """What the owner said to a running agent, as their messages in the chat (T3 Code's timeline): a steer that
    landed has a 'Steered' caption under it, set like a timestamp; a queued message that started its turn reads
    plainly; one the run ended before says so. What still waits is Up next, not here; a removed one is gone."""
    out = []
    for m in talk:
        state = m.get("state")
        if state == "removed" or (m.get("mode") == "queue" and state in ("waiting", "queued")):
            continue
        if m.get("mode") == "stop":  # not a message: a line where it was asked, as T3 Code's lifecycle rows
            word = "Interrupt requested" if state == "waiting" else "You stopped the run"
            out.append(f'<p class="talk-stop">{_ic("stop", True)}{word}</p>')
            continue
        said = f'<span class="um-t">{_text(_reflow(str(m.get("text") or "")))}</span>'
        if state == "steered":
            tip = _STEER_TIPS.get(m.get("mode"), _STEER_TIPS["steer"])
            out.append(f'<div class="talk-steered"><div class="user-message">{said}</div>'
                       f'<p class="um-cap" title="{tip}">{_ic("up", True)}Steered</p></div>')
        elif state == "dropped":
            out.append(f'<div class="talk-dropped"><div class="user-message">{said}</div>'
                       '<p class="um-note">Not sent: the run ended first</p></div>')
        else:  # sent (it started the next turn), or a steer the agent has not read yet
            out.append(f'<div class="user-message{" sending" if state == "waiting" else ""}">{said}</div>')
    return "".join(out)


def _up_next(talk):
    """T3 Code's queued rows: what waits for the turn to end, numbered, each with Edit, Steer and Remove; past three,
    the first three and 'n more'. Empty, the region stays (hidden), so the page's regions do not change
    when one is queued and the composer keeps its draft."""
    queued = [m for m in talk if m.get("mode") == "queue" and m.get("state") in ("waiting", "queued")]
    n, rows = len(queued), []
    for i, m in enumerate(queued, 1):
        more = " un-more" if n > 3 and i > 3 else ""
        rows.append(f'<li class="un-row{more}" data-id="{_text(m["id"])}" aria-label="Queued message {i} of {n}, not sent">'
                    f'<span class="un-i">{i}</span><button type="button" class="un-t" data-q-edit title="Edit in the composer">'
                    f'{_text(str(m.get("text") or ""))}</button>'
                    '<span class="un-editing" hidden>Editing in the composer</span><span class="un-acts">'
                    f'<button type="button" class="ghost un-b" data-q-edit aria-label="Edit queued message" '
                    f'title="Edit in the composer">{_ic("pencil", True)}</button>'
                    f'<button type="button" class="ghost un-b un-steer" data-q="promote" aria-label="Steer" '
                    f'title="Send as a steer instead">{_ic("up", True)}</button>'
                    f'<button type="button" class="ghost un-b" data-q="remove" aria-label="Remove queued message" '
                    f'title="Remove from queue">{_ic("x", True)}</button></span></li>')
    every = (f'<button type="button" class="ghost un-all" data-q-all aria-expanded="false" '
             f'data-more="{n - 3} more">{n - 3} more</button>') if n > 3 else ""
    body = (f'<p class="un-head"><span>Up next</span><span class="un-n"> · {n}</span></p>'
            f'<ol class="un-list">{"".join(rows)}</ol>{every}') if n else ""
    return _region("j-up-next", body, "section", "up-next", ' aria-label="Up next"' + ("" if n else " hidden"))


def _talk_form(ctx, jid, who="the agent"):
    """The running job's composer, after T3 Code's: Enter steers (joins the turn), Ctrl/⌘ with it queues (the next
    turn), and empty its button is Stop. The script posts it to /job/message; an edit of a queued row sends update."""
    # the app's own glyphs (owner: not T3's square and arrows): its stop ring, the send arrow every composer has, a clock
    icons = "".join(_ic(name).replace('class="ic"', f'class="ic t-{state}"')
                    for name, state in (("stop", "stop"), ("up", "steer"), ("clock", "queue"), ("check", "update")))
    return (f'<form class="compose composer talk" method="post" action="/job/message" data-message data-state="stop">'
            f'{_hidden(ctx, [("job", jid), ("mode", "steer"), ("target", "")])}'
            f'<label class="sr" for="talk-{_text(jid)}">Message the agent</label>'
            f'<textarea class="c-text" id="talk-{_text(jid)}" name="text" rows="1" placeholder="Steer {_text(who)}…"></textarea>'
            '<div class="c-row"><div class="compose-tools">'
            '<button type="button" class="ghost talk-cancel" data-q-cancel hidden '
            'aria-label="Cancel editing queued message">Cancel</button>'
            '<button class="btn primary send talk-send" aria-label="Stop generation" title="Interrupt" '
            f'data-tip="Click to steer, Ctrl/⌘-click to queue">{icons}</button></div></div></form>')


def _model_picker(policy, kind, draft, only=False):
    """The model and effort pickers for a route: every route's, with all but `kind` hidden (the New job composer), or
    only `kind`'s (`only`, the reply box under a job)."""
    draft = draft if isinstance(draft, dict) else {}
    # Codex on this Mac follows the same dated policy as Codex cloud: its models, at standard speed.
    # This Mac runs any provider's agent; each group is a provider, each model carries its mark.
    codex_models = list(policy.get("codex-cloud", {}).get("models", ()))
    providers = (("openai", "OpenAI", codex_models),
                 ("claude", "Anthropic", list(policy.get("claude-local", {}).get("models", ()))),
                 ("devin", "Cognition", sorted(policy.get("devin-local", {}).get("models", ()),
                                               key=lambda key: not key.startswith("fusion-"))))  # Fusion first, as Devin
    routes = {"gpt-chat": [("openai", "", list(policy.get("gpt-chat", {}).get("models", ())))],
              "codex-cloud": [("openai", "", codex_models)],
              "local-agent": [p for p in providers if p[2]]}
    if only:  # the reply box under a job: only the route the job runs on
        routes = {kind: routes.get(kind, [])}
    # Reasoning effort, like Codex's: the levels a provider's CLI knows, of which the dated policy allows some.
    effort_tables = {("gpt-chat", "openai"): "gpt-chat", ("codex-cloud", "openai"): "codex-cloud",
                     ("local-agent", "openai"): "codex-local", ("local-agent", "claude"): "claude-local"}

    def effort_levels(route, prov):
        levels = [key for key, _ in _EFFORTS if key != "ultra" or prov == "openai"] if prov != "devin" else []
        table = effort_tables.get((route, prov))
        allowed = set(policy.get(table, {}).get("efforts", ())) if table else set()
        return levels, [key for key in levels if key in allowed]

    models, controls, picked_name, picked_effort, effort_selects = [], [], "", "", []
    for route, groups in routes.items():
        disabled = " disabled hidden" if route != kind else ""
        rules = policy.get(route, {})
        entries = [key for _, _, keys in groups for key in keys]
        if entries:
            pick = draft.get("model") if draft.get("model") in entries else entries[0]

            def opts(prov, keys):
                levels, allowed = effort_levels(route, prov)
                return "".join(f'<option value="{_text(key)}" data-prov="{prov}" data-levels="{" ".join(levels)}" '
                               f'data-efforts="{" ".join(allowed)}"{_devin_tags(key) if prov == "devin" else ""}'
                               f'{" selected" if pick == key else ""}>'
                               f'{_ic(prov, True)}{_text(_MODEL_NAMES.get(key, key))}</option>' for key in keys)
            values = "".join(f'<optgroup label="{label}">{opts(prov, keys)}</optgroup>' if label else opts(prov, keys)
                             for prov, label, keys in groups if keys)
            # the effort that goes with the chosen model: the draft's if its provider allows it, else the highest
            pick_prov = next(prov for prov, _, keys in groups if pick in keys)
            allowed = effort_levels(route, pick_prov)[1]
            if allowed:
                effort = draft.get("effort") if draft.get("effort") in allowed else allowed[-1]
                levels = "".join(f'<option value="{key}"{" selected" if effort == key else ""}>{word}</option>'
                                 for key, word in _EFFORTS)
                effort_selects.append(f'<select class="effort-pick" name="effort" data-route="{route}" '
                                      f'aria-label="Reasoning effort"{disabled}>{levels}</select>')
            if route == kind:
                picked_name = _MODEL_NAMES.get(pick, pick)
                picked_effort = dict(_EFFORTS).get(effort, "") if allowed else ""
            models.append(f'<select class="ghost model-pick" data-route="{route}" name="model" aria-label="Model" '
                          f'title="Model"{disabled}>{values}</select>')
    prov_marks = "".join(f'<span class="prov" data-prov="{prov}" aria-hidden="true">{_ic(prov, True)}</span>'
                         for prov in ("openai", "claude", "devin"))
    # The model picker after T3 Code's: the button shows the provider's mark and the model; the script opens a panel
    # with a rail of providers (Cognition for Devin), a search over every model and one row per model. The selects
    # stay underneath as the form's values.
    rail_marks = "".join(f'<span data-mark="{m}">{_ic(m, True)}</span>' for m in ("openai", "claude", "devin", "cognition"))
    trigger = (f'<button type="button" class="mp-trigger" aria-haspopup="dialog" aria-expanded="false" '
               f'aria-label="Model" title="Model">{prov_marks}<span class="mp-name">{_text(picked_name)}</span>'
               f'{_ic("chev", True)}</button><template class="mp-marks">{rail_marks}</template>')
    effort_picker = ('<div class="effort-picker">' + "".join(effort_selects)
                     + f'<button type="button" class="ef-trigger" aria-haspopup="dialog" aria-expanded="false" '
                       f'aria-label="Reasoning effort" title="Reasoning effort"{"" if picked_effort else " hidden"}>'
                       f'{_ic("effort", True)}<span class="ef-name">{picked_effort}</span>{_ic("chev", True)}</button></div>')
    worker_picker = f'<div class="model-picker">{trigger}' + "".join(models) + '</div>' + effort_picker
    return worker_picker


def _gpt_name(m):
    """A GPT model the table does not know yet, in the table's words: gpt-6.2-astra is GPT-6.2 Astra."""
    found = re.fullmatch(r"gpt-(\d+(?:\.\d+)?)-([a-z]+(?:-[a-z]+)*)", m)
    return f"GPT-{found.group(1)} {found.group(2).replace('-', ' ').title()}" if found else m


def _names(models):
    """Model names in words: "SWE-2 Max", "SWE-2 Max and SWE-2 Flash", "A, B and C"."""
    # a Fusion model is named by its pair, as the picker names it: which lead and which sidekick run
    names = [" + ".join(_MODEL_NAMES.get(part.removeprefix("claude-"), part)
                        for part in m.removeprefix("fusion-").split("-sidekick-")) if m.startswith("fusion-")
             else _MODEL_NAMES.get(m.removeprefix("claude-"), _MODEL_NAMES.get(m, _gpt_name(m))) for m in models]
    return " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " and " + names[-1]


def _slash(ctx):
    """The composer's commands: typing / lists them, and a brief that starts with one says what it will do."""
    swarm = ctx.get("swarm") or {}
    models = [m for m in swarm.get("models") or [] if isinstance(m, str)]
    judge = swarm.get("judge") or (models[0] if models else "")
    if not models:
        return ""
    does = f"Runs on {_names(models)} at once; {_names([judge])} keeps the best work."
    return ('<div class="slash" data-slash role="listbox" aria-label="Commands" hidden>'
            '<button type="button" class="slash-o" role="option" data-slash-cmd="/swarm ">'
            f'{_ic("swarm", True)}<span class="slash-n mono">/swarm</span><span class="slash-d">{_text(does)}</span>'
            f'</button></div>')


def _swarm_rows(swarm, form=""):
    """The swarm's model ticks grouped by provider, with the glyphs the model picker uses and a check of the app's
    own, and the judge's options; with a form id, each field belongs to that form wherever it is drawn."""
    choices = [m for m in swarm.get("choices") or [] if isinstance(m, str)]
    ticked, judge = set(swarm.get("models") or []), swarm.get("judge") or ""
    owner = f' form="{form}"' if form else ""
    groups = {}
    for m in choices:
        groups.setdefault("claude" if m.startswith("claude-") else "devin", []).append(m)
    def row(prov, m):
        # box, glyph, name in every list that draws these rows: Settings and the composer's popover alike (r27 F12)
        return (f'<label class="swarm-m"><input type="checkbox" name="models" value="{_text(m)}"'
                f'{" checked" if m in ticked else ""}{owner} data-name="{_text(_names([m]))}">'
                f'<span class="swarm-x" aria-hidden="true"></span>{_ic(prov, True)}'
                f'<span class="swarm-n">{_text(_names([m]).removeprefix("Claude "))}</span></label>')
    parts = []
    for prov, models in groups.items():
        if prov != "devin":
            parts.append(f'<p class="swarm-g">{_AGENT_WHO.get(prov, (prov.capitalize(),))[0]}</p>')
            parts += [row(prov, m) for m in models]
            continue
        # Devin's models under the picker's heads and in its order, peers of the Claude head (each row wears the
        # Devin glyph): Fusion, which costs and says so, then the free SWE-2 models
        for head, picked in (("Fusion · API", [m for m in models if m.startswith("fusion-")]),
                             ("SWE-2 · Free", [m for m in models if m.startswith("swe-2")]),
                             ("Devin", [m for m in models if not m.startswith(("fusion-", "swe-2"))])):
            if picked:
                parts.append(f'<p class="swarm-g">{_text(head)}</p>')
            parts += [row(prov, m) for m in picked]
    boxes = "".join(parts)
    judges = "".join(f'<option value="{_text(m)}"{" selected" if m == judge else ""}>{_text(_names([m]))}</option>'
                     for m in choices)
    judge_row = (f'<label class="swarm-judge"><span>Judge</span><span class="swarm-sel"><select name="judge"{owner}>'
                 f'{judges}</select>{_ic("chev", True)}</span></label>')
    return boxes, judge_row


_SWARM_PICK = "swarm-pick-f"
_TEAM_PICK = "team-pick-f"


def _swarm_note(ctx):
    """What a /swarm brief will do, said where the model picker stands, which steps aside for it. On a live page
    the line is that picker's stand-in: it opens the swarm's ticks and judge, saved as they change."""
    swarm = ctx.get("swarm") or {}
    models = [m for m in swarm.get("models") or [] if isinstance(m, str)]
    if not models:
        return ""
    judge = swarm.get("judge") or models[0]
    chip = (f'<span class="swarm-chip">{_ic("swarm", True)}Swarm'
            '<button type="button" class="swarm-off" data-swarm-off aria-label="Turn swarm off" title="Turn swarm off">'
            '×</button></span>')
    ran = _swarm_line([_names([m]) for m in models])
    judged = f"judge {_names([judge])}"
    quick_says = f'{_text(ran)}{_SEP_HTML}{_text(judged)}'
    team = _active_team(swarm)
    says = (f'{_text(team["name"])}{_SEP_HTML}{_team_size(team)} agents' if team else quick_says)
    if ctx["live"] and swarm.get("choices"):
        boxes, judge_row = _swarm_rows(swarm, _SWARM_PICK)
        teams = ""
        if swarm.get("team_models"):  # the teams Settings keeps, picked here; Quick swarm is the ticks below
            data = _team_presets(swarm)
            # each radio carries its line as plain text, 'A · B'; team.js puts the dot back as the page draws it
            rows = [("quick", "Quick swarm", ran, f"{ran} · {judged}")] + [
                (p["id"], p["name"], _team_sub(p),
                 f'{p["name"]} · {_team_size(p)} agents')
                for p in data["presets"] if isinstance(p, dict) and p.get("id")]
            teams = ('<div class="swarm-teams" role="radiogroup" aria-label="Team" data-team-pick>'
                     + "".join(f'<label class="swarm-team"><input type="radio" name="active" value="{_text(pid)}" '
                               f'form="{_TEAM_PICK}"{" checked" if pid == (team["id"] if team else "quick") else ""} '
                               f'data-says="{_text(said)}"><span class="swarm-team-n">{_text(name)}</span>'
                               f'<span class="swarm-team-d">{_text(sub)}</span></label>'
                               for pid, name, sub, said in rows)
                     + f'<button type="button" class="swarm-team-edit" data-team-open>{_ic("swarm", True)}'
                     'Edit teams</button></div>')
        return (f'<div class="swarm-note" data-swarm-note hidden>{chip}<details class="swarm-pick">'
                f'<summary aria-label="Swarm models" title="Swarm team and models"><span class="swarm-says" '
                f'data-swarm-says="{_SWARM_PICK}">{says}</span>{_ic("chev", True)}</summary>'
                f'<div class="swarm-pop" data-team="{"quick" if not team else "preset"}">{teams}'
                f'<div class="swarm-quick"><div class="swarm-models">{boxes}</div>'
                f'<div class="swarm-opts in-pop">{judge_row}</div></div></div></details></div>')
    return f'<p class="swarm-note" data-swarm-note hidden>{chip}<span class="swarm-says">{says}</span></p>'


def _swarm_pick_form(ctx):
    """The form the composer's swarm popover saves through: outside the composer's own form, so its fields never
    go out with a job, and carrying the check command, which the settings route replaces with the rest."""
    swarm = ctx.get("swarm") or {}
    if not (ctx["live"] and swarm.get("choices") and swarm.get("models")):
        return ""
    return (f'<form id="{_SWARM_PICK}" action="/settings/swarm" method="post" data-swarm-form hidden>'
            f'{_hidden(ctx, [("check", swarm.get("check") or "")])}</form>'
            + (f'<form id="{_TEAM_PICK}" action="/settings/swarm-active" method="post" data-team-pick-form hidden>'
               f'{_hidden(ctx, [])}</form>' if swarm.get("team_models") else ""))


def _composer(ctx, draft, ready, policy):
    """The one control that starts a job: what it should do, where it runs, its name, and the rest folded away."""
    draft = draft if isinstance(draft, dict) else {}
    value = lambda name: _text(draft.get(name, ""))  # noqa: E731
    kind = draft.get("kind") or ("codex-cloud" if ready else "local-agent")
    if kind not in ("gpt-chat", "local-agent", "local-shell", "codex-cloud"):
        kind = "local-agent"
    cloud = "Codex cloud" if ready else f"Codex cloud{_SEP}setup required"
    workers = (("gpt-chat", "GPT Pro", "chat"), ("local-agent", HERE, "mac"), ("codex-cloud", cloud, "cloud"),
               ("local-shell", "Shell", "term"))
    options = "".join(f'<option value="{key}"{" selected" if kind == key else ""}>{_ic(icon, True)}{_text(name)}</option>'
                      for key, name, icon in workers)
    have = set(draft.get("needs", [])) if isinstance(draft.get("needs"), list) else set()
    needs = "".join(f'<label class="need"><input type="checkbox" name="needs" value="{n}"{" checked" if n in have else ""}>'
                    f'{_NEED_WORDS[n]}</label>' for n in _NEEDS)
    timeout = value("timeout_min") or "120"
    opened = " open" if draft else ""
    worker_picker = _model_picker(policy, kind, draft)
    controls = []
    marks = "".join(f'<span class="wk-ic" data-for="{key}">{_ic(icon, True)}</span>' for key, _, icon in workers)
    runner = (f'<div class="worker-picker">{marks}<select class="ghost" name="kind" aria-label="Where it runs" '
              f'title="Where it runs">{options}</select></div>')
    message = ('<label class="sr" for="brief">What should the job do?</label>'
               '<textarea class="c-text" id="brief" name="brief" rows="1" required '
               f'placeholder="Describe a coding or research job…">{value("brief")}</textarea>' + _slash(ctx))
    minutes = [15, 30, 60, 120, 240, 480]
    if timeout.isdigit() and int(timeout) not in minutes:
        minutes = sorted(minutes + [int(timeout)])
    spans = "".join(f'<label class="seg"><input type="radio" name="timeout_min" value="{m}"'
                    f'{" checked" if str(m) == timeout else ""}><span>{_minutes(m)}</span></label>' for m in minutes)

    def row(cls, icon, label, val, hint, body):
        return (f'<details class="op-row{(" " + cls) if cls else ""}"><summary>{_ic(icon, True)}<span class="op-label">'
                f'{label}</span><span class="op-val" data-op-val>{val}</span>{_ic("chev", True)}</summary>'
                f'<div class="op-edit"><p class="op-hint">{hint}</p>{body}</div></details>')

    answer = draft.get("answer") if draft.get("answer") in ("chat", "file") else "chat"
    places = "".join(f'<label class="seg"><input type="radio" name="answer" value="{key}"'
                     f'{" checked" if answer == key else ""}><span>{word}</span></label>' for key, word in _ANSWER_WORDS)
    # Two groups: what you get back, and how the job runs. Each row says its current value in words.
    fields = (
        '<p class="op-title answer-option">Result</p>'
        + row("answer-option", "chat", "Answer", dict(_ANSWER_WORDS)[answer],
              "Where the agent's reply goes. In the chat shows it on the job page as a message; as answer.md keeps "
              "it as a file you open.", f'<div class="segs">{places}</div>')
        + '<p class="op-title">Run</p>'
        + row("", "clock", "Time limit", _minutes(int(timeout)) if timeout.isdigit() else "2 h",
              "The job is stopped if it is still running after this long.", f'<div class="segs">{spans}</div>')
        + row("accept-option", "flask", "Test when done", _text(draft.get("accept", "")) or "Don't test",
              "A command to run after the job, for example pytest -q. The job only counts as done if it passes.",
              '<input type="text" name="accept" autocomplete="off" spellcheck="false" class="mono" '
              f'placeholder="pytest -q" value="{value("accept")}">')
        + row("", "plug", "Requires", ", ".join(_NEED_WORDS[n] for n in _NEEDS if n in have) or "Nothing special",
              "The job only starts on a machine that has everything ticked here.", f'<div class="needs">{needs}</div>')
        + row("cmd", "term", "Command", _text(draft.get("command", "")) or "Run the brief",
              "What the shell runs. Leave empty to run the brief as typed.",
              '<input type="text" id="command" name="command" class="mono" autocomplete="off" spellcheck="false" '
              f'placeholder="make test" value="{value("command")}">')
        + row("", "tag", "Job name", _text(draft.get("id", "")) or "From the brief",
              "How the job is listed. Leave empty to name it from the brief.",
              '<input type="text" name="id" autocomplete="off" spellcheck="false" '
              'pattern="[a-z0-9][a-z0-9._\\-]{0,62}" '
              f'placeholder="fix-login-bug" value="{value("id")}">'))
    workspace = ctx["workspace"]
    # the repository heads the page, so the line under it names only the branch
    where = "".join(f'<span class="where-chip" title="{title}">{_ic(icon, True)}<span class="wc-t">{_text(workspace[key])}</span></span>'
                    for key, icon, title in (("branch", "branch", "Branch"),) if workspace.get(key))
    # the folder the title's menu picked; empty works in the configured repository
    folder = (f'<input type="hidden" name="folder" value="{value("folder")}">' if ctx.get("folders") is not None
              else "")
    form = _composer_form(ctx, "/job", "Start job", message, worker=worker_picker + _swarm_note(ctx) + folder,
                          options=fields, opened=opened,
                          controls="".join(controls), where=f'<div class="where-bar">{runner}{where}</div>', files=True,
                          picked=draft.get("input_path", "") if isinstance(draft.get("input_path"), str) else "")
    body = f'<div class="body">{form}{_swarm_pick_form(ctx)}</div>'
    return (f'<div class="s-compose" data-region="compose" data-key="section:compose" '
            f'data-sig="{_sig("compose", body)}">{body}</div>')


def _folder_title(ctx, current):
    """The composer's title as the folder a This Mac job works in, with a menu of the folders jobs used, the
    configured repository and Choose folder… (a native dialog in the app, a typed path in a browser). The server
    takes only a git working tree; cloud jobs stay on the configured repository."""
    name = Path(current).name if current else "Choose a folder"  # none configured: the menu is how to pick one
    home = str(Path.home())

    def where(path):  # the folder's parent, as ~/Projects: its own name is already the row's name
        parent = str(Path(path).parent)
        return "~" + parent[len(home):] if parent == home or parent.startswith(home + "/") else parent
    rows = "".join(
        f'<button type="button" class="fm-row" role="menuitemradio" aria-checked="{str(path == current).lower()}" '
        f'data-folder="{_text(path)}" title="{_text(path)}">{_ic("folder", True)}'
        f'<span class="fm-n">{_text(fname)}</span><span class="fm-p">{_text(where(path))}</span>'
        + ('<span class="fm-def">Default</span>' if path == ctx["repo_path"] else "")
        + f'{_ic("check", True)}</button>' for path, fname in ctx["folders"])
    # the title and its menu share one box the title's size, so the menu opens off the title on any layout
    return (f'<div class="folder-pick"><h1 class="compose-title" id="compose-title" tabindex="-1" '
            f'data-repo="{_text(name)}"><button type="button" class="folder-btn" aria-haspopup="menu" aria-expanded="false" '
            f'aria-controls="folder-menu" title="{_text(current)}" data-home="{_text(ctx["repo_path"])}">'
            f'<span class="fb-glyph">{_ic("folder", True)}</span><span class="fb-name">{_text(name)}</span>'
            f'{_ic("chev", True)}</button><span class="ct-research">What should we find out?</span></h1>'
            f'<div class="folder-menu" id="folder-menu" role="menu" aria-label="Working folder" hidden>'
            f'<p class="fm-head">Work in</p>{rows}<span class="fm-rule" role="separator"></span>'
            f'<button type="button" class="fm-row fm-choose" role="menuitem" data-folder-choose>{_ic("plus", True)}'
            '<span class="fm-n">Choose folder…</span></button>'
            '<label class="fm-path" hidden><input type="text" class="mono" aria-label="Path to a git folder" '
            'placeholder="~/Projects/app" autocomplete="off" spellcheck="false">'
            '</label></div></div>')


def _compose_strip(ctx, view, focus=False):
    """The same form opens in the center for a new job and docks below the working view."""
    form = _composer(ctx, view.get("compose"), ctx["ready"], view.get("job_policy") or {})
    if focus:
        form = form.replace('<textarea class="c-text" id="brief"', '<textarea class="c-text" id="brief" autofocus', 1)
    # A coding job works in this repository, so its name heads the page; with none known, the page asks.
    repo = str((ctx.get("workspace") or {}).get("repo") or "")
    draft = view.get("compose") if isinstance(view.get("compose"), dict) else {}
    picked = str(draft.get("folder") or "").strip()
    if ctx.get("folders") is not None and ctx["live"]:
        current = picked or (ctx["repo_path"] if repo else "")
        title = _folder_title(ctx, current)
        repo = Path(current).name if current else ""
    else:
        title = (f'<h1 class="compose-title" id="compose-title" tabindex="-1" data-repo="{_text(repo)}">{_text(repo)}'
                 '</h1>' if repo else
                 '<h1 class="compose-title" id="compose-title" tabindex="-1">What are we building?</h1>')
    # On its own page the composer names the keys it answers to; the shortcuts sheet lists them for a reader.
    hint = (f'<p class="compose-hint" aria-hidden="true"><kbd>/</kbd> commands{_SEP_HTML}'
            '<kbd>⌘K</kbd> jump to anything</p>' if focus else "")
    return (f'<section class="c-strip{" has-repo" if repo else ""}" aria-labelledby="compose-title">'
            f'<div class="compose-intro">{title}</div>' + form + hint + '</section>')


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
        status = (_SEP_HTML.join(_text(x) for x in ("Paused" if j.row.get("paused") else "", j.word,
                                                    _plain(j.row.get("detail"))) if x) if j.items else "")
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
    parts.append(_section("today", "Today", f'<p>{counts["live"]} live{_SEP_HTML}{counts["next"]} up next{_SEP_HTML}'
                                            f'{len(view.get("recent", []))} recently finished</p>'
                                            f'<p class="soft">Last 24 h: {day or "nothing finished"}</p>'))
    return f'<aside class="rail" aria-label="What needs you">{"".join(parts)}</aside>'


def _home(jobs, stop_rows, loose_cards, view, ctx, feedback, heading=""):
    """Today: what needs you first, then the rest of the jobs, under the composer as one line; on the right, the sea,
    where each seat is a jellyfish. A printed snapshot has neither, and lists on the right what needs a person, each
    with the command that settles it."""
    when = (f'<h1 tabindex="-1">Today</h1><span class="top-meta mono"><span class="when">{_date(ctx["now"])}'
            '</span></span>')
    listed = f'<div class="list">{_job_list(jobs, stop_rows, ctx)}</div>'
    if not ctx["live"]:
        jobs_head = '<div class="jobs-head"><h2 id="jobs-h">Jobs</h2></div>'
        return (_top(when) + feedback + f'<div class="page mc"><div class="center"><section class="jobs" '
                f'aria-labelledby="jobs-h">{jobs_head}{listed}</section></div>'
                f'{_snapshot_rail(jobs, loose_cards, view, ctx)}</div>')
    strip = _compose_strip(ctx, view, bool(heading)) if ctx["can_add"] else ""
    return (feedback + f'<div class="page sea-page">{_reef(ctx, view, jobs)}'
            f'<div class="dock">{strip}</div></div>')


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
    if str(code) == "127":  # the shell's own code for a program it cannot find
        word = str(cmd).split()[0] if str(cmd).split() else str(cmd)
        return f"there is no command named {word} on this Mac"
    if str(code) == "126":
        return "the command was found but could not be run"
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
    if finished:
        stamp = [_platform(receipt.get("route") or job.get("kind") or spec.get("kind", ""))]
        if job.get("pinned_sha"):
            stamp.append(str(job["pinned_sha"])[:7])
        if receipt.get("model_served"):
            stamp.append(f"model {receipt['model_served']}")
        if receipt.get("diff_sha256"):
            stamp.append(f"diff {str(receipt['diff_sha256'])[:12]}")
        stamp.append(jid)
        parts.append(f'<p class="stamp mono">{_SEP_HTML.join(_text(s) for s in stamp)}</p>')
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
    word = _CHAT_WAIT[0] if kind == "gpt-chat" and shown == "handoff" else _STATES.get(shown, (shown,))[0]
    rows = [("Job", f'<span class="mono">{_text(job.get("id", ""))}</span>'),
            ("State", f'{_ring(shown, kind=kind)}<span>{_text(word)}</span>'),
            ("Runs on", f'{_ic({"local-shell": "mac", "gpt-chat": "chat"}.get(kind, "cloud"), True)}'
                        f'<span>{_text(_platform(kind))}</span>')]
    seat = str(spec.get("seat") or "")
    if seat:
        card = next((s for s in view.get("seats", []) if isinstance(s, dict) and s.get("name") == seat), {})
        rows.append(("Seat", f'<span class="mono">{_text(seat)}</span>'
                     + (f'<span class="mono dim">{_text(card["model"])}</span>' if card.get("model") else "")))
    for key, label in (("created", "Added"), ("started", "Started"), ("updated", "Updated")):
        if _number(job.get(key)) and not (key == "updated" and entry.where != "recent"):
            rows.append(("Finished" if key == "updated" else label,
                         f'<span class="num">{_clock(job[key], now)}</span>', "prop-time"))
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
    body = '<h2>Properties</h2>' + "".join(
        f'<div class="prop {row[2] if len(row) > 2 else ""}"><span class="pk">{row[0]}</span>'
        f'<span class="pv">{row[1]}</span></div>' for row in rows)
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


def _job_head(title, kind, tip="", sub="", folder=""):
    """A compact, persistent title, named as the sidebar names the job (its id when another listed job shares its
    name, with the name on hover), and its worker crumb; `sub` is a second quiet line under it, when one is due."""
    tip = f' title="{_text(tip)}"' if tip else ""
    body = ('<div class="jd-head-in">'
            f'<h1 tabindex="-1" data-clear{tip}>{_text(title)}</h1>'
            f'<p class="job-worker" data-clear>{_SEP_HTML}<span>{_text(_platform(kind))}</span>'
            + (f'{_SEP_HTML}<span class="job-folder" title="{_text(folder)}">{_text(Path(folder).name)}</span>'
               if folder else "") + f'</p>{sub}</div>')
    return _region("j-head", body, "section", "jd-head")


def _said(provider, model, text, link="", inner=False, role="", status="", live=True, key=""):
    """An agent's answer as a chat message: who answered and on which model, then the answer rendered from Markdown.
    `role` names what the agent was (a swarm's "Judge") in place of its provider; `status` (the run's ring and status
    line) sits at the far end of the same line, so a turn opens with one line of chrome, not two. A live page adds a
    Copy under the answer, fed by a hidden textarea holding the Markdown as written; the snapshot has no script, so
    it gets no control. `key` gives the Copy its single-key shortcut — only the page's latest answer passes one."""
    who, mark = _AGENT_WHO.get(provider or "", ("Answer", "chat"))
    who = role or who
    name = _MODEL_NAMES.get(model, model) if model else ""
    head = (f'<p class="who" data-prov="{_text(provider or "")}">{_ic(mark, True)}{who}'
            + (f'<span class="who-model">{_text(name)}</span>' if name else "")
            + (f'<span class="who-run">{link}</span>' if link else "")
            + (f'<span class="who-status">{status}</span>' if status else "") + "</p>")
    body = f'{head}<div class="assistant-message md">{_markdown(text)}</div>'
    if live and text:
        aid = "ans-" + hashlib.sha1(text.encode()).hexdigest()[:10]
        k = f' data-k="{key}" aria-keyshortcuts="{key}"' if key else ""
        body += (f'<textarea hidden id="{aid}" readonly>{_text(text)}</textarea>'
                 f'<div class="msg-acts"><button type="button" class="msg-copy" data-copy="{aid}"{k} '
                 f'aria-label="Copy the answer">{_ic("copy", True)}<span class="pc-t">Copy</span></button></div>')
    return body if inner else f'<div class="msg ai">{body}</div>'


def _brief_copy(text):
    """A brief bubble's Copy on a live page: a hidden textarea holding the ask as written — the bubble shows it
    reflowed — and the row that copies it."""
    aid = "ask-" + hashlib.sha1(text.encode()).hexdigest()[:10]
    return (f'<textarea hidden id="{aid}" readonly>{_text(text)}</textarea>'
            f'<div class="msg-acts me"><button type="button" class="msg-copy" data-copy="{aid}" '
            f'aria-label="Copy the brief">{_ic("copy", True)}<span class="pc-t">Copy</span></button></div>')


def short_cmd(spec, cmd):
    """A failed command as a person reads it: an agent run is its CLI and model, not the agent.py line with the brief."""
    agent = agent_of(spec.get("command"))
    if agent and cmd != str(spec.get("accept") or ""):
        provider, model, _ = agent
        return f"{provider} --model {model}" if model else provider
    return cmd


def _fail_banner(state, spec, bad):
    """What failed, as the job page and the Inbox both say it: the sentence, then the command with its exit code."""
    what = "The done-when check" if spec.get("accept") and bad[0] == str(spec["accept"]) else "The command"
    found = _found(bad[0], bad[1])
    # an exit code says itself: "The command exited with code 1"
    sentence = (f"{what} exited with code {bad[1]}" if what == "The command" and found == f"it exited with code {bad[1]}"
                else f"{what} failed: {found}")
    return (f'<p class="fail-k">{_ring(state)}{_text(sentence)}</p>'
            f'<p class="fail-c"><code>{_text(short_cmd(spec, bad[0]))}</code><span class="exit mono">exit {_text(bad[1])}</span></p>')


def _swarm_line(names, judge=""):
    """The composer's swarm line: 'SWE-2 Max, Flash · judge SWE-2 Max', or the models alone with no judge (the Quick
    swarm's subtitle in the teams lists). A family every model shares is named once; a list that mixes families,
    or holds a Fusion pair, keeps whole names. Runs join with commas: '+' is Fusion's own. The judge keeps its
    whole name — a bare 'Max' reads as an effort. app.js's swarmLine writes the same line as the ticks change."""
    heads = {n.split(" ", 1)[0] for n in names}
    fam = next(iter(heads)) if len(heads) == 1 and all(" " in n and "+" not in n for n in names) else ""
    models = fam + " " + ", ".join(n[len(fam) + 1:] for n in names) if fam else ", ".join(names)
    return f"{models} · judge {judge}" if judge else models


def _swarm_again(ctx, runs):
    """An ended swarm's way to run the whole swarm again on the same brief: the composer's /job post with '/swarm '
    back in front of the first run's brief — the brief before any judging text — as the submit listener writes it,
    with the runs' time limit."""
    m = ctx["meta"].get(runs[0]) if runs else None
    m = m if isinstance(m, dict) else {}
    brief = str(m.get("brief") or "")
    if not brief.strip():
        return ""
    fields = [("brief", "/swarm " + brief)]
    limit = m.get("timeout_min")
    if _number(limit) and limit > 0:
        fields.append(("timeout_min", str(int(limit))))
    return (f'<form method="post" action="/job" data-act data-label="Swarm again">'
            f'{_hidden(ctx, fields)}'
            f'<button class="sw-again" data-k="w" aria-keyshortcuts="w">{_ic("swarm", True)}'
            '<span>Swarm again</span><kbd aria-hidden="true">W</kbd></button></form>')


def _swarm_verdict(kept, rid, runs):
    """One run's verdict under its judge's kept: "kept" for the run the judge kept whole, "lost" for one it passed
    over for a sibling, "combined" when the judge merged the runs and "none" when it kept none of them; "" before
    the judge has finished."""
    if kept in (rid, rid.rsplit(".", 1)[-1]):
        return "kept"
    if kept == "combined":
        return "combined"
    if kept:
        return "lost" if kept in {*runs, *(r.rsplit(".", 1)[-1] for r in runs)} else "none"
    return ""


def _swarm_run(ctx, jid):
    """A swarm run's own line under its page's head: 'Run 1 of 2 in a swarm', the judge's verdict on it — 'Kept',
    'Not kept' or 'Combined' once the judge has finished, 'Judge still working' before — and, on a live page, a
    way back to the swarm. "" for a job that is no swarm's run."""
    judge, runs = next(((j, rs) for j, rs in (ctx.get("swarms") or {}).items() if jid in rs), ("", []))
    if not judge:
        return ""
    m = ctx["meta"].get(judge)
    kept = str(m.get("kept") or "") if isinstance(m, dict) else ""
    verdict = _swarm_verdict(kept, jid, runs)
    word = {"kept": "Kept", "combined": "Combined"}.get(verdict)
    if not word:
        ended = ctx["states"].get(judge) in ("done", "cancelled", *_FAILED)
        word = "Not kept" if verdict or ended else "Judge still working"
    line = f"Run {runs.index(jid) + 1} of {len(runs)} in a swarm{_SEP_HTML}{word}"
    if ctx["live"]:
        line += f'{_SEP_HTML}<a href="{_url(ctx, "/job/" + judge)}" data-nav>Open swarm ›</a>'
    return f'<p class="job-swarm" data-clear>{line}</p>'


def _swarm_card(ctx, runs, checks=None, kept=None, stats=None, final=None):
    """A swarm's runs on its judge's page: each run's model, state, time, diff size, check verdict and first
    answer line, linked to its page; a judge that combined them gets the kept work's check verdict in the head."""
    now, items = ctx["now"], []
    checks = checks if isinstance(checks, dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    final = final if isinstance(final, str) else ""
    for rid in runs:
        m = ctx["meta"].get(rid) if isinstance(ctx["meta"].get(rid), dict) else {}
        st = ctx["states"].get(rid) or "waiting"
        word = "Done" if st == "done" else _STATES.get(st, (st,))[0]
        start, end = m.get("started"), m.get("updated")
        took = _text(_duration(end - start) if st not in (*_LIVE, *_PRE) and _number(start) and _number(end)
                     else _duration(now - start) if st in _LIVE and _number(start) else "")
        model = _names([m["model"]]) if m.get("model") else rid
        said = str(m.get("said") or "")
        pair = stats.get(rid)
        if isinstance(pair, (list, tuple)) and len(pair) == 2:  # the run's diff size, right after its time
            took += f'<span class="sw-d">{_counts(pair[0], pair[1])}</span>'
        verdict = str(checks.get(rid.rsplit(".", 1)[-1]) or checks.get(rid) or "")
        if verdict and verdict != "no changes":
            ok = verdict == "passed"
            took = (took + " " if took else "") + (f'<span class="sw-c{"" if ok else " bad"}">'
                                                   f'{"Check passed" if ok else "Check failed"}</span>')
        # in its own span: a phone row leaves it to the ring; a done run's ring already says 'done'
        state = f'<span class="sw-w">{_text(word)}</span>' if st != "done" else ""
        inner = (f'{_ring(st, "red" if st in _FAILED else None)}<span class="sw-m">{_text(model)}</span>'
                 f'<span class="sw-s">{_SEP_HTML.join(x for x in (state, took) if x)}</span>'
                 + (f'<span class="sw-a">{_text(said)}</span>' if said else ""))
        short = rid.rsplit(".", 1)[-1]
        mark = _swarm_verdict(kept, rid, runs)
        cls = f"sw-run {mark}" if mark in ("kept", "lost") else "sw-run"
        inner = f'<span class="sw-id mono">{_text(short)}</span>' + inner + (
            '<span class="sw-k">Kept</span>' if mark == "kept" else "")
        # a row opens its run, and says so as 'Show ›' does
        go = f'<span class="sw-go" aria-hidden="true">{_ic("chev", True)}</span>'
        live = " data-live" if st in _LIVE else ""  # a run at work wears a slow sheen (rh.css)
        items.append(f'<li><a class="{cls}" href="{_url(ctx, "/job/" + rid)}" data-nav{live}>{inner}{go}</a></li>'
                     if ctx["live"] else f'<li><div class="{cls}"{live}>{inner}</div></li>')
    n = len(runs)
    ended = sum((ctx["states"].get(r) or "waiting") not in (*_LIVE, *_PRE) for r in runs)
    again = ""
    if ended < n:  # while a run still works, the head says how far along the swarm is, and draws it
        side = (f'{ended} of {n} finished<span class="sw-bar" role="progressbar" aria-label="Runs finished" aria-valuemin="0" '
                f'aria-valuemax="{n}" aria-valuenow="{ended}" style="--p:{ended / n:.0%}"></span>')
    else:  # ended: the rows below show how many; the head says the judge combined them, when it did, and the kept work's check
        side = "Combined" if kept == "combined" else ""
        if kept == "combined" and final:  # the combined tree's check, as a row wears it
            ok = final == "passed"
            side += _SEP_HTML + (f'<span class="sw-c{"" if ok else " bad"}">'
                                 f'{"Check passed" if ok else "Check failed"}</span>')
        again = _swarm_again(ctx, runs) if ctx["live"] else ""
    body = (f'<div class="head"><h2>{_ic("swarm", True)}Swarm</h2><span class="lines">{side}</span>{again}</div>'
            f'<ul class="sw-runs">{"".join(items)}</ul>')
    return _region("j-swarm", body, "section", "out s-j-swarm", ' aria-label="Swarm runs"')


_TEAM_ROLE = {"best": "Lead", "plan": "Plan"}


def _team_card(ctx, jid):
    """A layered swarm's whole team on its lead's page, drawn as a tree: the lead, its plan and managers, and each
    manager's agents, every one with its state and time. Wires join each agent to whoever judges it; team.js draws
    them, flowing while that agent works, and lights the path of the work each judge kept up to the lead. A flat
    swarm (every run under the lead works) has no tree: its runs card says it all."""
    swarms, now = ctx.get("swarms") or {}, ctx["now"]
    runs = swarms.get(jid) or []
    if not any(swarms.get(r) for r in runs):
        return ""
    seen, tally = set(), {"n": 0, "live": 0}

    def node(rid, up, kept_by_up, depth):
        seen.add(rid)
        m = ctx["meta"].get(rid) if isinstance(ctx["meta"].get(rid), dict) else {}
        st = ctx["states"].get(rid) or "waiting"
        tone = "live" if st in _LIVE else "fail" if st in _FAILED else "done" if st == "done" else "wait"
        tally["n"] += 1
        tally["live"] += tone == "live"
        short = rid.rsplit(".", 1)[-1]
        role = _TEAM_ROLE.get(short) or ("Manager" if re.fullmatch(r"m\d+", short) and swarms.get(rid) else "Agent")
        start, end = m.get("started"), m.get("updated")
        took = (_duration(now - start) if tone == "live" and _number(start) else
                _duration(end - start) if tone in ("done", "fail") and _number(start) and _number(end) else "")
        word = "" if tone == "done" else _STATES.get(st, (st,))[0]
        model = _names([m["model"]]) if m.get("model") else short
        kept = str(m.get("kept") or "")
        cls = f"lt-n s-{tone}" + (" kept" if kept_by_up else "")
        inner = (f'{_ring(st, "red" if st in _FAILED else None)}<span class="lt-r">{_text(role)}'
                 f'<span class="mono"> {_text(short)}</span></span><span class="lt-m">{_text(model)}</span>'
                 f'<span class="lt-t">{_SEP_HTML.join(_text(x) for x in (word, took) if x)}</span>')
        attrs = f' data-lt="{_text(rid)}" data-up="{_text(up)}" data-tone="{tone}"'
        head = (f'<a class="{cls}" href="{_url(ctx, "/job/" + rid)}" data-nav{attrs}>{inner}</a>' if ctx["live"]
                else f'<div class="{cls}"{attrs}>{inner}</div>')
        kids = [k for k in swarms.get(rid, []) if k not in seen] if depth < 5 else []
        sub = "".join(node(k, rid, tone_kept(kept, k), depth + 1) for k in kids)
        return f'<li>{head}{f"<ul>{sub}</ul>" if sub else ""}</li>'

    def tone_kept(kept, kid):
        short = kid.rsplit(".", 1)[-1]
        return bool(kept) and (kept == short or kept == "combined" and ctx["states"].get(kid) == "done")

    tree = node(jid, "", False, 0)
    side = f'{tally["n"]} agents' + (_SEP_HTML + f'{tally["live"]} working' if tally["live"] else "")
    body = (f'<div class="head"><h2>{_ic("swarm", True)}Team</h2><span class="lines">{side}</span></div>'
            f'<div class="lt" data-lt-box><svg class="lt-wires" aria-hidden="true"></svg>'
            f'<ul class="lt-tree">{tree}</ul></div>')
    return _region("j-team", body, "section", "out s-j-team", ' aria-label="Swarm team"')


def _job_pane(opened, jobs, ctx, feedback, view):
    """A job's page: the work on the left (what it was asked, what it needs from you, its output, and Run again), and
    its properties, receipt, pauses and stops, and timeline on the right."""
    now = ctx["now"]
    job = opened.get("job") if isinstance(opened.get("job"), dict) else {}
    jid = str(job.get("id") or opened.get("id") or "")
    crumbs = (f'<nav class="crumbs" aria-label="Breadcrumb"><a href="{_url(ctx, "/")}" data-nav>Today</a>'
              if ctx["live"] else '<nav class="crumbs" aria-label="Breadcrumb"><span>Today</span>')
    if not job:
        top = _top(crumbs + f'{_ic("chev", True)}<span class="mono">{_text(jid)}</span></nav>')
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
    receipt = opened.get("receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    started_at = receipt.get("started") or job.get("started")
    ended_at = receipt.get("ended") or (job.get("updated") if finished else None)
    title = _title(spec, jid)
    acts = []
    if ctx["live"] and not finished and not entry.mine:
        acts.append(_pause(ctx, jid))
    if ctx["live"] and state in (*_PRE, "stalled") and not finished:  # the states rh job cancel accepts
        acts.append(_cancel(ctx, jid, stalled=state == "stalled"))
    named = entry.label if entry.label != entry.title else (_lead(spec)[0] or jid)  # as the sidebar names it
    swarm = _swarm_run(ctx, jid)  # a swarm's own run says so on a quiet line under its head
    command = spec.get("command")  # a This Mac agent in a folder other than the configured repository names it
    folder = (str(command[4]) if agent_of(command) and ctx.get("repo_path")
              and str(Path(str(command[4])).resolve()) != ctx["repo_path"] else "")
    head = (_job_head(jid, kind, entry.label, swarm, folder) if entry.dup
            else _job_head(named, kind, sub=swarm, folder=folder))
    work = []
    if feedback:
        work.append(feedback)
    failed = finished and state != "done" and state != "cancelled"
    # A failed job leads with the check that failed: what it ran and its exit code, before anything else.
    bad = next(((cmd, code) for passed, cmd, code in _checks_of(opened, spec, True) if passed is False), None)
    runs = next((e["runs"] for e in ctx["needs"] if e.get("job") and jid in [j.id for j in e["runs"]]), [])
    others = ""
    if len(runs) > 1:
        named = [f'<b class="mono">{_text(j.id)}</b>' if j.id == jid else
                 f'<a class="mono" href="{_url(ctx, "/job/" + j.id)}" data-nav>{_text(j.id)}</a>' for j in runs]
        others = f'<p class="fail-runs">Failed the same way {len(runs)} times: {_SEP_HTML.join(named)}</p>'
    if failed and (bad or others):
        said_fail = _fail_banner(state, spec, bad) if bad else ""
        work.append(_region("j-fail", said_fail + others, "section", "s-j-fail", ' aria-label="What failed"'))
    # The chat: each earlier run as what was asked and what the agent answered, then what this run asks.
    thread = opened.get("thread") if isinstance(opened.get("thread"), list) else []
    if thread:
        turns = []
        for run in thread:
            if not isinstance(run, dict):
                continue
            if run.get("ask"):
                ask = str(run["ask"])
                bubble = f'<div class="user-message"><span class="um-t">{_text(_reflow(ask))}</span></div>'
                # each ask's Copy wakes on its own turn's hover, so the bubble and its row share an .ask-turn
                turns.append(f'<div class="ask-turn">{bubble}{_brief_copy(ask)}</div>' if ctx["live"] else bubble)
            link = (f'<a class="mono" href="{_url(ctx, "/job/" + str(run.get("id")))}" data-nav>{_text(run.get("id"))}</a>'
                    if ctx["live"] else f'<span class="mono">{_text(run.get("id"))}</span>')
            # each earlier turn wears the status row the current one does, above what its agent said
            word = _text("Done" if run.get("state") == "done" else _STATES.get(run.get("state"), (run.get("state"),))[0])
            state_html = f'{_ring(str(run.get("state")))}<span class="job-result-status">{word}{_SEP_HTML}{link}</span>'
            if run.get("answer"):  # one line: who answered on the left, how the run went on the right
                turns.append(_said(run.get("provider"), run.get("model"), run["answer"], status=state_html,
                                   live=ctx["live"]))
            else:
                turns.append(f'<p class="job-result turn-state">{_ring(str(run.get("state")))}<span class="job-result-status">'
                             f'{word}{_SEP_HTML}{link}{_SEP_HTML}no answer</span></p>')
        work.append(_region("j-thread", "".join(turns), "section", "s-j-thread", ' aria-label="Earlier runs"'))
    original_prompt = ask_of(spec)
    if original_prompt:
        # the ask's text has its own span: it folds and fades there, and the bubble keeps its fill; a live page's
        # Copy sits after the bubble, so the script's Show all still lands beside it
        bubble = f'<div class="user-message"><span class="um-t">{_text(_reflow(original_prompt))}</span></div>'
        work.append(_region("j-brief", bubble + (_brief_copy(original_prompt) if ctx["live"] else ""),
                            "section", "s-j-brief", ' aria-label="Original request"' if not spec.get("prior")
                            else ' aria-label="Follow-up"'))
    elif spec.get("prior"):
        work.append(_region("j-brief", '<p class="run-note">Ran again with no changes</p>', "section", "s-j-brief",
                            ' aria-label="Follow-up"'))
    talk = [m for m in opened.get("inbox") or [] if isinstance(m, dict) and m.get("id")]
    talking = isinstance(opened.get("inbox"), list) and state == "running" and ctx["live"]
    if talk or talking:  # what the owner said to it as it ran; the region stays put, so a draft survives a refresh
        work.append(_region("j-said", _talk_said(talk), "section", "s-j-said", ' aria-label="Your messages"'))
    check_rows = _checks_of(opened, spec, finished or state not in _PRE)
    checks_at = None  # where the status line counts the checks, if it does
    check_values = [passed for passed, _, _ in check_rows if passed is not None]
    shown = "env" if entry.native else state
    word = _CHAT_WAIT[0] if kind == "gpt-chat" and shown == "handoff" else _STATES.get(shown, (shown,))[0]
    runs = ctx.get("swarms", {}).get(jid) or []
    # a swarm whose judge has not started reads as its runs do: running while one works, and how many have ended
    swarming = bool(runs) and state in _PRE
    if swarming and any(ctx["states"].get(r) in _LIVE for r in runs):
        shown, word = "running", _STATES["running"][0]
    result = [f'{_ring("done" if state == "done" else shown, kind=kind)}']
    status_line = [_text("Done" if state == "done" else word)]
    late = state == "stalled"
    # quiet for over 5 min past the limit: the log card says output stopped, so the status line does not repeat it
    quiet_log = (late and _number(opened.get("log_at")) and now - opened["log_at"] > 300
                 and isinstance(opened.get("log"), str) and bool(opened["log"].strip()))
    if late:  # past its limit, said once, with the limit (the line under it is left out)
        limit = spec.get("timeout_min")
        status_line = [_text(f"Past its {_minutes(limit)} limit" if _number(limit) and limit > 0 else word)]
        if _number(opened.get("log_at")):  # whether it still works or hangs: when its output last changed
            if now - opened["log_at"] > 300:  # quiet for over 5 min: the ring dims, so the page reads stopped
                result[0] = result[0].replace('class="st ', 'class="st quiet ', 1)
            if not quiet_log:
                status_line.append(_when(opened["log_at"], now, "j:quiet", pre="last output "))
    elif swarming:
        pass  # how many runs have finished is said once, by the Swarm card's head beside its bar
    elif check_rows and not all(value is True for value in check_values) or len(check_values) < len(check_rows):
        # every check passed: the done ring says so, and the details panel lists each; a count shows only when
        # one failed or has not run
        if len(check_values) == len(check_rows):
            checks_text = f'{sum(value is True for value in check_values)}/{len(check_rows)} checks'
        elif check_values:
            checks_text = f'{len(check_values)}/{len(check_rows)} checks checked'
        else:
            checks_text = f'{len(check_rows)} {"check" if len(check_rows) == 1 else "checks"} pending'
        checks_at = len(status_line)
        status_line.append(_text(checks_text))
    if runs:  # a swarm's time is the whole swarm's, from its first run's start
        firsts = [ctx["meta"].get(r, {}).get("started") for r in runs if isinstance(ctx["meta"].get(r), dict)]
        firsts = [t for t in firsts if _number(t)]
        started_at = min([*firsts, *([started_at] if _number(started_at) else [])], default=started_at)
    if late:
        pass  # its time is past the limit it names
    elif _number(started_at) and _number(ended_at):
        status_line.append(_text(_duration(ended_at - started_at)))
    elif _number(started_at) and not finished:
        status_line.append(_when(started_at, now, "j:worked", short=True))
    # A run whose newest lines count "N of M" says how far along it is, and about how long is left at its pace.
    step = _progress(opened.get("log")) if state in ("running", "stalled") else None
    if step:
        done_n, total = step
        status_line.append(f'<span class="num">{done_n:,} / {total:,}</span>')  # as finished logs write 2,400
        spent = now - started_at if _number(started_at) else 0
        if spent > 30 and done_n < total:
            rest = spent * (total - done_n) / done_n
            # past its limit, 'N left' would read as budget left: it says when it should end instead
            status_line.append(_text(f"ends ~{_clock(now + rest, now)}" if late else _left(rest)))
    quiet = failed and bad and len(status_line) > 1
    if quiet:  # the banner above says it failed, in red, and names the check: the line is the state and its time, grey
        if checks_at is not None:
            del status_line[checks_at]
    # each part keeps the dot that leads it, so a wrapped line can hide its leading dot (see .sl in rh.css)
    status_line = [f'<span class="nowrap">{_SEP_HTML if i else ""}{part}</span>' for i, part in enumerate(status_line)]
    result.append(f'<span class="job-result-status sl">{"<wbr>".join(status_line)}</span>')
    if step:
        result.append(f'<progress class="run-progress" max="{step[1]}" value="{step[0]}" '
                      f'aria-label="{step[0]} of {step[1]}"></progress>')
    status_at, status_html = len(work), "".join(result)
    work.append(_region("job-result", status_html, "p", "job-result after-fail" if quiet else "job-result"))
    if runs:
        team = _team_card(ctx, jid)
        if team:
            work.append(team)
        work.append(_swarm_card(ctx, runs, opened.get("swarm_checks"), opened.get("swarm_kept"),
                                opened.get("swarm_stats"), final=opened.get("swarm_final")))
    # What it needs from you, each with the form that settles it.
    cards, lead = [], True
    chat_dock = ""
    for item in entry.items:
        if item.get("kind") == "chat":
            cards.append(_chat_card(ctx, jid, spec, item, lead, include_reply=False))
            chat_dock = _chat_reply_form(ctx, jid, lead)
        else:
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
        cards.append(f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you{_SEP_HTML}{when}</p>'
                     f'<h3>{verb} from the app: {_text(_clause(stop["reason"]))}</h3><p class="detail">{detail}</p>'
                     f'{_acts(act) if ctx["live"] else ""}</div>')
        lead = False
    if entry.held:
        cards.append(f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you</p>'
                     f'<h3>On hold: {_text(_plain(job.get("detail")))}</h3>'
                     f'<p class="detail">{_NEXT["held"]}</p></div>')
    if cards:
        work.append(_region("j-asks", "".join(cards), "section", "s-j-asks", ' aria-label="Needs you"'))
    answer = opened.get("reply")
    if isinstance(answer, str) and answer.strip():
        work.append(_region("j-answer", f'<p class="who">{_ic("chat", True)}GPT Pro</p><div class="assistant-message">'
                            f'{_text(_reflow(answer))}</div>',
                            "section", "msg ai", ' aria-label="The reply"'))
    said = opened.get("answer")
    if isinstance(said, str) and said.strip():
        agent = agent_of(spec.get("command"))
        provider, model = agent[:2] if agent else ("codex" if kind == "codex-cloud" else "", "")
        if not model and kind == "codex-cloud" and isinstance(spec.get("model"), dict):
            model = str(spec["model"].get("requested") or "")
        # a finished run's status joins its agent's line, and its own row goes
        merge = finished and not step and 'data-region="job-result"' in work[status_at]
        if merge:
            work.pop(status_at)
        work.append(_region("j-answer", _said(provider, model, said, inner=True, role="Judge" if runs else "",
                                              status=status_html if merge else "", live=ctx["live"], key="y"),
                            "section", "msg ai",
                            ' aria-label="The answer"'))
    if not finished:
        loop = view.get("checks") if isinstance(view.get("checks"), dict) else None
        doing = (f"Running {_PLACES[kind]}" if state == "running" and kind in _PLACES else entry.word)
        next_line = ("It starts once Codex cloud is set up." if entry.setup
                     else "" if entry.native or kind == "gpt-chat" else _NEXT.get(state, "").replace("{place}", _PLACES.get(kind, "on its platform")))
        if loop and loop.get("mode") == "paused":
            next_line = (next_line + " Automatic checks are paused: the app won't launch or collect it until you "
                         "resume them or press Check once.").strip()
        detail = _plain(job.get("detail")) if state not in ("held", "handoff") and not entry.native else ""
        if runs and state in _PRE:  # a swarm's judge: its runs are listed above, so say what it does with them
            judge = _names([(ctx["meta"].get(jid) or {}).get("model") or ""]) or "The judge"
            working = any(ctx["states"].get(r) in _LIVE for r in runs)
            doing, detail = ("Runs working" if working else "Starting the judge"), ""
            next_line = f"{judge} keeps the best work once every run has ended."
        now_body = (f'<p class="now">{_dot(entry.tone, state == "running")}<span class="what">{_text(doing)}</span>'
                    + (f'<span class="soft">{_text(detail)}</span>' if detail and detail != doing else "")
                    + (f'<span class="soft">{_text(next_line)}</span>' if next_line else "") + "</p>")
        # The head already gives the state, so this line is there only when it adds what comes next or why.
        if (next_line or (detail and detail != doing)) and state != "stalled":
            work.append(_region("j-now", now_body, "section", "s-j-now", ' aria-label="Now"'))
    # A running job shows its output as it comes. A finished one leads with what it made, or, when it failed, with
    # where it stopped and the box to fix it right under that; its output (the last 200 lines) is folded away below.
    log = opened.get("log")
    log = log if isinstance(log, str) and log.strip() else ""
    lines = log.rstrip("\n").splitlines()
    files = opened.get("files") if isinstance(opened.get("files"), list) else []
    if files:
        preview = opened.get("peek") if isinstance(opened.get("peek"), dict) else None
        file_rows = []
        for file in files:
            path = str(file.get("path", ""))
            file_url = ("/job/" + jid + "/file?p=" + quote(path) + "&t=" + ctx["token"]
                        if ctx["live"] else "")
            link = (f'<a href="{_text(file_url)}" target="_blank" rel="noopener">{_text(path)}</a>'
                    if ctx["live"] else _text(path))
            details = summed = copy = ""
            if preview and preview.get("path") == path:
                # (own names: render's head and text are the page's)
                peek_text = str(preview.get("text", ""))
                diff_head = (_diff_head(peek_text, whole=not preview.get("more"))
                             if path.endswith((".patch", ".diff")) else "")
                more = (('<span class="dl">…</span>' if diff_head else "\n…") if preview.get("more") else "")
                # one head: the file's own row names what a patch changes and its counts, and heads the card open;
                # the preview may be cut short, so Copy fetches the file's whole text at the row link's URL
                summed = f'<span class="peek-sum">{diff_head}</span>' if diff_head else ""
                copy = (f'<button type="button" class="peek-copy" data-copy-url="{_text(file_url)}" '
                        f'aria-label="Copy the whole patch">{_ic("copy", True)}<span class="pc-t">Copy</span></button>'
                        if ctx["live"] and diff_head else "")
                details = (f'<details class="file-preview"><summary>Show</summary><div class="peek-card">'
                           f'<pre class="peek{" diff" if diff_head else ""}" tabindex="0">'
                           f'{_peek_html(path, peek_text, whole=not preview.get("more"))}{more}</pre></div></details>')
            # a diff's counts are the fact a patch row needs; the size stays only where it is the row's one fact
            size = "" if summed else f'<span class="session-file-size">{_text(_size(file.get("size")))}</span>'
            file_rows.append(f'<li class="session-file"><span class="session-file-main">{_ic("file", True)}{link}'
                             f'{summed}{size}{copy}</span>{details}</li>')
        work.append(_region("session-files", '<ul class="session-files">' + "".join(file_rows) + '</ul>',
                            "section", "session-file-list", ' aria-label="Files produced"'))
    # A done job that gave no answer made its output: its last lines show open, as a failed one's do.
    answered = any(isinstance(opened.get(k), str) and opened[k].strip() for k in ("answer", "reply"))
    card = bool(log) and (failed or (finished and state == "done" and not answered))
    total = opened.get("log_total") if opened.get("log_cut") and isinstance(opened.get("log_total"), int) else None
    if card:
        tail = lines[-_TAIL:]
        # the failed check and its exit code lead the page; the card adds only what the run printed last
        name, region = ("Where it stopped", "j-error") if failed else ("Last output", "j-last")
        # the card names the whole log's length once (its title already says it shows the last lines); a kept tail of
        # unknown length is no whole, so the card counts its own
        whole = total if total else None if opened.get("log_cut") else len(lines)
        n = whole if whole and whole > len(tail) else len(tail)
        # the count says what is hidden: a log longer than the tail shown, or a kept tail cut from one; over lines
        # already in view it counts nothing and the head goes without it
        count = (f'<span class="lines mono">{n:,} {"line" if n == 1 else "lines"}</span>'
                 if opened.get("log_cut") or len(lines) > len(tail) else "")
        err = " err" if failed else ""
        # the rest of the log folds into the card's own foot, so one card names the output once (r27 F13): open, the
        # whole replaces the last lines and the foot stays under it
        rest = (f'<details class="out-more"><summary><span class="om-show">'
                f'{f"Show last {len(lines):,} lines" if opened.get("log_cut") else "Show all"}</span>'
                f'<span class="om-hide">Show less</span></summary><pre class="log ended{err}" tabindex="0">'
                f'{"".join(_ln(l) for l in lines)}</pre></details>' if len(lines) > len(tail) else "")
        body = (f'<div class="head"><h2>{_ic("term", True)}{name}</h2>{count}</div>'
                f'<pre class="log ended{err}" tabindex="0">{"".join(_ln(l) for l in tail)}</pre>{rest}')
        work.append(_region(region, body, "section", "out", f' aria-label="{name}"'))
    # a card holds its whole output in its own foot; a page without one folds the output here
    if (not finished or log) and not card and not (swarming and not log):
        tag = f'<span class="livetag">{_dot("live", True)}live</span>' if state == "running" else ""
        last = "last " if opened.get("log_cut") else ""
        # under a card that names the whole, the fold says only how much of it it holds: 'last 200', or nothing; a
        # live log shows whole in the open fold, so its count stays only for a kept tail ('last N') or a log long
        # enough to scroll (over 20 lines)
        line_count = ((f"last {len(lines):,} lines" if last else "") if card else
                      f'{len(lines)} of {total:,} lines' if total and finished else
                      f'{last}{len(lines)} {"line" if len(lines) == 1 else "lines"}'
                      if finished or last or len(lines) > 20 else "")
        # The status line above keeps the run's time, so the fold names only its output; while the job runs it
        # starts open: watching the output is what the page is for until the job ends.
        output_status = ("All output" if card and not last else "Output") if log else "No output received yet"
        # the count sits right, as every log card's does
        count = f'<span class="lines mono">{_text(line_count)}</span>' if log and line_count else ""
        # folded, a glyph where a file row's icon sits starts 'Output' on the file name's edge
        work_summary = (f'<summary class="worked-summary">{_ic("term", True)}'
                        f'<span class="worked-output">{output_status}</span>{tag}{count}</summary>')
        # past its limit and quiet a while, the card says its output stopped, a hairline under its last line
        quiet_row = (f'<span class="log-quiet">no output since {_text(_clock(opened["log_at"], now))}'
                     f'{_SEP_HTML}{_when(opened["log_at"], now, "j:quiet", short=True)}</span>'
                     if quiet_log else "")
        output_body = (f'<pre class="log"{"" if finished else " data-follow"} tabindex="0">{_text(log)}{quiet_row}</pre>'
                       if log else '<p class="soft">The worker has not produced output yet.</p>')
        worked_body = f'{work_summary}<div class="worked-content">{output_body}</div>'
        work.append(_region("j-output", worked_body, "details", "worked",
                            ' aria-label="Worked and output"' + ("" if finished else " open")))
    again_word = "Judge again" if runs else "Run again"
    if finished and ctx["live"]:
        again = state != "done" or spec.get("kind") != "codex-cloud"
        message = ('<label class="sr" for="changed">What should change?</label>'
                   '<textarea class="c-text" id="changed" name="changed" rows="1" '
                   'placeholder="Reply…"></textarea>')
        agent = agent_of(spec.get("command"))
        # An agent's next run may use another model or effort: the reply box has the composer's pickers.
        picker = (_model_picker(view.get("job_policy") or {}, "local-agent", {"model": agent[1], "effort": agent[2]},
                                only=True) if agent else "")
        # a swarm judge's next run judges the same runs' work again, on the model picked here: it says so
        reply = _composer_form(ctx, "/again", again_word, message, job=jid, files=True, worker=picker,
                               controls=_worker_note(_platform(kind)), followup=True,
                               primary=lead and again and state != "done")
    else:
        reply = ""
    if talking:
        talker = agent_of(spec.get("command"))
        dock_content = _talk_form(ctx, jid, talker[0].capitalize() if talker else "the agent")
        dock_region = "j-message"
        dock_label = "Message the agent"
    elif chat_dock:
        dock_content = chat_dock
        dock_region = "j-chat-reply"
        dock_label = "Save reply"
    else:
        dock_content = reply
        dock_region = "j-reply"
        dock_label = again_word
    scroll = f'<div class="jd-scroll"><div class="jd-body">{"".join(work)}</div></div>'
    dock = (_region(dock_region, dock_content, "section", "jd-compose session-dock", f' aria-label="{dock_label}"')
            if dock_content else "")
    dock = f'<div class="jd-dock">{_up_next(talk) if talking else ""}{dock}</div>' if dock else ""
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
    record = "".join(side)
    if ctx["live"]:
        record = (f'<details class="jd-inspector"><summary>{_ic("inspector", True)}'
                  '<span class="sr">Job details</span></summary>'
                  f'<div class="jd-record">{record}</div></details>')
    page = (f'<div class="page jd" data-session="1" data-state="{_text(state)}"><div class="jd-main">{head}{scroll}{dock}</div>'
            f'<aside class="jd-side" aria-label="Job details">{record}</aside></div>')
    actions = _region("job-actions", "".join(acts), "div", "job-actions") if acts else ""
    return "job:" + jid, actions + page, title


def _exit_why(jid, view):
    """A failed row's reason as the failure itself: its exit code and the last line it printed (else its command), so
    failures tell apart without opening them; "" when no failed check is recorded."""
    check = ((view or {}).get("fails") or {}).get(jid)
    if not (isinstance(check, list) and len(check) == 3):
        return ""
    lines = [l.strip() for l in str(((view or {}).get("tails") or {}).get(jid) or "").splitlines() if l.strip()]
    return (f'<span class="ib-why ib-exit">Exit {_text(check[1])}{_SEP_HTML}'
            f'<span class="ib-out">{_text(lines[-1] if lines else check[0])}</span></span>')


def _ib_row(ctx, entry, picked, view=None):
    """One thing that waits on you: an item opens here beside the list; a job with nothing to answer opens its page,
    and the app's own pause opens Today, where its Resume is."""
    job, jid = entry["job"], entry["jid"]
    href = (_url(ctx, f"/inbox/{entry['item']['id']}") if entry["item"] else _url(ctx, f"/job/{jid}") if job
            else _url(ctx, "/"))
    created = entry["created"]
    age = _clock(created, ctx["now"]) if _number(created) else ""  # clock times, as the sidebar and Settled give
    head = job.title if job else entry["ask"]
    # why it waits, then the run's id on a line of its own, which tells runs of one brief apart
    failed = job and not entry["item"] and job.state in _FAILED and _exit_why(jid, view)
    sub = (failed if failed else
           f'<span class="ib-why">{job.headline_html(len(entry.get("runs", [])) or 1)}</span>' if job and not entry["item"] else
           f'<span class="ib-why">{_text(entry["ask"])}</span>' if job else
           "A job not listed here" if jid
           else "The app's own pause" if entry["stop"] else "The harness")
    idline = f'<span class="ib-id mono">{_text(jid)}</span>' if jid else ""
    current = ' aria-current="true"' if picked else ""
    # a job's entry wears the ring it wears on Today, so a state has one mark in both places
    mark = _ring(job.state, "red" if job.state in _FAILED else "amber", job.kind) if job else _ic(entry["icon"])
    inner = (f'<span class="ib-ic">{mark}</span><span class="ib-t">{_text(head)}</span>'
             f'<span class="ib-age mono num">{age}</span><span class="ib-s">{sub}</span>{idline}')
    link = (f'<a class="ib" href="{href}" data-nav{current}>{inner}</a>' if ctx["live"]
            else f'<div class="ib">{inner}</div>')
    return _row(entry["key"], link, "ib-li", f' data-kind="{entry["tab"]}"')


def _settled_row(ctx, item, picked, runs=1):
    """One settled item, or the newest of `runs` asked and answered alike, which counts them (critic r25 F6)."""
    ask, _ = _ask_line(item)
    jid = item.get("job")
    note = str(item.get("resolution") or "").strip()
    note = note[:1].upper() + note[1:]  # a stored note may start lower case ("job cancelled"); the list reads in sentences
    # the outcome, then the run's id on its own line, as the rows that wait read
    sub = f'<span class="ib-why">{_text(note)}</span>' if note else ""
    if runs > 1:
        sub += (_SEP_HTML if sub else "") + f'<span class="ib-n">{runs} runs</span>'
    idline = f'<span class="ib-id mono">{_text(jid)}</span>' if jid else ""
    current = ' aria-current="true"' if picked else ""
    inner = (f'<span class="ib-ic">{_ic("check")}</span><span class="ib-t">{_text(ask)}</span>'
             f'<span class="ib-age mono num">{_clock(item["resolved"], ctx["now"])}</span>'
             f'<span class="ib-s">{sub or "Settled"}</span>{idline}')
    link = (f'<a class="ib done" href="{_url(ctx, "/inbox/" + str(item["id"]))}" data-nav{current}>{inner}</a>'
            if ctx["live"] else f'<div class="ib done">{inner}</div>')
    return _row(f"settled:{item['id']}", link, "ib-li settled")


def _ib_job(ctx, entry, view):
    """A job that waits on you with nothing to answer here, as the Inbox shows it when it leads the list: what it was,
    what went wrong, and the way to its page, where it is retried or followed up."""
    job, runs = entry["job"], len(entry.get("runs", [])) or 1
    # the pane opens on what the job is; where it ran, since when and its run id follow in one quiet line
    who = [f'<span>{_text(_platform(job.kind))}</span>']
    if _number(entry.get("created")):
        who.append(f'<span>since <span class="mono num">{_clock(entry["created"], ctx["now"])}</span></span>')
    who.append(f'<span class="mono">{_text(job.id)}</span>')
    again = job.act()  # Run again leads when the row offers it; the job's page is the quieter way
    open_it = (f'<a class="btn{"" if again else " primary"}" href="{_url(ctx, "/job/" + job.id)}" data-nav>Open job</a>' if ctx["live"]
               else f'<p class="cli"><code>rh job show {_text(job.id)}</code></p>')
    bad = job.state in _FAILED
    tail = _tail_lines((view.get("tails") or {}).get(job.id))
    # the tail shows whole, so a count of it would count nothing hidden: the head is the name alone
    stopped = (f'<section class="out"><div class="head"><h2>{_ic("term", True)}Where it stopped</h2></div>'
               f'<pre class="log ended err" tabindex="0">{"".join(_ln(l) for l in tail)}</pre></section>'
               if tail else "")
    # a failed check reads as on the job's page: the sentence, the command and its exit code
    check = (view.get("fails") or {}).get(job.id)
    said = (_fail_banner(job.state, {"accept": check[2]}, check[:2])
            if bad and isinstance(check, list) and len(check) == 3 else
            f'<p class="q-fail-k">{_ring(job.state, "red" if bad else "amber", job.kind)}'
            f'<span>{job.headline_html(runs)}</span></p>')
    # The title is the brief whole when the job's name in the lists cannot say it all: it folds at three
    # lines with a Show all in its foot, the fold the job's own page gives the ask. A brief the name
    # already says keeps the bare title.
    # A follow-up's brief ends with its follow-up ("Follow-up: ..."): the title is the brief as first asked, so the
    # pane's loudest line is what the job is; the follow-up, the owner's own words, is quoted where the job page puts
    # what was asked: under the banner, over the output, the actions closing the stack.
    brief, ask = job.brief, ""
    if job.meta.get("prior"):
        brief = brief.split("\n\nFollow-up: ", 1)[0]
        ask = " ".join(ask_of(job.meta).split())
    quoted = (f'<figure class="q-fu"><figcaption>Follow-up</figcaption><blockquote>{_text(ask)}</blockquote></figure>'
              if ask else "")
    head, rest = _lead({"brief": brief})
    title = (f'<h2 class="q-title user-message" id="q-h" data-clear>{_text(_reflow(brief))}</h2>'
             '<button type="button" class="ask-more" aria-expanded="false">Show all</button>'
             if brief and (rest or head != job.title) else
             f'<h2 class="q-title" id="q-h" data-clear>{_text(job.title)}</h2>')
    body = (f'<div class="q-head"><div class="q-head-in">{title}'
            f'<p class="q-who q-sub" data-clear>{_SEP_HTML.join(who)}</p></div></div>'
            f'<div class="q-body"><div class="{"q-fail" if bad else "q-wait"}">{said}</div>{quoted}{stopped}'
            f'<div class="q-acts">{again}{open_it}</div></div>')
    return _region("ib-detail", body, "section", attrs=' aria-labelledby="q-h"')


def _ib_detail(ctx, entry, settled, view, missing, lead=None):
    """The item the Inbox shows: who asks and about what, over a patch of water, then the form that settles it."""
    if lead is not None:
        return _ib_job(ctx, lead, view)
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
        who.append(f'<span>{_text(_platform(job.kind))}</span>')
    if _number(item.get("created")):
        who.append(f'<span>asked at <span class="mono num">{_clock(item["created"], ctx["now"])}'
                   f'</span>' + (f', waiting {_when(item["created"], ctx["now"], "q:age", short=True)}' if entry else "")
                   + '</span>')
    # each part keeps the dot that leads it, so a wrapped line can hide its leading dot (see .sl in rh.css)
    who = [f'<span class="nowrap">{_SEP_HTML if i else ""}{part}</span>' for i, part in enumerate(who)]
    about = ""
    if job:
        link = (f'<a href="{_url(ctx, "/job/" + job.id)}" data-nav>{_text(job.title)}</a>' if ctx["live"]
                else _text(job.title))
        about = (f'<p class="q-job" data-clear>{_dot("warn" if entry else "neutral")}<span>On “{link}”</span>'
                 f'{_SEP_HTML}<span>{_text(job.word)}</span></p>')
    tone = "amber" if entry and not (job and job.state in _FAILED) else "faint"
    # An item on a job is its seat's jellyfish; one from the harness itself is the octopus.
    water = (_tank([_creature(job.seat or "harness", tone, 1, 1, (0.9, 0.55))], "q-water") if job else
             _tank([], "q-water", {"octopus": {"tone": tone, "at": [0.92, 0.5]}}))
    back = (f'<a class="ib-back" href="{_url(ctx, "/inbox")}" data-nav>Inbox</a>' if ctx["live"] else "")
    head = (f'{back}<div class="q-head">{water}<div class="q-head-in">'
            f'<p class="q-who sl" data-clear>{"<wbr>".join(who)}</p><h2 class="q-title" id="q-h" data-clear>{_text(ask)}</h2>'
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
        elif kind == "chat" and jid:
            meta = ctx["meta"].get(jid) or {}
            parts.append(_chat_card(ctx, jid, {"brief": meta.get("brief", ""), "model": meta.get("model") or {}},
                                    item, True))
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
    waiting = ctx["needs"]  # already newest first
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
    # With nothing to answer here, the Inbox opens on the newest job that waits on you, as an item would open.
    lead = (next((e for e in waiting if e["job"] and not e["item"]), None)
            if entry is None and settled_item is None and missing is None else None)
    rows = [_ib_row(ctx, e, (entry or lead) is not None and e["key"] == (entry or lead)["key"], view) for e in waiting]
    # settled items with the same question and outcome (a swarm's runs held alike) are one row for the newest
    alike = {}
    for i in settled:  # newest first, so each group's first item is its row
        alike.setdefault((_ask_line(i)[0], str(i.get("resolution") or "").strip().lower()), []).append(i)
    done = [_settled_row(ctx, group[0], settled_item is not None and any(i["id"] == settled_item["id"] for i in group),
                         len(group)) for group in alike.values()]
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
    # Tabs filter only a mix of kinds.
    tabbed = ctx["live"] and len(tabs) > 2
    tabbar = f'<div class="tabs" role="tablist" aria-label="Filter">{"".join(tabs)}</div>' if tabbed else ""
    top = _top('<h1 tabindex="-1">Inbox</h1>'
                    f'<span class="top-meta mono"><span data-need="{len(waiting)}">{_need_word(len(waiting))}</span>'
                    '</span>')  # one count, the sidebar badge's: the Settled today section says the rest
    # alone, the group's name repeats the top bar's count: it stays for a screen reader only
    lists = (_section("ib-wait", "Needs you" if done else '<span class="vh">Needs you</span>',
                      _rows(rows) if rows else '<p class="empty">Nothing needs you.</p>',
                      # the top bar carries the one count of what waits and what was settled; the lists repeat none
                      tabbar)
             + _section("ib-settled", "Settled today", _rows(done), "", hidden=not done))
    ctx["back"] = "/inbox"
    detail = _ib_detail(ctx, entry, settled_item, view, missing, lead)
    # On a narrow screen the list and an item are two screens: the list until an item is asked for by its address.
    shown = "item" if pick is not None else "list"
    page = (f'<div class="page ibx" data-view="{shown}"><div class="ib-list" aria-label="Waiting on you">{lists}</div>'
            f'{detail}</div>')
    if not rows and not done:  # nothing waits and nothing was settled: one empty state, not a bare list beside it
        page = f'<div class="page ibx" data-view="empty">{detail}</div>'
    return top + feedback + page


def _palette(jobs, ctx, keys):
    """⌘K: every job, the pages, and what this page can do, filtered as you type."""
    # grouped and named as the sidebar groups them; an older run folded into a newer one's Needs you row sits with the
    # runs that ended, so the first group holds exactly what the badge counts
    heads = {e["jid"] for e in ctx["needs"] if e.get("job")}
    group = lambda j: j.group if j.group != "needs" or j.id in heads else "folded"  # noqa: E731
    order = {"needs": 0, "live": 1, "next": 2, "ended": 3, "earlier": 4, "folded": 5}
    names = {"needs": "Needs you", "live": "Live", "next": "Up next", "ended": "Ended today", "earlier": "Earlier",
             "folded": "Earlier runs of what needs you"}
    # a finished group whose rows all share one state word says it by its heading and rings, not on every row (r26 F3)
    words = {}
    for j in jobs:
        words.setdefault(group(j), set()).add(j.word)
    quiet = {g for g, w in words.items() if g in ("ended", "earlier") and len(w) == 1}
    rows, at = [], None
    for j in sorted(jobs, key=lambda j: order[group(j)]):
        if group(j) != at:
            at = group(j)
            rows.append(f'<p class="p-sec">{names[at]}</p>')
        hue = ("red" if j.state in _FAILED else "amber") if group(j) == "needs" else None
        same = group(j) in quiet
        one = " one" if same and not j.dup else ""  # name and time only: one line on a phone too
        rows.append(f'<a class="p-row{one}" role="option" data-id="{_text(j.id)}" href="{_url(ctx, "/job/" + j.id)}" '
                    f'data-nav><span class="p-ic">{_ring(j.state, hue, j.kind, j.past)}</span>'
                    # as the sidebar names it: when names clash the id leads and the shared name moves to the grey
                    # line, which leads with the state so states form one column
                    + f'<span class="p-t">{_text(j.id if j.dup else j.label)}</span>'
                    + '<span class="p-s">' + ("" if same else f'<span class="p-w">{_text(j.word)}</span>')
                    # no id or slug here (typing one still finds the row, by its data-id); only a clash names both
                    + (f'<span class="p-b">{_text(j.label)}</span>' if j.dup else "")
                    + '</span>'
                    # the time the sidebar shows, in a cell of its own on the title line at every width, so every
                    # row's time shares one edge and runs of one brief tell apart at a glance
                    + (f'<span class="p-when mono num">{_clock(j.since(), ctx["now"])}</span>' if j.since() else "")
                    + f'<span class="p-a">{_where(j.kind)}</span></a>')
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
            'action" placeholder="Jump to anything" autocomplete="off" spellcheck="false">'
            '<kbd>esc</kbd><button type="button" class="p-x" data-pal-close>Cancel</button></div>'
            f'<div class="p-list" role="listbox" aria-label="Results">{"".join(rows)}'
            '<p class="p-none" hidden>Nothing matches.</p></div>'
            f'<div class="p-foot">{_mark(16)}<span>research-harness</span><span class="p-f-r"><span>Open</span>'
            '<kbd>↵</kbd><span class="bar"></span><span>Close</span><kbd>esc</kbd></span></div>')
    return (f'<dialog class="pal" id="pal" aria-label="Jump to anything" data-region="pal" '
            f'data-sig="{_sig(_still(body))}">{body}</dialog>')


def _sidebar(ctx, view, page, wait, queue):
    live = ctx["live"]
    if live:
        new = (f'<a class="new-job" href="{_url(ctx, "/new")}" data-nav data-k="n" aria-label="New job, N">'
               f'{_ic("new")}<span>New job</span><kbd aria-hidden="true">N</kbd></a>'
               if ctx["can_add"] else "")
        ws = ('<div class="mode-tabs" role="tablist" aria-label="Mode">'
              '<button type="button" role="tab" data-mode="coding" aria-selected="true">Coding</button>'
              '<button type="button" role="tab" data-mode="research" aria-selected="false">Research</button></div>'
              f'<span class="ws-tools"><button type="button" class="icon-btn" data-pal aria-label="Jump to anything, ⌘K">'
              f'{_ic("search")}</button><button type="button" class="icon-btn side-collapse" data-collapse-side '
              f'aria-label="Hide the sidebar" title="Hide the sidebar">{_ic("sidebar")}</button></span>{new}')
        links = [("inbox", "Inbox", "/inbox", page.startswith("inbox"))]
        current = ' aria-current="page"'
        nav = "".join(f'<a href="{_url(ctx, path)}" data-nav{current if here else ""}>{_ic(icon)}<span>{word}</span>'
                      + (f'<b class="count num" data-need="{wait}" aria-label="{_need_word(wait)}">{wait}</b>'
                         if icon == "inbox" and wait else "") + '</a>' for icon, word, path, here in links)
        nav = f'<nav class="nav" aria-label="Main" data-region="nav" data-sig="{_sig(nav)}">{nav}</nav>'
        tools = (f'<button type="button" class="icon-btn" data-settings aria-label="Settings">{_ic("gear")}</button>'
                 '<span class="conn" data-live data-state="on" role="status"></span>')
    else:
        ws = f'<p class="ws-btn">{_mark()}<span>research-harness</span></p>'
        nav = ""
        tools = '<div class="tools"><span class="soft">Snapshot</span></div>'
    top = f'<div class="ws" data-region="side-top" data-sig="{_sig(ws)}">{ws}</div>'
    if live:
        foot = f'<div class="side-foot side-controls">{_coord(view, ctx, ctx["jobs"])}{tools}</div>'
    else:
        foot = (f'<div class="side-foot">{_host(view, ctx)}<footer class="checks"><h2 class="side-h">Check loop</h2>'
                f'{_checks(view, ctx)}{tools}</footer></div>')
    grip = ('<div class="side-grip" role="separator" aria-orientation="vertical" aria-label="Resize the sidebar" '
            'tabindex="0"></div>') if live else ""
    return f'<aside class="side" aria-label="research-harness" id="sidebar">{top}{nav}{queue}{foot}{grip}</aside>'


def _swarm_settings(ctx):
    """Which models /swarm runs a brief on, and which one judges their work. Only Claude and Devin models are offered:
    a swarm never spends the ChatGPT plan."""
    swarm = ctx.get("swarm") or {}
    if not any(isinstance(m, str) for m in swarm.get("choices") or []):
        return ""
    boxes, judge_row = _swarm_rows(swarm)
    team = _active_team(swarm)
    row = ('<div class="team-row"><span class="team-now"><span class="soft">Team</span>'
           f'<b data-team-name>{_text(team["name"]) if team else "Quick swarm"}</b></span>'
           f'<button type="button" class="btn quiet small" data-team-open>{_ic("swarm", True)}Edit teams</button></div>'
           if swarm.get("team_models") else "")
    return ('<form action="/settings/swarm" method="post" data-swarm-form>'
            f'{_hidden(ctx, [])}<fieldset><legend>Swarm</legend>{row}'
            '<p class="set-hint">Start a brief with <code>/swarm</code> to run the team named here. Quick swarm runs '
            f'it on each model ticked below at once, and the judge keeps the best work.</p>'
            f'<div class="swarm-models">{boxes}</div>'
            f'<div class="swarm-opts">{judge_row}'
            '<label class="swarm-judge"><span>Check each run</span><input type="text" name="check" class="mono" '
            f'autocomplete="off" spellcheck="false" maxlength="300" placeholder="pytest -q" '
            f'value="{_text(swarm.get("check") or "")}"></label></div>'
            '<p class="set-hint">The harness runs it on each run\'s changes in a clean copy before the judge reads '
            'them. Leave empty to judge without it.</p></fieldset></form>')


def _team_presets(swarm):
    """The stored presets ({active, presets}) the page was given, or none."""
    data = swarm.get("presets")
    return data if isinstance(data, dict) and isinstance(data.get("presets"), list) else {"active": "quick", "presets": []}


def _active_team(swarm):
    """The active swarm preset, or None for the quick swarm (the ticks)."""
    data = _team_presets(swarm)
    return next((p for p in data["presets"] if isinstance(p, dict) and p.get("id") == data.get("active")), None)


def _team_sub(preset):
    """A team's subtitle, as team.js's sub() writes it: its lead's model, then its layers or 'side by side'."""
    def depth(node):
        kids = node.get("children") if isinstance(node, dict) and isinstance(node.get("children"), list) else []
        return 1 + max((depth(k) for k in kids if isinstance(k, dict)), default=0)
    tree = preset.get("tree") if isinstance(preset.get("tree"), dict) else {}
    lead = _names([tree["model"]]) if isinstance(tree.get("model"), str) and tree["model"] else "No lead"
    return f'{lead} · {f"{depth(tree)} layers" if preset.get("shape") == "vertical" else "side by side"}'


def _team_size(preset):
    """How many agents a preset has, as its editor draws them: every copy of every node. The lead's plan is the lead's
    own first pass, so it is not counted again."""
    def count(node):
        kids = node.get("children") if isinstance(node, dict) and isinstance(node.get("children"), list) else []
        return sum(max(1, int(k.get("count") or 1)) * count(k) for k in kids if isinstance(k, dict)) + 1
    tree = preset.get("tree") if isinstance(preset.get("tree"), dict) else {}
    return count(tree)


def _team_dialog(ctx):
    """Swarm teams: the presets, each a lead over managers over workers, edited by dragging (rh/web/team.js draws the
    tree from the JSON here). The form only carries the token; the script sends the whole set as its data field."""
    swarm = ctx.get("swarm") or {}
    if not (ctx["live"] and swarm.get("team_models")):
        return ""
    models = []
    for item in swarm.get("team_models") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            models.append({"id": item[0], "name": _names([item[0]]), "prov": item[1], "plan": item[1] == "codex"})
    data = _team_presets(swarm)
    seen = {m["id"] for m in models}
    names = {}
    for preset in data["presets"]:  # models a preset names that the policy does not list yet
        for role in preset.get("roles") or []:
            for m in role.get("models") or []:
                if m not in seen:
                    names[m] = _names([m])
        stack = [preset.get("tree") or {}]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("model") and node["model"] not in seen:
                    names[node["model"]] = _names([node["model"]])
                stack += node.get("children") or []
    quick = {"models": [m for m in swarm.get("models") or [] if isinstance(m, str)], "judge": swarm.get("judge") or ""}
    blob = json.dumps({"presets": data, "models": models, "names": names, "quick": quick, "template": hierarchy._roles(),
                       "quickNames": {"models": [_names([m]) for m in quick["models"]], "judge": _names([quick["judge"]])
                                      if quick["judge"] else ""},
                       "icons": {p: _ic(p, True) for p in ("claude", "devin", "openai")}})
    return ('<dialog class="team" id="team" aria-labelledby="team-title"><header><h2 id="team-title" tabindex="-1">'
            'Swarm teams</h2><span class="team-saved" data-team-saved role="status" aria-live="polite"></span>'
            '<form method="dialog"><button class="icon-btn" aria-label="Close swarm teams">×</button></form></header>'
            f'<form class="team-form" action="/settings/swarm-presets" method="post" data-team-form>{_hidden(ctx, [])}'
            '</form><form action="/settings/swarm-active" method="post" data-team-active>'
            f'{_hidden(ctx, [])}</form><div class="team-body" data-team-body>'
            '<p class="soft">Turn on scripts to edit swarm teams.</p></div>'
            f'<div id="team-data" data-json="{_text(blob)}" hidden></div></dialog>')


def _settings(ctx, preset, tint="cobalt"):
    choices = "".join(f'<label><input type="radio" name="background" value="{name}"'
                      f'{" checked" if name == preset else ""}><canvas class="background-preview" data-preview="{name}" '
                      f'aria-hidden="true"></canvas><span>{name.capitalize()}</span></label>' for name in PRESETS)
    tints = "".join(f'<label class="tint" title="{name.capitalize()}"><input type="radio" name="tint" value="{name}"'
                    f'{" checked" if name == tint else ""} aria-label="{name.capitalize()}">'
                    f'<span class="tint-dot" data-tint="{name}"></span></label>' for name in TINTS)
    return ('<dialog class="settings" id="settings" aria-labelledby="settings-title"><header>'
            '<h2 id="settings-title" tabindex="-1" autofocus>Settings</h2><form method="dialog"><button class="icon-btn" aria-label="Close settings">'
            '×</button></form></header><div class="settings-body"><form action="/appearance" method="post" '
            'data-appearance>'
            f'{_hidden(ctx, [])}<fieldset><legend>Background</legend><div class="background-choices">{choices}</div>'
            f'</fieldset><fieldset><legend>Colour</legend><div class="tint-choices">{tints}</div></fieldset></form>'
            f'{_swarm_settings(ctx)}<footer><button type="button" data-keys class="btn quiet small" hidden>'
            'Keyboard shortcuts <kbd>?</kbd></button></footer></div></dialog>' + _team_dialog(ctx))


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
    # A finished swarm is named by what the run its judge kept did when the judge itself led with "I chose run w1";
    # a judge that leads with the change's own title keeps it.
    for jid, m in list(meta.items()):
        runs = m.get("after") if isinstance(m, dict) and isinstance(m.get("after"), list) else []
        meta_line = _SELF_REMARK.match(str(m.get("said") or "")) if runs else None
        kept = str(m.get("kept") or "") if runs and (meta_line or not m.get("said")) else ""
        won = next((r for r in runs if isinstance(r, str) and kept in (r, r.rsplit(".", 1)[-1])), None) if kept else None
        said = str((meta.get(won) or {}).get("said") or "") if won and isinstance(meta.get(won), dict) else ""
        said = "" if _SELF_REMARK.match(said) else said  # the kept run on its own process names nothing either
        if said or meta_line:  # with no one run to name it by (a combined judge), the brief names it, not the remark
            meta = {**meta, jid: {**m, "said": said}}
    # A swarm's runs are its judge's work: the swarm is one row, live while any of its runs works. A run keeps a row
    # only while its own page is open, so the sidebar lights what the header names.
    # a job the status view no longer lists (a swarm's runs that ended long ago) keeps its state from the ledger
    states = {**{jid: m["state"] for jid, m in meta.items() if isinstance(m, dict) and isinstance(m.get("state"), str)},
              **{row["id"]: row["state"] for row, _ in rows}}
    swarms = {jid: [r for r in m["after"] if isinstance(r, str)] for jid, m in meta.items()
              if isinstance(m, dict) and isinstance(m.get("after"), list) and m["after"]}
    in_swarm = {r for runs in swarms.values() for r in runs}
    open_id = opened["job"].get("id") if opened and isinstance(opened.get("job"), dict) else None
    working = {jid for jid, runs in swarms.items() if any(states.get(r) in _LIVE for r in runs)}
    rows = [(({**row, "state": "running"}, "live") if row["id"] in working and where == "not_started" else (row, where))
            for row, where in rows if row["id"] not in in_swarm or row["id"] == open_id]
    known = {row["id"] for row, _ in rows}
    # the open job always has a row, however far back it ended, so the sidebar lights what the header names
    shown = opened.get("job") if opened else None
    if isinstance(shown, dict) and shown.get("id") and shown["id"] not in known:
        spec = shown.get("spec") if isinstance(shown.get("spec"), dict) else {}
        state = shown.get("state")
        rows.append(({"id": shown["id"], "state": state, "paused": False, "detail": shown.get("detail", ""),
                      "external_id": shown.get("external_id")},
                     "live" if state in _LIVE else "not_started" if state in _PRE else "recent"))
        known.add(shown["id"])
        if shown["id"] not in meta:
            meta = {**meta, shown["id"]: {"brief": spec.get("brief", ""), "kind": shown.get("kind") or spec.get("kind", ""),
                                          "created": shown.get("created"), "updated": shown.get("updated")}}
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
           "workspace": view.get("workspace") or {},
           # the composer's folder menu, when the server offers one (it may list none yet)
           "folders": None if view.get("folders") is None else
           [tuple(f) for f in view["folders"] if isinstance(f, (list, tuple)) and len(f) == 2],
           "repo_path": str(view.get("repo_path") or ""),
           "states": states, "swarms": swarms,
           "swarm": view.get("swarm") if isinstance(view.get("swarm"), dict) else {},
           "back": back, "meta": meta, "ready": ready, "setup": setup, "native": native, "midnight": _midnight(now),
           "followed": {str(m.get("prior")) for m in meta.values() if isinstance(m, dict) and m.get("prior")},
           "rerun": _reruns(meta),
           "others": [s for s in view.get("stops", []) if s.get("owner") != "app"
                      and not str(s["scope"]).startswith("job:")]}
    jobs = [_Job(row, where, ctx) for row, where in rows]
    names = {}
    for j in jobs:  # runs that share a name in the lists are told apart by their ids; the runs of one chain share
        # its first said as one name, so chain-mates never mark each other
        names.setdefault(j.label, set()).add(j.root if j.label == j.said else j.id)
    for j in jobs:
        j.dup = len(names[j.label]) > 1
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
        pane = _home(jobs, stop_rows, loose, view, ctx, feedback, "New job")
    else:
        page, title = "home", "Research harness"
        pane = _home(jobs, stop_rows, loose, view, ctx, feedback)
    if live and page != "inbox":
        pane = f'<div class="detail">{pane}</div>'
    wait = len(ctx["needs"])
    queue = _lane(jobs, stop_rows, ctx, named) if live else ""
    side = _sidebar(ctx, view, page, wait, queue)
    fast = any(j.state in ("draft", "launching", "collecting") or (j.state == "running" and j.kind == "local-shell")
               or (j.state == "ready" and not j.row.get("detail")) for j in jobs)
    loop = view.get("checks") if isinstance(view.get("checks"), dict) else None
    pace = "fast" if fast and loop and loop.get("mode") == "on" else "calm"
    preset = _LEGACY.get(view.get("background"), view.get("background"))
    if preset not in PRESETS:
        preset = "beam"
    tint = view.get("tint") if view.get("tint") in TINTS else "cobalt"
    if live:
        head = ('<noscript><meta http-equiv="refresh" content="30"></noscript>\n'
                '<script src="/app.js" defer></script>\n')
        keys = "".join(f"<dt><kbd>{k}</kbd></dt><dd>{v}</dd>" for k, v in _KEYS)
        extra = (_settings(ctx, preset, tint) + '<div class="toasts" aria-live="polite"></div>'
                 '<dialog id="keys" aria-label="Keyboard shortcuts">'
                 f'<p class="title" tabindex="-1" autofocus>Keyboard</p><dl>{keys}</dl>'
                 f'<div class="foot"><a href="{_url(ctx, "/actions")}">Every action as a plain form</a>'
                 '<form method="dialog"><button class="btn">Close</button></form></div></dialog>\n'
                 + _palette(jobs, ctx, set(re.findall(r'data-k="([a-z])"', pane + side))) + "\n")
        style = _FACES + STYLE + '\n:root{--halftone-image:url(/halftone.webp);--clouds-image:url(/clouds.webp);--lens:url(#lg-lens)}'
    else:
        head, extra, style = "", "", STYLE
    live_marker = ' data-live-app' if live else ''
    build = f' data-build="{BUILD}"' if live else ''
    field = ('<canvas class="bg-field" aria-hidden="true"></canvas>'
             '<svg class="lg-defs" width="0" height="0" aria-hidden="true" focusable="false"><filter id="lg-lens" x="0" y="0" width="1" height="1" color-interpolation-filters="sRGB"><feFlood flood-color="#fff" result="box"/><feGaussianBlur in="box" stdDeviation="18" result="soft"/><feColorMatrix in="soft" type="matrix" values="0 0 0 1 0  0 0 0 1 0  0 0 0 1 0  0 0 0 0 1" result="field"/><feConvolveMatrix in="field" order="3" kernelMatrix="0 0 0  -1 0 1  0 0 0" divisor="0.03" bias="0.5" preserveAlpha="true" edgeMode="duplicate" result="gx"/><feConvolveMatrix in="field" order="3" kernelMatrix="0 -1 0  0 0 0  0 1 0" divisor="0.03" bias="0.5" preserveAlpha="true" edgeMode="duplicate" result="gy"/><feColorMatrix in="gx" type="matrix" values="1 0 0 0 0  0 0 0 0 0  0 0 0 0 0.5  0 0 0 0 1" result="rx"/><feColorMatrix in="gy" type="matrix" values="0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 0 1" result="ry"/><feComposite in="rx" in2="ry" operator="arithmetic" k1="0" k2="1" k3="1" k4="0" result="map"/><feDisplacementMap in="SourceGraphic" in2="map" scale="90" xChannelSelector="R" yChannelSelector="G"/></filter></svg>') if live else ''
    sidebar_toggle = field + (f'<button class="icon-btn sidebar-toggle" type="button" aria-label="Toggle sidebar" '
                      f'aria-expanded="false" aria-controls="sidebar">{_ic("sidebar")}</button>'
                      '<div class="side-scrim" aria-hidden="true"></div>') if live else ''
    return (f'<!doctype html>\n<html lang="en"{live_marker}>\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{_text(title)}</title>\n'
            f'{head}<style>{style}</style>\n</head>\n<body data-now="{now:.3f}" data-open="{_text(page)}" '
            f'data-here="{_text("job:" + named if named else "")}" '
            f'data-pace="{pace}" data-jobs="{1 if jobs else 0}" data-background="{preset}" data-tint="{tint}"{build}>\n'
            f'<div class="app">{sidebar_toggle}{side}<main class="pane" data-pane="{_text(page)}" '
            f'tabindex="-1">{pane}</main></div>\n'
            f'{extra}</body>\n</html>\n')
