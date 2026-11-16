"""MCP (Model Context Protocol) transport for the bridge tool layer.

psbridge.tools holds the handlers: structured JSON in, structured JSON out,
with strict argument validation. This module is the transport an MCP client
speaks to -- it adds the JSON-RPC envelope and nothing else. Keeping the two
apart is deliberate: the handlers are testable without a subprocess, and the
transport is testable without a Plant Simulation installation.

Wire format is the MCP stdio transport: one JSON-RPC 2.0 message per line over
stdin/stdout. Anything the server writes to stdout is protocol traffic, so
diagnostics go to stderr.

Implemented methods:

    initialize                 -> protocol version, capabilities, server info
    notifications/initialized  -> no response (notification)
    ping                       -> empty result
    tools/list                 -> the tool catalog with input schemas
    tools/call                 -> runs one tool, returns its JSON as text

A tool that reports {"ok": false} is a tool execution error, so it comes back
with isError true and the payload still in the content block -- the caller can
read the error code instead of parsing a protocol-level fault.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from psbridge import __version__
from psbridge.tools import TOOLS, call_tool, close_session

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "plant-sim-bridge"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def tool_definitions() -> list[dict[str, Any]]:
    """The catalog in MCP shape: inputSchema, camelCase, no handler."""
    return [
        {
            "name": spec["name"],
            "description": spec["description"],
            "inputSchema": spec["input_schema"],
        }
        for spec in TOOLS
    ]


def _result(request_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(payload)}


def _error(request_id: Any, code: int, message: str,
           data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _call_tool(params: Mapping[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("tools/call requires a string 'name'")
    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, Mapping):
        raise ValueError("tools/call 'arguments' must be an object when present")
    payload = call_tool(name, arguments)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        payload = {
            "ok": False,
            "error": {
                "code": "BACKEND_ERROR",
                "message": "tool output is not strict JSON",
                "details": {"tool": name, "error": str(exc)},
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    return {
        "content": [{"type": "text", "text": encoded}],
        "isError": not payload.get("ok", True),
    }


def _valid_request_id(value: Any) -> bool:
    return value is None or (not isinstance(value, bool) and isinstance(value, (str, int)))


def handle_message(message: Any) -> dict[str, Any] | None:
    """Handle one decoded JSON-RPC message.

    Returns the response object, or None when the message is a notification
    (no id) and therefore must not be answered.
    """
    if not isinstance(message, Mapping):
        return _error(None, INVALID_REQUEST, "message must be a JSON object")

    has_id = "id" in message
    raw_id = message.get("id")
    if has_id and not _valid_request_id(raw_id):
        return _error(None, INVALID_REQUEST, "id must be a string, integer, or null")
    request_id = raw_id if has_id else None

    if message.get("jsonrpc") != "2.0":
        return _error(request_id, INVALID_REQUEST, 'jsonrpc must be "2.0"')

    method = message.get("method")
    if method is None:
        # A response from the peer. This server never issues requests, so there
        # is nothing to correlate and nothing to answer.
        return None
    if not isinstance(method, str):
        return _error(request_id, INVALID_REQUEST, "method must be a string")

    is_notification = not has_id
    if "params" not in message or message["params"] is None:
        params: Mapping[str, Any] = {}
    else:
        raw_params = message["params"]
        if not isinstance(raw_params, Mapping):
            return None if is_notification else _error(
                request_id, INVALID_PARAMS, "params must be an object"
            )
        params = raw_params

    try:
        if method == "initialize":
            payload = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        elif method == "ping":
            payload = {}
        elif method == "tools/list":
            payload = {"tools": tool_definitions()}
        elif method == "tools/call":
            payload = _call_tool(params)
        elif method.startswith("notifications/"):
            return None
        else:
            return None if is_notification else _error(
                request_id, METHOD_NOT_FOUND, "unknown method", {"method": method})
    except ValueError as exc:
        return None if is_notification else _error(
            request_id, INVALID_PARAMS, str(exc))
    except Exception as exc:  # noqa: BLE001 - the transport must not die on one call
        return None if is_notification else _error(
            request_id, INTERNAL_ERROR, type(exc).__name__, {"detail": str(exc)})

    return None if is_notification else _result(request_id, payload)


def serve(stdin: TextIO, stdout: TextIO, stderr: TextIO | None = None) -> int:
    """Read newline-delimited JSON-RPC until stdin closes."""
    log = stderr if stderr is not None else sys.stderr
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                response = _error(None, PARSE_ERROR, "invalid JSON", {"detail": str(exc)})
            else:
                response = handle_message(message)
            if response is None:
                continue
            try:
                wire = json.dumps(response, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                fallback = _error(response.get("id") if isinstance(response, Mapping) else None,
                                  INTERNAL_ERROR, "non-serializable response", {"detail": str(exc)})
                wire = json.dumps(fallback, ensure_ascii=False, allow_nan=False)
            stdout.write(wire + "\n")
            stdout.flush()
    finally:
        close_session()
        log.flush()
    return 0


def main() -> int:
    return serve(sys.stdin, sys.stdout, sys.stderr)
