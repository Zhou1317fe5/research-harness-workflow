#!/usr/bin/env python3
"""供工作流自动化调用 Hindsight 的 Streamable HTTP MCP 客户端。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class MCPError(RuntimeError):
    """只携带状态信息，避免远端异常中的请求头进入日志。"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HindsightMCP:
    def __init__(self, url: str, api_key: str, *, timeout: float = 45):
        parsed = urlsplit(url)
        local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or (parsed.scheme != "https" and not local) or (
            parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise MCPError("invalid MCP endpoint")
        if not api_key or timeout <= 0:
            raise MCPError("missing API key or invalid timeout")
        self.url = url.rstrip("/") + "/"
        self.timeout = timeout
        self.headers = {
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.opener = urllib.request.build_opener(NoRedirect)
        self.sequence = 0
        self.initialized = False

    @classmethod
    def from_env(cls, *, timeout: float = 45):
        url = os.environ.get("HINDSIGHT_MCP_URL", "").strip()
        if not url:
            base = os.environ.get("HINDSIGHT_API_URL", "").strip()
            bank = os.environ.get("HINDSIGHT_BANK_ID", "").strip()
            if not base or not bank:
                raise MCPError("set HINDSIGHT_MCP_URL or HINDSIGHT_API_URL / HINDSIGHT_BANK_ID")
            url = base.rstrip("/") + "/mcp/" + quote(bank, safe="") + "/"
        return cls(url, os.environ.get("HINDSIGHT_API_KEY", ""), timeout=timeout)

    def _rpc(self, method: str, params: dict | None = None, *, notification: bool = False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self.sequence += 1
            body["id"] = self.sequence
        request = urllib.request.Request(
            self.url, data=json.dumps(body, ensure_ascii=False).encode(),
            headers=self.headers, method="POST",
        )
        deadline = time.monotonic() + self.timeout
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.headers["Mcp-Session-Id"] = session
                if response.status == 202 and notification:
                    return {}
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    size = 0
                    result = None
                    while time.monotonic() < deadline:
                        line = response.readline(MAX_RESPONSE_BYTES + 1)
                        if not line:
                            break
                        size += len(line)
                        if size > MAX_RESPONSE_BYTES:
                            raise MCPError("MCP response exceeds byte budget")
                        if not line.startswith(b"data:"):
                            continue
                        event = json.loads(line[5:])
                        if event.get("id") == body.get("id") and (
                            "result" in event or "error" in event
                        ):
                            result = event
                            break
                    if result is None:
                        raise MCPError("MCP stream ended or timed out without a matching response")
                else:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise MCPError("MCP response exceeds byte budget")
                    result = json.loads(raw)
                if not isinstance(result, dict) or result.get("id") != body.get("id"):
                    raise MCPError("invalid JSON-RPC response identity")
                if "error" in result:
                    raise MCPError(f"MCP RPC error: {result['error'].get('code')}")
                return result.get("result", {})
        except urllib.error.HTTPError as error:
            raise MCPError(f"MCP HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise MCPError(f"MCP transport error: {type(error).__name__}") from None
        except (ValueError, UnicodeError):
            raise MCPError("invalid MCP JSON response") from None

    def initialize(self) -> dict:
        result = self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "research-harness-workflow", "version": "0.1"},
        })
        if "tools" not in result.get("capabilities", {}):
            raise MCPError("server does not advertise MCP tools")
        self.headers["MCP-Protocol-Version"] = result["protocolVersion"]
        self._rpc("notifications/initialized", notification=True)
        self.initialized = True
        return result

    def list_tools(self) -> list[dict]:
        if not self.initialized:
            self.initialize()
        result = self._rpc("tools/list")
        tools = list(result.get("tools", []))
        while result.get("nextCursor"):
            result = self._rpc("tools/list", {"cursor": result["nextCursor"]})
            tools.extend(result.get("tools", []))
        return tools

    def call(self, name: str, arguments: dict | None = None) -> dict:
        if not self.initialized:
            self.initialize()
        return self._rpc("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self):
        if "Mcp-Session-Id" not in self.headers:
            return
        request = urllib.request.Request(self.url, headers=self.headers, method="DELETE")
        try:
            with self.opener.open(request, timeout=min(self.timeout, 10)):
                pass
        except (urllib.error.URLError, TimeoutError):
            pass
        finally:
            self.headers.pop("Mcp-Session-Id", None)
            self.initialized = False

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *args):
        self.close()


def unpack_result(result: dict):
    if result.get("isError"):
        raise MCPError("MCP tool returned isError; inspect the isolated test evidence")
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    texts = [item["text"] for item in result.get("content", []) if item.get("type") == "text"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            return {"text": texts[0]}
    return {"text": "\n".join(texts)}
