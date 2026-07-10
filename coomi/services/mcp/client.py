"""Minimal MCP stdio client."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from ... import __version__
from .models import McpServerConfig, McpToolSpec


class McpError(RuntimeError):
    pass


class StdioMcpClient:
    def __init__(self, server: McpServerConfig, timeout: float = 30.0):
        if server.transport != "stdio":
            raise McpError(f"Unsupported MCP transport: {server.transport}")
        if not server.command:
            raise McpError("MCP stdio server requires a command")
        self.server = server
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._next_id = 1
        self._framing = "jsonl"
        self._messages: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_chunks: list[bytes] = []

    def __enter__(self) -> "StdioMcpClient":
        errors: list[str] = []
        for framing in ("jsonl", "content_length"):
            self._framing = framing
            self._start_process()
            try:
                self._initialize()
                return self
            except McpError as exc:
                errors.append(f"{framing}: {exc}")
                self._shutdown()
        raise McpError("MCP initialize failed (" + "; ".join(errors) + ")")

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._send_notification("notifications/cancelled", {"reason": "client closed"})
        except Exception:
            pass
        self._shutdown()

    def _start_process(self) -> None:
        env = os.environ.copy()
        env.update(self.server.env)
        try:
            self._proc = subprocess.Popen(
                [self.server.command, *self.server.args],
                cwd=self.server.cwd or None,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise McpError(f"Unable to start MCP server: {exc}") from exc
        self._messages = queue.Queue()
        self._stderr_chunks = []
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread.start()

    def _shutdown(self) -> None:
        proc = self._proc
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        self._proc = None

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
            remaining = max(0.01, deadline - time.time())
            try:
                item = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise McpError(f"MCP request timed out: {method}") from exc
            if isinstance(item, BaseException):
                if isinstance(item, McpError):
                    raise item
                raise McpError(str(item)) from item
            message = item
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
        if self._framing == "content_length":
            header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
            proc.stdin.write(header + data)
        else:
            proc.stdin.write(data + b"\n")
        proc.stdin.flush()

    def _reader_loop(self) -> None:
        proc = self._proc
        if not proc or proc.stdout is None:
            return
        while self._proc is proc:
            try:
                message = self._read_stream_message(proc.stdout)
            except BaseException as exc:
                self._messages.put(exc)
                return
            self._messages.put(message)

    def _stderr_loop(self) -> None:
        proc = self._proc
        if not proc or proc.stderr is None:
            return
        while self._proc is proc:
            chunk = proc.stderr.read(1024)
            if not chunk:
                return
            self._stderr_chunks.append(chunk)
            if len(self._stderr_chunks) > 32:
                del self._stderr_chunks[:-32]

    def _read_stream_message(self, stream) -> dict[str, Any]:
        first = b""
        while not first or first in b" \t\r\n":
            first = stream.read(1)
            if not first:
                raise McpError(self._stderr_text() or "MCP server closed stdout")

        if first in {b"{", b"["}:
            body = first + stream.readline()
        else:
            header_bytes = bytearray(first)
            while b"\r\n\r\n" not in header_bytes and b"\n\n" not in header_bytes:
                chunk = stream.read(1)
                if not chunk:
                    raise McpError(self._stderr_text() or "MCP server closed stdout")
                header_bytes.extend(chunk)
            header_text = header_bytes.decode("ascii", errors="replace")
            content_length = 0
            for line in header_text.splitlines():
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
            if content_length <= 0:
                raise McpError("MCP response missing Content-Length")
            body = stream.read(content_length)
            if len(body) != content_length:
                raise McpError("MCP response body was truncated")
        try:
            message = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise McpError(f"Invalid MCP JSON response: {exc}") from exc
        if not isinstance(message, dict):
            raise McpError("Invalid MCP response type")
        return message

    def _stderr_text(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", errors="replace").strip()

    def _require_proc(self) -> subprocess.Popen:
        if not self._proc or self._proc.stdin is None or self._proc.stdout is None:
            raise McpError("MCP process is not running")
        if self._proc.poll() is not None:
            raise McpError(
                self._stderr_text() or f"MCP process exited with {self._proc.returncode}"
            )
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
