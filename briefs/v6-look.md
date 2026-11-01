# Look v6: one workspace for GPT Pro, Codex cloud and This Mac

Founder, 1 Nov: a near-full redesign of the layout. Desktop only; the light theme is the dark one inverted; GPT Pro
chats and Codex cloud are first-class; the coordination system stays out of sight and is run from one control; every
screen compact and fitting the window; Linear's list and Claude Code's conversation as the models.

## Layout (desktop, the window at least 1024 x 640)

```
┌ sidebar 224 ──┬ lane 384 ───────────────────┬ detail (the rest) ──────────────────────────┐
│ ▣ research-h… │ Jobs   All Chat Cloud Mac  + │ crumbs · state                    actions   │
│ ⌕  ✎          │ Needs you 3                  │ Title of the job                            │
│ Inbox     3   │ ◐ Ask GPT Pro: …     ◇ 13:02 │ ● GPT Pro · gpt-6.1-sol-pro · 13:02 · seat  │
│ Jobs          │ Live 1                       │                                             │
│               │ ◔ Keep a process …   ▭ 13:27 │ You ─ the brief, as written                 │
│ WORKERS       │ Up next                      │ ┌ Needs you: copy the prompt, paste reply ┐ │
│ ◇ GPT Pro   1 │ Ended today                  │ └─────────────────────────────────────────┘ │
│ ☁ Codex     4 │ Earlier                      │ GPT Pro ─ the reply                         │
│ ▭ This Mac  2 │                              │ Activity ─ steps, checks, receipt           │
│               │                              │ ┌ Ask for a change / run again … ⌘↵ ──────┐ │
│ ● Checks on ▾ │                              │ └─────────────────────────────────────────┘ │
└───────────────┴──────────────────────────────┴─────────────────────────────────────────────┘
```

- **Sidebar.** Workspace, search (⌘K) and new (N); Inbox and Jobs; a Workers section (GPT Pro, Codex cloud, This
  Mac), each a filter of the lane with its live count. At the foot one control, the coordination pill.
- **Lane.** Linear's list: a 44 px head with the view's name, the worker filter and New; groups (Needs you, Live,
  Up next, Ended today, Earlier) of 36 px rows: state ring, title, worker icon, time. The open job's row is lit.
- **Detail.** Claude Code's conversation for the open job: the title and one line of properties, then the brief as
  "You", what it needs from you as cards, the reply (GPT Pro) or output (Codex, This Mac), the activity, and a
  composer pinned at the bottom (ask for a change, run again). With no job open it holds the sea: a jellyfish per
  seat and the counts the page shows, and New opens the composer there.
- **Coordination, hidden.** The pill at the sidebar's foot says one thing ("Checks on · 2 live"); it opens a panel
  with the check loop (check now, pause), Codex cloud (pause launches, pacing), This Mac (load, memory, the heavy
  lease) and the seats. Nothing else of it is on screen.

## GPT Pro chats

A job of kind `gpt-chat` (backend: briefs C1 in the lead's notes). The composer's worker picker reads GPT Pro, Codex
cloud, This Mac. A chat job waits on you as soon as it is added: its card holds the prompt with Copy, a link to
ChatGPT, and a box for the reply; saving the reply ends the job done, and the reply reads as the next message.

## Theme

Dark is the base. Light is its inverse: the same steps of grey from frame to raised turned over, ink near black, and
each hue darkened until it reads at 4.5:1 on every surface (test_text_contrast_in_both_schemes).
