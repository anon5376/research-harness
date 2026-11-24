# GPT Pro jobs in ChatGPT, through an MCP tunnel

Give a `gpt-chat` job to GPT Pro without copying the brief around. You still open ChatGPT and pick the model
yourself. GPT Pro then reads the job and the repo through `rh mcp`, and on plans that allow it, hands its answer
straight back.

```
ChatGPT (GPT Pro) ──MCP──▶ OpenAI tunnel ◀──polls── tunnel-client (this Mac) ──stdio──▶ rh mcp ──▶ ledger + repo
```

No public port is opened. `tunnel-client` makes outbound HTTPS calls to OpenAI, starts `rh mcp` as a child process,
and serves its health check on a random loopback port only.
([OpenAI: Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels))

## What GPT Pro can do

| tool | what it does | kind |
|---|---|---|
| `list_jobs` | GPT Pro jobs waiting for an answer | read |
| `get_job` | one waiting job's full brief, word for word | read |
| `list_files` | the repo's files as committed at `HEAD` | read |
| `read_file` | one committed file, in 200,000-character pages | read |
| `submit_reply` | saves the answer and ends the job `done` | write |

Files are read from git's own copy at `HEAD`, so uncommitted edits, ignored files like `.env`, and anything a
committed symlink points at never leave the machine. Commit what GPT Pro should see before you ask it.

**Plans:** on ChatGPT Pro, custom MCP servers get read tools only, so `submit_reply` isn't offered. GPT Pro writes
the answer in the chat and you paste it back with `rh chat reply <job> <file>` (or the app's reply box). On
Business, Enterprise and Edu it works, and ChatGPT asks before running it.
([OpenAI help](https://help.openai.com/en/articles/12584461))

## Setup (once)

1. **Install tunnel-client.** Get `tunnel-client-runtime-<version>-darwin-arm64.zip` from
   [openai/tunnel-client releases](https://github.com/openai/tunnel-client/releases/latest), check it against
   `SHA256SUMS.txt`, and put the `tunnel-client-runtime` binary at `~/.local/bin/tunnel-client`.
2. **Create a tunnel** at [Platform → Settings → Tunnels](https://platform.openai.com/settings/organization/tunnels).
   That needs Tunnels **Read** and **Manage** on your Platform organization. Associate the tunnel with your ChatGPT
   workspace as well as the Platform organization, or ChatGPT won't list it. Then run:
   ```bash
   bin/rh config set mcp.tunnel_id tunnel_<the rest of its id>
   ```
3. **Create a Restricted API key** at [Platform → API keys](https://platform.openai.com/settings/organization/api-keys)
   with only **Tunnels: Read** and **Tunnels: Use**. A key limited to that can't run models, so it can't spend.
   Copy it, then save it alone in a file only you can read (the `umask` makes the file private before the key is
   written):
   ```bash
   H="${RH_HOME:-$HOME/.research-harness}"; (umask 077 && mkdir -p "$H/secrets" && pbpaste > "$H/secrets/openai-tunnel.key")
   ```
4. **Start the tunnel** and leave it running:
   ```bash
   bin/rh mcp --tunnel
   ```
   It passes the key by file reference and clears tunnel-client's own environment settings, so the tunnel never
   falls back to an `OPENAI_API_KEY` that can spend or to an endpoint set elsewhere. Raw HTTP logging stays off.
   The log is `tunnel.log` in your harness home.
5. **Add it in ChatGPT:** [Plugins](https://chatgpt.com/plugins) → **+** → **Add custom MCP server** → name it
   `research-harness` → Connection **Tunnel** → pick your tunnel → Authentication **No authentication** → tick
   **I understand and want to continue** → **Create as a plugin**. Then find it under your plugins and
   **install** it. Picking the tunnel here needs Tunnels **Read** and **Use**.

## Use

In a new ChatGPT chat, pick GPT Pro, type `@research-harness` and ask: *take job `<id>`*, or *what jobs are
waiting?* GPT Pro reads the brief, opens the files it needs and writes the answer. On Pro, paste the answer back
with `rh chat reply`.

## Limits worth knowing

- OpenAI's tunnel gives each call 120 seconds and 10 MiB. A file over 8 MB is refused, and long files come in pages.
- Anyone who can use the plugin in your ChatGPT workspace can read this repo's committed files and answer its GPT
  Pro jobs. Keep the plugin private to you.
