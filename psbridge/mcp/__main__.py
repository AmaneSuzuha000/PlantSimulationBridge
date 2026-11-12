"""Entry point so a client can launch the server with python -m psbridge.mcp."""

from psbridge.mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
