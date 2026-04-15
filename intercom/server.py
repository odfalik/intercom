"""Intercom — agent-to-agent MCP channel plugin.

Lets independent Claude Code sessions (in tmux) send messages to each other,
delivered as real-time push notifications via notifications/claude/channel.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio

import mcp.types as types
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

logger = logging.getLogger("intercom")

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_session: ServerSession | None = None

_STATE_DIR = Path.home() / ".config" / "intercom"
_AGENTS_DIR = _STATE_DIR / "agents"
_EVENTS_FILE = _STATE_DIR / "events.jsonl"
_SESSIONS_DIR = _STATE_DIR / "sessions"

# Our agent key and lock file handle (kept open for lifetime of process)
_agent_key: str | None = None  # tmux pane ID (e.g. "%42") or "pid_{N}" for non-tmux
_static_name: str | None = None  # set when using AGENT_NAME (no tmux)
_lock_fh: Any = None  # file handle held open with flock


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def _resolve_identity() -> tuple[str, str | None]:
    """Return (agent_key, static_name).

    In tmux: agent_key = $TMUX_PANE, static_name = None (resolved live).
    With AGENT_NAME: agent_key = "pid_{PID}", static_name = $AGENT_NAME.
    """
    pane = os.environ.get("TMUX_PANE")
    if pane:
        return pane, None

    name = os.environ.get("AGENT_NAME")
    if name:
        return f"pid_{os.getpid()}", name

    raise RuntimeError(
        "intercom requires either tmux ($TMUX_PANE) or $AGENT_NAME to be set."
    )


def _key_to_filename(key: str) -> str:
    """Sanitize agent key for use as filename. '%42' -> 'pane_42', 'pid_123' -> 'pid_123'."""
    if key.startswith("%"):
        return "pane_" + key.lstrip("%")
    return key


def _get_window_name(pane: str) -> str | None:
    """Query tmux for the window name of a pane. Returns None for non-tmux keys."""
    if not pane.startswith("%"):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{window_name}"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _my_name() -> str:
    """Get our current name. Live tmux lookup, or static AGENT_NAME."""
    if _static_name:
        return _static_name
    name = _get_window_name(_agent_key) if _agent_key else None
    return name or "unknown"


# ---------------------------------------------------------------------------
# Liveness: flock-based agent registration
# ---------------------------------------------------------------------------

def _acquire_lock() -> None:
    """Acquire exclusive flock on our agent lock file. Held for process lifetime."""
    global _lock_fh
    _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _AGENTS_DIR / f"{_key_to_filename(_agent_key)}.lock"
    _lock_fh = open(lock_path, "w")
    fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Write PID on first line; static name on second line (for non-tmux agents)
    _lock_fh.write(str(os.getpid()) + "\n")
    if _static_name:
        _lock_fh.write(_static_name + "\n")
    _lock_fh.flush()


def _is_alive(lock_path: Path) -> bool:
    """Check if a lock file is held (agent is alive)."""
    try:
        fh = open(lock_path, "r")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # We got the lock → agent is dead. Release and clean up.
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
            try:
                lock_path.unlink()
            except OSError:
                pass
            return False
        except (OSError, BlockingIOError):
            # Lock held → agent is alive
            fh.close()
            return True
    except OSError:
        return False


def _key_from_lockfile(lock_path: Path) -> str:
    """Extract agent key from lock filename. 'pane_42.lock' -> '%42', 'pid_123.lock' -> 'pid_123'."""
    stem = lock_path.stem
    if stem.startswith("pane_"):
        return "%" + stem.removeprefix("pane_")
    return stem


def _read_static_name(lock_path: Path) -> str | None:
    """Read the static name (second line) from a lock file, if present."""
    try:
        with open(lock_path) as f:
            lines = f.readlines()
            if len(lines) >= 2:
                return lines[1].strip()
    except OSError:
        pass
    return None


def _who() -> list[dict[str, str]]:
    """Return list of active agents with their keys and names."""
    _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    agents = []
    for lock_path in _AGENTS_DIR.glob("*.lock"):
        if not _is_alive(lock_path):
            continue
        key = _key_from_lockfile(lock_path)
        # tmux agents: resolve name live. Non-tmux: read static name from lock file.
        name = _get_window_name(key) or _read_static_name(lock_path)
        if name is None:
            continue
        agents.append({"pane": key, "name": name})
    return agents


def _resolve_name(name: str) -> list[str]:
    """Resolve a window name to list of active pane IDs. Raises if none found."""
    agents = _who()
    panes = [a["pane"] for a in agents if a["name"] == name]
    if not panes:
        active_names = sorted(set(a["name"] for a in agents))
        raise ValueError(
            f"No agent named '{name}' found. "
            f"Active agents: {', '.join(active_names) if active_names else '(none)'}. "
            f"Consider listing all active agents to see if it should go to someone else."
        )
    return panes


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

def _append_event(event: dict) -> None:
    """Append a JSON event to the shared event bus."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_EVENTS_FILE, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# Channel notification (raw _write_stream bypass)
# ---------------------------------------------------------------------------

