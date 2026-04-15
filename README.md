# Intercom

Agent-to-agent communication for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Independent Claude Code sessions can send messages to each other in real time via push notifications. No shared files, no polling, no "write to a markdown file and hope the other agent reads it." Messages are delivered directly into the recipient's session the moment they're sent.

<img width="924" height="102" alt="image" src="https://github.com/user-attachments/assets/1cd743f4-553a-400c-870f-d205c4474b5c" />

Each session is an agent, addressed by its tmux window name (or a static `AGENT_NAME`). Agents discover each other automatically.

```
┌─────────────────────┐         ┌─────────────────────┐
│  tmux: "training"   │         │  tmux: "eval"        │
│  Claude Code + MCP  │◄───────►│  Claude Code + MCP   │
└─────────────────────┘  event  └─────────────────────┘
                         bus
                    (~/.config/intercom/events.jsonl)
```


## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) with channel plugin support
- [uv](https://docs.astral.sh/uv/)
- Python 3.10+
- [tmux](https://github.com/tmux/tmux) (or set `AGENT_NAME` env var as a fallback)

## Install

Clone the repo:

```bash
git clone https://github.com/odfalik/intercom.git
```

Add the MCP server to your global config (`~/.mcp.json`):

```json
{
  "mcpServers": {
    "intercom": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/intercom", "intercom-server"]
    }
  }
}
```

## Running Claude Code with Intercom

Intercom is a **channel plugin**. Launch Claude Code with:

```bash
claude --dangerously-load-development-channels server:intercom
```

Combine with other flags as needed:

```bash
claude --allow-dangerously-skip-permissions \
       --dangerously-load-development-channels server:intercom
```

Set up an alias to keep it short:

```bash
alias c="claude --allow-dangerously-skip-permissions --dangerously-load-development-channels server:intercom"
```

Then run `c` in any tmux window. The window name becomes the agent's address.

### Without tmux

If you're not using tmux, set `AGENT_NAME` before launching:

```bash
AGENT_NAME=training claude --dangerously-load-development-channels server:intercom
```

The name is static for the lifetime of the session (no live rename like tmux).

## Usage

Claude Code gets two tools:

### `who()`

Lists all active agents:

```
Active agents (3):
  training [%42]
  eval [%55]
  data-prep [%60] (you)
```

### `send(to, message)`

Send a message to another agent by name:

```
send(to="training", message="what epoch are you on?")
→ Message sent to: training
```

The recipient gets a push notification and can reply:

```
send(to="data-prep", message="epoch 47, loss 0.023")
```

Send to multiple agents:

```
send(to=["training", "eval"], message="shutting down the cluster in 10 min")
```

## How It Works

**Identity**: In tmux, each agent is addressed by its window name, resolved live. The stable key is `$TMUX_PANE`, which never changes even if you rename the window. Without tmux, `AGENT_NAME` provides a static name.

**Liveness**: Each MCP server holds an exclusive `flock` on a lock file. The OS releases the lock when the process dies, no matter how. `who()` checks liveness by trying to grab each lock: held means alive, acquirable means dead (clean up the file). No heartbeats, no polling, no stale entries.

**Messaging**: Messages are appended to a shared JSONL event bus. Each MCP server tails the bus, filters for messages addressed to its name, and delivers matches as push notifications via `notifications/claude/channel`.

**No replay**: Agents start reading from the end of the event bus. No history, no catch-up, just live messages.

## Shared State

```
~/.config/intercom/
  agents/{key}.lock        # flock per instance (liveness)
  events.jsonl             # append-only event bus
  sessions/{pid}.json      # conversation ID (written by SessionStart hook)
```

## Limitations

- **Development channels flag**: channel plugins require `--dangerously-load-development-channels`, which shows a confirmation prompt on every launch. This is a Claude Code limitation, not an intercom one.
- **Same machine only**: communication is via the local filesystem
- **No message history**: agents start with a blank slate
- **Plain text only**: no structured data, no attachments
- **No threading**: flat messages

## License

MIT
