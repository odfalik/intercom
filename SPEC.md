# Intercom — Agent-to-Agent Communication Channel Plugin

MCP channel plugin that lets independent Claude Code sessions send messages to each other, delivered as real-time push notifications. Modeled on the [slack-channel plugin](~/repos/golem/channel-plugin/).

## Identity

- **Name** = the agent's tmux window name, resolved live every time via `tmux display-message -p -t $TMUX_PANE "#{window_name}"`
- **Stable key** = `$TMUX_PANE` (set once at shell launch, never changes even when the window is renamed)
- No registration, no roles, no metadata. The name is just an address.
- Renaming a tmux window instantly changes how that agent is addressed.
- tmux-only. No `AGENT_NAME` env var fallback. Good error messages if `$TMUX_PANE` is unset.

## Liveness

- Each MCP server instance holds an exclusive `flock` on `~/.config/intercom/agents/{pane_id}.lock`
- OS releases the lock when the process dies (Claude Code exit → MCP server dies → lock released)
- `who()` iterates lock files, tries non-blocking lock on each:
  - Lock held → alive → query tmux for current window name
  - Lock acquired → dead → clean up file, release
- No heartbeats, no polling, no stale entries

## Addressing

- Agents are addressed by current tmux window name
- **Name collisions**: if multiple panes share a window name, messages are delivered to all of them (multicast)
- **Scope**: global — one event bus, one set of lock files. Agents across different projects/tmux sessions can communicate.
- **Unknown recipient**: `send` fails immediately with error: `No agent named '{name}' found. Active agents: x, y, z. Consider listing all active agents to see if it should go to someone else.`

## Architecture

Same three-tier pattern as the slack-channel plugin, minus leader election (no external connection needed):

1. **JSONL event bus** (`~/.config/intercom/events.jsonl`) — append-only, all messages written here
2. **Event bus tailing** — each MCP server instance tails the bus every 0.5s, filters for messages addressed to its current name
3. **Push notification** — matching messages delivered via `notifications/claude/channel` (raw JSON-RPC write to `_write_stream`, bypassing MCP SDK validation)

No leader election. No cold replies. Each instance is a peer.

### Message flow

1. Agent A calls `send(to="caching", message="are you using pd-postman-vae-cache?")`
2. MCP server resolves "caching" → checks lock files → finds the pane(s) with window name "caching"
3. If found: writes `{to: "caching", from_pane: "%42", from_name: "training", message: "...", ts: "..."}` to event bus
4. If not found: returns error immediately
5. Agent B's MCP server tails the bus, sees a message addressed to its current window name
6. Delivers via `notifications/claude/channel` with `source="intercom"`, `sender="training"`
7. Agent B sees: `<channel source="intercom" sender="training">training: are you using pd-postman-vae-cache?</channel>`
8. Agent B can respond with `send(to="training", message="yes, need ~2 more hours")`

## Tools

### `send(to, message)`
- `to`: string or list of strings — recipient agent name(s) (tmux window names)
- `message`: string — plain text message
- Resolves each name to active pane(s), fails if any name has no match
- Multicast: if `to` is a list, or if a name matches multiple panes, delivers to all
- Sender name (current tmux window name) is resolved live and included in the event

### `who()`
- Returns list of current tmux window names for all active agent instances
- No arguments
- Liveness determined by lock files, names resolved live from tmux

## Notification format

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/claude/channel",
  "params": {
    "content": "training: are you using pd-postman-vae-cache?",
    "meta": {
      "sender": "training",
      "sender_pane": "%42",
      "ts": "1718000000.123"
    }
  }
}
```

Rendered in session as: `<channel source="intercom" sender="training">training: are you using pd-postman-vae-cache?</channel>`

## Event bus format

One JSON object per line in `~/.config/intercom/events.jsonl`:

```json
{"to": "caching", "from_pane": "%42", "from_name": "training", "message": "are you using pd-postman-vae-cache?", "ts": "1718000000.123"}
```

- Agents start from end of file on startup (blank slate, no replay)
- File can be truncated/rotated; instances detect this via size check and reset position

## Shared state

```
~/.config/intercom/
  agents/{pane_id}.lock    # flock per instance (liveness)
  events.jsonl             # append-only event bus
  sessions/{pid}.json      # conversation_id written by SessionStart hook
```

## Hooks

`SessionStart` hook writes conversation ID (for future use / debugging):

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/on-session-start.sh",
        "timeout": 5
      }]
    }]
  }
}
```

## Session discovery

Same pattern as slack-channel: `on-session-start.sh` writes `{conversation_id}` to `~/.config/intercom/sessions/$PPID.json`. MCP server resolves via process tree walk (ppid up to 5 levels).

## MCP server setup

- Python, low-level MCP server (not FastMCP — need raw `_write_stream` access)
- Dependencies: `mcp>=1.26.0`, `python-dotenv>=1.0.0`
- No Slack dependencies
- Entry point: `intercom.server:run`
- Run from repo: configured in `.mcp.json` as `uv run --directory ~/repos/intercom intercom-server`
- Declares `claude/channel` experimental capability

## Error handling

- `$TMUX_PANE` not set → clear error: "intercom requires tmux. Start Claude Code inside a tmux session."
- `tmux` binary not found → clear error
- tmux server not running → clear error
- Window name query fails for a pane → skip that pane, log warning

## Implementation notes

1. **Reference implementation**: `~/repos/golem/channel-plugin/slack_channel/server.py` is the architectural template. Critical patterns to copy:
   - Raw `_write_stream` notification bypass (line 558-559)
   - Event bus tailing loop (line 684-757)
   - `stdio_server` + `ServerSession` setup (line 858-898)
   - Do NOT use FastMCP — it doesn't expose the low-level write stream needed for `notifications/claude/channel`

2. **Notification delivery** is the hardest part. The MCP SDK rejects custom notification methods through its typed union validation, so you must write raw JSON-RPC directly to `_session._write_stream.send(SessionMessage(...))`. See `_send_channel_notification` in the reference.

3. **tmux pane ID gotcha**: `$TMUX_PANE` looks like `%42` — the `%` needs careful handling in file paths for lock files. Sanitize it (e.g., strip the `%` or replace with something filesystem-safe).

4. **Plugin structure**: needs `.claude-plugin/plugin.json` and `hooks/` for the SessionStart hook. Copy the structure from the slack-channel plugin.

5. **Testing**: two tmux windows, each running claude with the channel plugin loaded:
   ```bash
   claude --dangerously-load-development-channels server:intercom
   ```
   Send a message from one to the other and verify the channel notification appears.

## What this is NOT

- No threading / conversation grouping — flat messages, agents are smart enough
- No message persistence / history replay — blank slate on startup
- No structured data — plain text only
- No roles / registration / presence metadata
- No cold replies / auto-responders
- No Slack integration (that's the slack-channel plugin's job)