async def _send_channel_notification(event: dict) -> None:
    """Push a notifications/claude/channel message over the MCP session."""
    if _session is None:
        return

    sender = event.get("from_name", "unknown")
    message = event.get("message", "")

    meta: dict[str, str] = {
        "sender": sender,
        "sender_pane": event.get("from_pane", ""),
        "ts": event.get("ts", ""),
    }

    try:
        notification = types.JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={
                "content": f"{sender}: {message}",
                "meta": meta,
            },
        )
        raw_msg = SessionMessage(message=types.JSONRPCMessage(notification))
        await _session._write_stream.send(raw_msg)
    except Exception:
        logger.warning("Failed to send channel notification", exc_info=True)


# ---------------------------------------------------------------------------
# Event bus tailing
# ---------------------------------------------------------------------------

async def _watch_event_bus() -> None:
    """Tail the event bus and deliver notifications for messages addressed to us."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Start from end of file (no replay)
    try:
        pos = _EVENTS_FILE.stat().st_size
    except OSError:
        pos = 0

    while True:
        await anyio.sleep(0.5)

        try:
            size = _EVENTS_FILE.stat().st_size
        except OSError:
            continue

        if size <= pos:
            if size < pos:
                pos = 0  # file was truncated
            continue

        try:
            with open(_EVENTS_FILE) as f:
                f.seek(pos)
                new_data = f.read()
                pos = f.tell()
        except OSError:
            continue

        my_name = _my_name()

        for line in new_data.strip().split("\n"):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Skip messages we sent ourselves
            if event.get("from_pane") == _agent_key:
                continue

            # Check if this message is addressed to us (by current window name)
            to = event.get("to", "")
            if isinstance(to, list):
                if my_name not in to:
                    continue
            elif to != my_name:
                continue

            logger.info(
                "Event bus -> notify: from=%s message=%s",
                event.get("from_name", "?"),
                event.get("message", "")[:80],
            )
            await _send_channel_notification(event)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
server = Server("intercom")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="send",
            description="Send a message to another agent (identified by tmux window name)",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Recipient agent name(s) — tmux window name(s)",
                    },
                    "message": {
                        "type": "string",
                        "description": "Plain text message to send",
                    },
                },
                "required": ["to", "message"],
            },
        ),
        types.Tool(
            name="who",
            description="List all active agent instances (tmux window names)",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    args = arguments or {}
    if name == "send":
        return await _handle_send(args)
    elif name == "who":
        return await _handle_who(args)
    else:
        raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _handle_send(args: dict[str, Any]) -> list[types.TextContent]:
    to = args.get("to", "")
    message = args.get("message", "")

    if not to:
        return [types.TextContent(type="text", text="Error: 'to' is required")]
    if not message:
        return [types.TextContent(type="text", text="Error: 'message' is required")]

    # Normalize to list
    recipients = to if isinstance(to, list) else [to]
    from_name = _my_name()
    ts = f"{time.time():.3f}"

    # Resolve all names first (fail fast if any unknown)
    all_panes: list[tuple[str, str]] = []  # (name, pane)
    for recipient in recipients:
        try:
            panes = _resolve_name(recipient)
            for p in panes:
                all_panes.append((recipient, p))
        except ValueError as e:
            return [types.TextContent(type="text", text=str(e))]

    # Write events to bus
    for recipient_name, _ in all_panes:
        event = {
            "to": recipient_name,
            "from_pane": _agent_key,
            "from_name": from_name,
            "message": message,
            "ts": ts,
        }
        _append_event(event)

    delivered_to = sorted(set(name for name, _ in all_panes))
    return [types.TextContent(
        type="text",
        text=f"Message sent to: {', '.join(delivered_to)}",
    )]


async def _handle_who(args: dict[str, Any]) -> list[types.TextContent]:
    agents = _who()
    if not agents:
        return [types.TextContent(type="text", text="No active agents found.")]

    lines = []
    for a in agents:
        marker = " (you)" if a["pane"] == _agent_key else ""
        lines.append(f"  {a['name']} [{a['pane']}]{marker}")

    return [types.TextContent(
        type="text",
        text=f"Active agents ({len(agents)}):\n" + "\n".join(lines),
    )]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def _main() -> None:
    global _session, _agent_key, _static_name

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    # Resolve identity (tmux pane or AGENT_NAME)
    _agent_key, _static_name = _resolve_identity()
    logger.info("Starting intercom as %s (key: %s)", _my_name(), _agent_key)

    # Acquire liveness lock
    _acquire_lock()
    logger.info("Acquired agent lock for %s", _key_to_filename(_agent_key))

    async with stdio_server() as (read_stream, write_stream):
        init_options = InitializationOptions(
            server_name="intercom",
            server_version="0.1.0",
            capabilities=server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={"claude/channel": {}},
            ),
            instructions=(
                "Intercom agent-to-agent channel plugin. Use the `who` tool to "
                "see active agents and `send` to message them by name. "
                "Incoming messages appear as channel notifications."
            ),
        )

        async with AsyncExitStack() as stack:
            lifespan_ctx = await stack.enter_async_context(server.lifespan(server))
            session = await stack.enter_async_context(
                ServerSession(read_stream, write_stream, init_options)
            )
            _session = session

            async with anyio.create_task_group() as tg:
                tg.start_soon(_watch_event_bus)

                async for message in session.incoming_messages:
                    tg.start_soon(
                        server._handle_message,
                        message, session, lifespan_ctx, False,
                    )


def run() -> None:
    """CLI entrypoint."""
    anyio.run(_main)
