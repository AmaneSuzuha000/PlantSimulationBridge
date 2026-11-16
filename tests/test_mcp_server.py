"""The MCP transport must speak correct JSON-RPC without needing the product.

Every test here drives handle_message directly, so the transport is verified
without a Plant Simulation installation and without a subprocess. The one
subprocess-shaped behaviour -- newline framing over stdin -- is covered by
feeding serve() an in-memory stream.
"""
from __future__ import annotations

import io
import json

from psbridge.mcp.server import (
    PROTOCOL_VERSION,
    SERVER_NAME,
    handle_message,
    serve,
    tool_definitions,
)
from psbridge.tools import TOOLS, tool_names


def call(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def test_initialize_reports_a_version_and_server_name():
    response = handle_message(call("initialize", {"protocolVersion": PROTOCOL_VERSION}))
    assert response["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert response["result"]["serverInfo"]["name"] == SERVER_NAME
    assert response["result"]["capabilities"]["tools"]["listChanged"] is False


def test_ping_answers_with_an_empty_result():
    assert handle_message(call("ping"))["result"] == {}


def test_tools_list_carries_every_registered_tool():
    response = handle_message(call("tools/list"))
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert names == list(tool_names())
    assert len(names) == len(TOOLS)


def test_every_tool_definition_has_the_fields_mcp_requires():
    for definition in tool_definitions():
        assert set(definition) == {"name", "description", "inputSchema"}
        assert isinstance(definition["description"], str) and definition["description"]
        schema = definition["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        json.dumps(schema)


def test_tools_call_wraps_a_successful_result_as_text_content():
    response = handle_message(call("tools/call", {"name": "bridge_info", "arguments": {}}))
    result = response["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True


def test_tools_call_marks_a_failed_tool_as_an_error_but_keeps_the_payload():
    response = handle_message(call("tools/call", {"name": "no_such_tool", "arguments": {}}))
    result = response["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNKNOWN_TOOL"


def test_tools_call_rejects_arguments_the_tool_does_not_declare():
    response = handle_message(
        call("tools/call", {"name": "bridge_info", "arguments": {"nonsense": 1}})
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["ok"] is False
    assert response["result"]["isError"] is True


def test_tools_call_without_a_name_is_an_invalid_params_error():
    response = handle_message(call("tools/call", {"arguments": {}}))
    assert response["error"]["code"] == -32602


def test_unknown_method_is_reported_as_method_not_found():
    response = handle_message(call("does/not/exist"))
    assert response["error"]["code"] == -32601


def test_a_bad_jsonrpc_version_is_rejected():
    response = handle_message({"jsonrpc": "1.0", "id": 7, "method": "ping"})
    assert response["error"]["code"] == -32600


def test_a_notification_gets_no_response():
    """MCP sends notifications/initialized; answering it would be a protocol error."""
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled",
                           "params": {"requestId": 1}}) is None


def test_a_response_from_the_peer_is_ignored():
    assert handle_message({"jsonrpc": "2.0", "id": 3, "result": {}}) is None


def test_serve_frames_messages_and_survives_a_parse_error():
    stdin = io.StringIO("\n".join([
        json.dumps(call("initialize", {}, 1)),
        "   ",
        "{not json",
        json.dumps(call("ping", None, 2)),
        "",
    ]))
    stdout = io.StringIO()
    assert serve(stdin, stdout, io.StringIO()) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["id"] == 1 and "result" in lines[0]
    assert lines[1]["error"]["code"] == -32700
    assert lines[2] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_serve_answers_nothing_for_a_notification_line():
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    stdout = io.StringIO()
    serve(stdin, stdout, io.StringIO())
    assert stdout.getvalue() == ""


def test_non_ascii_payload_survives_the_wire():
    response = handle_message(call("tools/call", {"name": "bridge_info", "arguments": {}}))
    text = response["result"]["content"][0]["text"]
    assert json.loads(text)["ok"] is True
    assert "\\u" not in text


def test_non_finite_tool_payload_never_leaks_invalid_json(monkeypatch):
    import psbridge.mcp.server as srv

    monkeypatch.setattr(srv, "call_tool", lambda _name, _args: {"ok": True, "value": float("nan")})
    response = handle_message(call("tools/call", {"name": "bridge_info", "arguments": {}}))
    assert response["result"]["isError"] is True
    text = response["result"]["content"][0]["text"]
    assert "NaN" not in text
    parsed = json.loads(text)
    assert parsed["ok"] is False
    assert parsed["error"]["code"] == "BACKEND_ERROR"
