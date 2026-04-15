# Intercom

Agent-to-agent communication for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Let independent Claude Code sessions send messages to each other in real time.

Each Claude Code session becomes an agent, addressed by its tmux window name. Agents discover each other automatically and communicate via push notifications — no configuration, no registration, no central server.

```
┌─────────────────────┐         ┌─────────────────────┐
│  tmux: "training"   │         │  tmux: "eval"        │
│  Claude Code + MCP  │◄───────►│  Claude Code + MCP   │
└─────────────────────┘  event  └─────────────────────┘
                         bus
                    (~/.config/intercom/events.jsonl)
```

<img width="768" height="63" alt="image" src="https://github.com/user-attachments/assets/2093eeff-bbb1-498d-a8d5-51feccee03c1" />


## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (with channel plugin support)
- [tmux](https://github.com/tmux/tmux) — each agent runs in a tmux window
- [uv](https://docs.astral.sh/uv/) — Python package manager
- Python 3.10+

## Install

Clone the repo:

```bash
git clone https://github.com/odfalik/intercom.git
cd intercom
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

Intercom is a **channel plugin**, which means it needs the development channels flag:

```bash
claude --dangerously-load-development-channels server:intercom
```

That's the key flag. Combine it with whatever other flags you use:

```bash
# Example: with permissions bypass and multiple channel plugins
claude --allow-dangerously-skip-permissions \
       --dangerously-load-development-channels server:intercom

# Tip: alias it
alias c="claude --allow-dangerously-skip-permissions --dangerously-load-development-channels server:intercom"
```

Then just run `c` in any tmux window. The window name becomes the agent's address.

## Usage

Once running, Claude Code gets two tools:

### `who()`

Lists all active agents:

```
Active agents (3):
  training [%42]
  eval [%55]
  data-prep [%60] (you)
```

### `send(to, message)`

Send a message to another agent by window name:

```
send(to="training", message="what epoch are you on?")
→ Message sent to: training
```

The recipient sees it as a push notification and can reply:

```
send(to="data-prep", message="epoch 47, loss 0.023")
```

Messages to multiple agents:

```
send(to=["training", "eval"], message="shutting down the cluster in 10 min")
```

## How It Works

**Identity**: Each agent's identity is its tmux window name, resolved live via `tmux display-message`. The stable key is `$TMUX_PANE` (e.g., `%42`), which never changes even if you rename the window. Rename your window and you're instantly reachable at the new name.

**Liveness**: Each MCP server holds an exclusive `flock` on `~/.config/intercom/agents/{pane_id}.lock`. The OS releases the lock when the process dies — no heartbeats, no polling, no stale entries. `who()` checks liveness by trying to grab each lock: held = alive, acquirable = dead (clean up the file).

**Messaging**: Messages are appended to a shared JSONL event bus (`~/.config/intercom/events.jsonl`). Each MCP server tails the bus, filters for messages addressed to its current window name, and delivers matches as `notifications/claude/channel` push notifications.

**No replay**: Agents start reading from the end of the event bus. No history, no catch-up — just live messages from the point they joined.

## Shared State

```
~/.config/intercom/
  agents/{pane_id}.lock    # flock per instance (liveness)
  events.jsonl             # append-only event bus
  sessions/{pid}.json      # conversation ID (written by SessionStart hook)
```

## Limitations

- **tmux only** — agents must run inside tmux sessions
- **Same machine** — communication is via the local filesystem
- **No message history** — agents start with a blank slate
- **Plain text only** — no structured data, no attachments
- **No threading** — flat messages (agents are smart enough to track context)

## License

MIT
