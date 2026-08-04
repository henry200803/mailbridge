"""Minimal zero-dependency MCP server over stdio (JSON-RPC 2.0).

Implements just enough of the Model Context Protocol for a tools-only server:
  - initialize / notifications/initialized
  - tools/list
  - tools/call
  - ping

Deliberately has no third-party dependencies so the plugin runs anywhere a
Python 3.9+ interpreter exists, with no pip install step.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable, Dict, List, Optional

# Protocol versions we understand. We echo back the client's version when we
# know it, otherwise we fall back to our preferred one.
PREFERRED_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-03-26", "2024-11-05"}

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """Raised by a tool handler to report a clean, actionable failure.

    The message is returned to the model as tool output with isError=true,
    rather than as a protocol-level error, so the model can read it and retry.
    """


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[..., Any],
        annotations: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler
        self.annotations = annotations or {}

    def spec(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.annotations:
            out["annotations"] = self.annotations
        return out


class MCPServer:
    def __init__(self, name: str, version: str, instructions: str = "") -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self._tools: Dict[str, Tool] = {}
        self._negotiated_protocol = PREFERRED_PROTOCOL

    # ---------------------------------------------------------------- tools

    def tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        annotations: Optional[Dict[str, Any]] = None,
    ):
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._tools[name] = Tool(name, description, input_schema, fn, annotations)
            return fn

        return decorator

    # ------------------------------------------------------------- plumbing

    @staticmethod
    def _log(msg: str) -> None:
        # stdout is reserved for JSON-RPC framing; diagnostics go to stderr.
        sys.stderr.write(f"[mailbridge] {msg}\n")
        sys.stderr.flush()

    @staticmethod
    def _write(payload: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _result(self, req_id: Any, result: Dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: Any, code: int, message: str, data: Any = None) -> None:
        err: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._write({"jsonrpc": "2.0", "id": req_id, "error": err})

    # -------------------------------------------------------------- handlers

    def _handle_initialize(self, req_id: Any, params: Dict[str, Any]) -> None:
        requested = params.get("protocolVersion")
        if isinstance(requested, str) and requested in SUPPORTED_PROTOCOLS:
            self._negotiated_protocol = requested
        else:
            self._negotiated_protocol = PREFERRED_PROTOCOL
        result: Dict[str, Any] = {
            "protocolVersion": self._negotiated_protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        self._result(req_id, result)

    def _handle_tools_list(self, req_id: Any) -> None:
        self._result(req_id, {"tools": [t.spec() for t in self._tools.values()]})

    def _handle_tools_call(self, req_id: Any, params: Dict[str, Any]) -> None:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            self._error(req_id, INVALID_PARAMS, "arguments must be an object")
            return
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "(none)"
            self._error(
                req_id,
                METHOD_NOT_FOUND,
                f"Unknown tool '{name}'. Available tools: {known}",
            )
            return

        try:
            out = tool.handler(**args)
            self._result(req_id, {"content": _as_content(out), "isError": False})
        except ToolError as exc:
            self._result(req_id, {"content": _as_content(str(exc)), "isError": True})
        except TypeError as exc:
            # Almost always a bad/missing argument from the model.
            self._result(
                req_id,
                {
                    "content": _as_content(
                        f"Invalid arguments for {name}: {exc}. "
                        f"Check the tool's inputSchema and try again."
                    ),
                    "isError": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface everything to the model
            self._log("unhandled error in %s:\n%s" % (name, traceback.format_exc()))
            self._result(
                req_id,
                {
                    "content": _as_content(f"{type(exc).__name__}: {exc}"),
                    "isError": True,
                },
            )

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        # The wire format is UTF-8 JSON regardless of the launcher's locale.
        # Windows defaults piped stdio to the ANSI code page (GBK on Chinese
        # systems), which both mis-decodes incoming requests and crashes the
        # host's reader on our non-ASCII output when PYTHONIOENCODING is unset.
        for stream in (sys.stdin, sys.stdout):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                self._error(None, PARSE_ERROR, f"Invalid JSON: {exc}")
                continue

            if isinstance(msg, list):
                self._error(None, INVALID_REQUEST, "Batch requests are not supported")
                continue
            if not isinstance(msg, dict):
                self._error(None, INVALID_REQUEST, "Request must be a JSON object")
                continue

            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                params = {}

            # Notifications carry no id and expect no response.
            is_notification = "id" not in msg

            if method == "initialize":
                self._handle_initialize(req_id, params)
            elif method == "notifications/initialized":
                pass
            elif method == "ping":
                if not is_notification:
                    self._result(req_id, {})
            elif method == "tools/list":
                self._handle_tools_list(req_id)
            elif method == "tools/call":
                self._handle_tools_call(req_id, params)
            elif method in ("notifications/cancelled", "notifications/progress"):
                pass
            elif is_notification:
                pass  # unknown notification: ignore per JSON-RPC
            else:
                self._error(req_id, METHOD_NOT_FOUND, f"Unknown method '{method}'")


def _as_content(value: Any) -> List[Dict[str, Any]]:
    """Normalise a handler return value into MCP content blocks."""
    if isinstance(value, str):
        text = value
    elif value is None:
        text = "OK"
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return [{"type": "text", "text": text}]
