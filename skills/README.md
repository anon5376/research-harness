# Skills

Agent skills for running research work on Codex and Claude workers. Each folder holds a `SKILL.md`; copy the folder into your agent's skills directory to use it.

- [codex-cloud-loop](codex-cloud-loop/SKILL.md): run Codex cloud tasks as workers, from environment setup to an accepted patch.
- [claude-worker-loop](claude-worker-loop/SKILL.md): run Claude Code workers (subagents, workflows, background and desktop sessions, cloud runs) from brief to accepted result.
- [unlazy](unlazy/SKILL.md): acceptance gates written before the work and re-run before any done report; both loops tell their workers to load it.

## Attribution

- `unlazy/` is vendored unchanged from unlazy by Leonxlnx (MIT, Copyright (c) 2026 Leonxlnx; see `unlazy/LICENSE`). Its `package.json` and `agents/` metadata are left out. It refers to a `SECURITY.md` and a `research/` folder that this copy does not include.
- The structure of `codex-cloud-loop` and `claude-worker-loop` (budget, numbered steps, exact briefs in code blocks, stop rules) is adapted from `sol-loop` in [t3dotgg/tstack](https://github.com/t3dotgg/tstack) (MIT, © 2026 Theo Browne).
