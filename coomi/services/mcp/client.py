"""Minimal MCP stdio client."""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from ... import __version__
from .models import McpServerConfig, McpToolSpec


class McpError(RuntimeError):
    pass


class StdioMcpClient:
    def __init__(self, server: McpServerConfig, timeout: float = 15.0):
        if server.transport != "stdio":
            raise McpError(f"Unsupported MCP transport: {server.transport}")
        if not server.command:
            raise McpError("MCP stdio server requires a command")
        self.server = server
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._next_id = 1

    def __enter__(self) -> "StdioMcpClient":
        env = os.environ.copy()
        env.update(self.server.env)
        self._proc = subprocess.Popen(
            [self.server.command, *self.server.args],
            cwd=self.server.cwd or None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._proc:
            return
        try:
            self._send_notification("notifications/cancelled", {"reason": "client closed"})
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            self._proc.kill()

    def list_tools(self) -> list[McpToolSpec]:
        result = self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        specs: list[McpToolSpec] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            specs.append(
                McpToolSpec(
                    server_name=self.server.name,
                    name=str(tool.get("name", "")),
                    description=str(tool.get("description", "")),
                    input_schema=tool.get("inputSchema") or tool.get("input_schema") or {},
                )
            )
        return [spec for spec in specs if spec.name]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def _initialize(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "coomi-agent", "version": __version__},
            },
        )
        if not isinstance(result, dict):
            raise McpError("MCP initialize returned an invalid response")
        self._send_notification("notifications/initialized", {})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            message = self._read_message(deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    raise McpError(str(error.get("message") or error))
                raise McpError(str(error))
            return message.get("result") or {}
        raise McpError(f"MCP request timed out: {method}")

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._require_proc()
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        proc.stdin.write(header + data)
        proc.stdin.flush()

    def _read_message(self, deadline: float) -> dict[str, Any]:
        proc = self._require_proc()
        header_bytes = bytearray()
        while b"\r\n\r\n" not in header_bytes:
            if time.time() > deadline:
                raise McpError("MCP response timed out while reading headers")
            chunk = proc.stdout.read(1)
            if not chunk:
                stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                raise McpError(stderr.strip() or "MCP server closed stdout")
            header_bytes.extend(chunk)

        header_text = header_bytes.decode("ascii", errors="replace")
        content_length = 0
        for line in header_text.splitlines():
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        if content_length <= 0:
            raise McpError("MCP response missing Content-Length")

        body = proc.stdout.read(content_length)
        if len(body) != content_length:
            raise McpError("MCP response body was truncated")
        try:
            message = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise McpError(f"Invalid MCP JSON response: {exc}") from exc
        if not isinstance(message, dict):
            raise McpError("Invalid MCP response type")
        return message

    def _require_proc(self) -> subprocess.Popen:
        if not self._proc or self._proc.stdin is None or self._proc.stdout is None:
            raise McpError("MCP process is not running")
        if self._proc.poll() is not None:
            stderr = self._proc.stderr.read().decode("utf-8", errors="replace") if self._proc.stderr else ""
            raise McpError(stderr.strip() or f"MCP process exited with {self._proc.returncode}")
        return self._proc


class HttpMcpClient:
    def __init__(self, server: McpServerConfig, timeout: float = 15.0):
        if server.transport != "http":
            raise McpError(f"Unsupported MCP transport: {server.transport}")
        if not server.url:
            raise McpError("MCP HTTP server requires a URL")
        self.server = server
        self.timeout = timeout
        self._next_id = 1
        self._client: httpx.Client | None = None

    def __enter__(self) -> "HttpMcpClient":
        self._client = httpx.Client(timeout=self.timeout, headers=self.server.headers)
        self._initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._client:
            self._client.close()

    def list_tools(self) -> list[McpToolSpec]:
        result = self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        specs: list[McpToolSpec] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            specs.append(
                McpToolSpec(
                    server_name=self.server.name,
                    name=str(tool.get("name", "")),
                    description=str(tool.get("description", "")),
                    input_schema=tool.get("inputSchema") or tool.get("input_schema") or {},
                )
            )
        return [spec for spec in specs if spec.name]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "coomi-agent", "version": __version__},
            },
        )

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            response = self._require_client().post(self.server.url, json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise McpError(f"HTTP MCP request failed: {exc}") from exc
        if not isinstance(data, dict):
            raise McpError("Invalid HTTP MCP response type")
        if "error" in data:
            error = data["error"]
            if isinstance(error, dict):
                raise McpError(str(error.get("message") or error))
            raise McpError(str(error))
        return data.get("result") or {}

    def _require_client(self) -> httpx.Client:
        if not self._client:
            raise McpError("HTTP MCP client is not open")
        return self._client


class SseMcpClient:
    def __init__(self, server: McpServerConfig, timeout: float = 15.0):
        if server.transport != "sse":
            raise McpError(f"Unsupported MCP transport: {server.transport}")
        if not server.url:
            raise McpError("MCP SSE server requires a URL")
        self.server = server
        self.timeout = timeout
        self._next_id = 1
        self._client: httpx.Client | None = None
        self._stream_context = None
        self._response: httpx.Response | None = None
        self._lines = None
        self._message_url = ""

    def __enter__(self) -> "SseMcpClient":
        self._client = httpx.Client(timeout=self.timeout, headers=self.server.headers)
        self._stream_context = self._client.stream("GET", self.server.url)
        self._response = self._stream_context.__enter__()
        self._response.raise_for_status()
        self._lines = self._response.iter_lines()
        self._message_url = self._read_endpoint()
        self._initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._stream_context:
            self._stream_context.__exit__(exc_type, exc, tb)
        if self._client:
            self._client.close()

    def list_tools(self) -> list[McpToolSpec]:
        result = self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        specs: list[McpToolSpec] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            specs.append(
                McpToolSpec(
                    server_name=self.server.name,
                    name=str(tool.get("name", "")),
                    description=str(tool.get("description", "")),
                    input_schema=tool.get("inputSchema") or tool.get("input_schema") or {},
                )
            )
        return [spec for spec in specs if spec.name]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "coomi-agent", "version": __version__},
            },
        )

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            response = self._require_client().post(self._message_url, json=payload)
            response.raise_for_status()
        except Exception as exc:
            raise McpError(f"SSE MCP POST failed: {exc}") from exc

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            event = self._read_sse_event()
            data = event.get("data", "")
            if not data:
                continue
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    raise McpError(str(error.get("message") or error))
                raise McpError(str(error))
            return message.get("result") or {}
        raise McpError(f"SSE MCP request timed out: {method}")

    def _read_endpoint(self) -> str:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            event = self._read_sse_event()
            if event.get("event") == "endpoint" and event.get("data"):
                return urljoin(self.server.url, event["data"])
        raise McpError("SSE MCP server did not provide an endpoint event")

    def _read_sse_event(self) -> dict[str, str]:
        if self._lines is None:
            raise McpError("SSE MCP stream is not open")
        event = "message"
        data_lines: list[str] = []
        for line in self._lines:
            if line == "":
                if data_lines:
                    return {"event": event, "data": "\n".join(data_lines)}
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        raise McpError("SSE MCP stream ended")

    def _require_client(self) -> httpx.Client:
        if not self._client:
            raise McpError("SSE MCP client is not open")
        return self._client


def open_mcp_client(server: McpServerConfig):
    if server.transport == "stdio":
        return StdioMcpClient(server)
    if server.transport == "http":
        return HttpMcpClient(server)
    if server.transport == "sse":
        return SseMcpClient(server)
    raise McpError(f"Unsupported MCP transport: {server.transport}")
