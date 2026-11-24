# GPT Pro jobs in ChatGPT, through an MCP tunnel

Give a `gpt-chat` job to GPT Pro without copying the brief around. You still open ChatGPT and pick the model
yourself. GPT Pro then reads the job and the repo through `rh mcp`, and on plans that allow it, hands its answer
straight back.

```
ChatGPT (GPT Pro) ──MCP──▶ OpenAI tunnel ◀──polls── tunnel-client (this Mac) ──stdio──▶ rh mcp ──▶ ledger + repo
```

Nothing listens on a port. `tunnel-client` only makes outbound HTTPS calls to OpenAI and starts `rh mcp` as a
child process. ([OpenAI: Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels))

## What GPT Pro can do

| tool | what it does | kind |
|---|---|---|
| `list_jobs` | GPT Pro jobs waiting for an answer | read |
| `get_job` | one job's full brief, word for word | read |
| `list_files` | files git tracks in the repo | read |
| `read_file` | one tracked file, in 200,000-character pages | read |
| `submit_reply` | saves the answer and ends the job `done` | write |

Only tracked files can be read, so ignored files like `.env` never leave the machine.

**Plans:** on ChatGPT Pro, custom MCP servers get read tools only, so `submit_reply` isn't offered. Paste the
answer back with `rh chat reply <job> <file>` (or the app's reply box). On Business, Enterprise and Edu it works,
and ChatGPT asks before running it. ([OpenAI help](https://help.openai.com/en/articles/12584461))

## Setup (once)

1. **Install tunnel-client.** Get `tunnel-client-runtime-<version>-darwin-arm64.zip` from
   [openai/tunnel-client releases](https://github.com/openai/tunnel-client/releases/latest), check it against
   `SHA256SUMS.txt`, and put the `tunnel-client-runtime` binary at `~/.local/bin/tunnel-client`.
2. **Create a tunnel** at [Platform → Settings → Tunnels](https://platform.openai.com/settings/organization/tunnels),
   then run:
   ```bash
   bin/rh config set mcp.tunnel_id tunnel_<the 32 hex characters>
   ```
3. **Create a Restricted API key** at [Platform → API keys](https://platform.openai.com/settings/organization/api-keys)
   with only **Tunnels: Read** and **Tunnels: Use**. A key limited to that can't run models, so it can't spend.
   Save it alone in a file only you can read:
   ```bash
   mkdir -p ~/.research-harness/secrets && pbpaste > ~/.research-harness/secrets/openai-tunnel.key && chmod 600 ~/.research-harness/secrets/openai-tunnel.key
   ```
4. **Start the tunnel** and leave it running:
   ```bash
   bin/rh mcp --tunnel
   ```
   It passes the key by file reference and drops `OPENAI_API_KEY` from tunnel-client's environment, so the
   tunnel never falls back to a key that can spend. Logs go to `~/.research-harness/tunnel.log`.
5. **Add it in ChatGPT:** [Plugins](https://chatgpt.com/plugins) → **+** → **Add custom MCP server** → name it
   `research-harness` → Connection **Tunnel** → pick your tunnel → Authentication **No authentication** → tick
   **I understand and want to continue** → **Create as a plugin**.

## Use

In a new ChatGPT chat, pick GPT Pro, type `@research-harness` and ask: *take job `<id>`*, or *what jobs are
waiting?* GPT Pro reads the brief, opens the files it needs and writes the answer. On Pro, paste the answer back
with `rh chat reply`.

## Limits worth knowing

- Each tool call must finish within OpenAI's 120-second tunnel deadline. Ours are local reads, so they take
  milliseconds.
- Anyone who can use the plugin in your ChatGPT workspace can read this repo's tracked files and answer its GPT
  Pro jobs. Keep the plugin private to you.
