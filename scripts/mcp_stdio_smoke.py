"""Launch the MCP server over stdio and exercise JSON-RPC initialize, list, and call."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def talk(process: subprocess.Popen[str], message: dict, *, expect_reply: bool = True):
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    if not expect_reply:
        return None
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("server closed stdout while waiting for a reply")
    return json.loads(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test psbridge MCP stdio server")
    parser.add_argument("--out", default=None, help="optional path to write JSON report")
    args = parser.parse_args(argv)

    process = subprocess.Popen(
        [sys.executable, "-m", "psbridge.mcp"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    checks: dict[str, bool] = {}
    names: list[str] = []
    try:
        init = talk(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0"},
                },
            },
        )
        checks["initialize_has_protocol"] = "protocolVersion" in init["result"]
        checks["server_name"] = init["result"]["serverInfo"]["name"] == "plant-sim-bridge"

        talk(process, {"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)

        listing = talk(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in listing["result"]["tools"]]
        checks["tools_listed"] = len(names) >= 9
        checks["has_bridge_info"] = "bridge_info" in names

        called = talk(
            process,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "bridge_info", "arguments": {}}},
        )
        payload = json.loads(called["result"]["content"][0]["text"])
        checks["tool_ok"] = payload.get("ok") is True
        checks["verified"] = payload.get("plant_simulation_verified") is True
        checks["not_error"] = called["result"]["isError"] is False

        failed = talk(
            process,
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        )
        checks["unknown_tool_is_error"] = failed["result"]["isError"] is True
    finally:
        assert process.stdin is not None
        assert process.stderr is not None
        process.stdin.close()
        process.wait(timeout=20)
        stderr = process.stderr.read()

    checks["clean_exit"] = process.returncode == 0
    report = {"ok": all(checks.values()), "checks": checks, "tool_count": len(names), "stderr": stderr[-400:]}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
