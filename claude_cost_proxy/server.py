"""HTTP proxy that forwards Claude Code's API calls to Anthropic with logging and caching.

Handles both plain JSON responses and server-sent-event (``"stream": true``)
responses, which is what Claude Code sends by default.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import httpx
from aiohttp import web

from . import config
from .pricing import cost_usd

CACHE_TTL_SECONDS = 24 * 60 * 60
FORWARDED_HEADERS = ("anthropic-version", "anthropic-beta")
_PATH_PATTERN = re.compile(r'(?:^|[\s\'"(])((?:[./~])?[\w\-./]+\.\w+)(?:$|[\s\'"):,])')


def parse_sse_usage(raw: bytes) -> Tuple[Dict[str, int], bool]:
    """Pull token usage and tool-use presence out of an Anthropic SSE stream.

    ``message_start`` carries the input-side usage; ``message_delta`` events
    carry the (cumulative) output-side usage.
    """
    usage: Dict[str, int] = {}
    has_tool_use = False
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "message_start":
            for k, v in (event.get("message", {}).get("usage") or {}).items():
                if isinstance(v, int):
                    usage[k] = v
        elif etype == "message_delta":
            for k, v in (event.get("usage") or {}).items():
                if isinstance(v, int):
                    usage[k] = v
        elif etype == "content_block_start":
            if (event.get("content_block") or {}).get("type") == "tool_use":
                has_tool_use = True
    return usage, has_tool_use


def _usage_fields(usage: Dict[str, Any]) -> Dict[str, int]:
    return {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
    }


class ThrottleProxy:
    def __init__(
        self,
        port: int = 8080,
        log_dir: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        api_key: Optional[str] = None,
        upstream: Optional[str] = None,
        host: str = "127.0.0.1",
    ):
        self.port = port
        self.host = host
        self.log_dir = log_dir or config.log_dir()
        self.cache_dir = cache_dir or config.cache_dir()
        self.api_key = api_key or config.load_api_key()
        self.upstream = (upstream or config.upstream_url()).rstrip("/")

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---- logging -------------------------------------------------------
    def _get_log_file(self) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.log_dir / f"throttle-{today}.jsonl"

    def _log_request(self, log_entry: Dict[str, Any]) -> None:
        with open(self._get_log_file(), "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    def _log(self, model: str, cache_hit: bool, start: float, usage: Dict[str, Any],
             stream: bool) -> None:
        usage = _usage_fields(usage)
        cost = cost_usd(model, usage)
        self._log_request({
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "cache_hit": cache_hit,
            "stream": stream,
            "latency_ms": (time.time() - start) * 1000,
            "usage": usage,
            "cost_usd": 0.0 if cache_hit else cost,
            "saved_usd": cost if cache_hit else 0.0,
        })

    # Kept for backwards compatibility with earlier versions.
    def _calculate_cost(self, model: str, usage: Dict[str, int]) -> float:
        return cost_usd(model, usage)

    # ---- caching -------------------------------------------------------
    def _hash_request(self, request_body: Dict[str, Any]) -> str:
        canonical = json.dumps(request_body, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _has_tool_use(self, response_body: Dict[str, Any]) -> bool:
        content = response_body.get("content", [])
        if isinstance(content, list):
            return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
        return False

    def _extract_file_paths(self, request_body: Dict[str, Any]) -> list:
        """Heuristic: file-like paths mentioned in message text, for cache invalidation."""
        paths = []
        for message in request_body.get("messages", []):
            content = message.get("content", "")
            texts: Iterable[str]
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
            else:
                texts = []
            for text in texts:
                paths.extend(_PATH_PATTERN.findall(text))
        return paths

    def _check_files_unchanged(self, paths: list, cached_time: float) -> bool:
        for path_str in paths:
            try:
                path = Path(path_str).expanduser()
                if not path.is_absolute():
                    path = (Path.cwd() / path).resolve()
                if path.exists() and path.is_file() and path.stat().st_mtime > cached_time:
                    return False
            except (OSError, ValueError):
                return False
        return True

    def _get_cached_response(self, request_hash: str,
                             request_body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cache_file = self.cache_dir / f"{request_hash}.json"
        if not cache_file.exists():
            return None
        try:
            with open(cache_file, "r") as f:
                entry = json.load(f)
            cached_time = entry["timestamp"]
            if time.time() - cached_time > CACHE_TTL_SECONDS:
                return None
            paths = self._extract_file_paths(request_body)
            if paths and not self._check_files_unchanged(paths, cached_time):
                return None
            return entry
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def _save_to_cache(self, request_hash: str, request_body: Dict[str, Any],
                       response_body: Optional[Dict[str, Any]] = None,
                       sse: Optional[str] = None, usage: Optional[Dict] = None) -> None:
        entry: Dict[str, Any] = {"timestamp": time.time(), "request": request_body}
        if sse is not None:
            entry["sse"] = sse
            entry["usage"] = usage or {}
        else:
            entry["response"] = response_body
        with open(self.cache_dir / f"{request_hash}.json", "w") as f:
            json.dump(entry, f)

    # ---- forwarding ----------------------------------------------------
    def _upstream_headers(self, request: web.Request) -> Dict[str, str]:
        headers = {"content-type": "application/json"}
        for name in FORWARDED_HEADERS:
            if name in request.headers:
                headers[name] = request.headers[name]
        headers.setdefault("anthropic-version", "2023-06-01")
        if self.api_key:
            headers["x-api-key"] = self.api_key
        elif "x-api-key" in request.headers:
            headers["x-api-key"] = request.headers["x-api-key"]
        elif "authorization" in request.headers:
            headers["authorization"] = request.headers["authorization"]
        return headers

    def _upstream_url(self, request: web.Request) -> str:
        return f"{self.upstream}{request.rel_url}"

    async def handle_request(self, request: web.Request) -> web.StreamResponse:
        start = time.time()
        try:
            request_body = await request.json()
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        model = request_body.get("model", "unknown")
        stream = bool(request_body.get("stream"))
        request_hash = self._hash_request(request_body)

        cached = self._get_cached_response(request_hash, request_body)
        if cached is not None:
            if "sse" in cached:
                self._log(model, True, start, cached.get("usage", {}), stream=True)
                resp = web.StreamResponse(headers={"content-type": "text/event-stream",
                                                   "x-claude-proxy-cache": "hit"})
                await resp.prepare(request)
                await resp.write(cached["sse"].encode())
                await resp.write_eof()
                return resp
            if "response" in cached:
                body = cached["response"]
                self._log(model, True, start, body.get("usage", {}), stream=False)
                return web.json_response(body, headers={"x-claude-proxy-cache": "hit"})

        headers = self._upstream_headers(request)
        url = self._upstream_url(request)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
                if stream:
                    return await self._forward_stream(request, client, url, headers,
                                                      request_body, request_hash, model, start)
                response = await client.post(url, json=request_body, headers=headers)
        except httpx.HTTPError as e:
            return web.Response(status=502, text=f"Proxy error: {e}")

        try:
            response_body = response.json()
        except (json.JSONDecodeError, ValueError):
            response_body = None

        if response.status_code == 200 and isinstance(response_body, dict):
            self._log(model, False, start, response_body.get("usage", {}), stream=False)
            if not self._has_tool_use(response_body):
                self._save_to_cache(request_hash, request_body, response_body=response_body)

        return web.Response(
            status=response.status_code,
            body=response.content,
            headers={"content-type": response.headers.get("content-type", "application/json"),
                     "x-claude-proxy-cache": "miss"},
        )

    async def _forward_stream(self, request, client, url, headers, request_body,
                              request_hash, model, start) -> web.StreamResponse:
        async with client.stream("POST", url, json=request_body, headers=headers) as upstream:
            resp = web.StreamResponse(
                status=upstream.status_code,
                headers={"content-type": upstream.headers.get("content-type", "text/event-stream"),
                         "x-claude-proxy-cache": "miss"},
            )
            await resp.prepare(request)
            chunks = []
            try:
                async for chunk in upstream.aiter_raw():
                    chunks.append(chunk)
                    await resp.write(chunk)
            except httpx.HTTPError:
                # Upstream dropped mid-stream: end the response, don't log or cache.
                await resp.write_eof()
                return resp
            await resp.write_eof()

        if upstream.status_code == 200:
            raw = b"".join(chunks)
            usage, has_tool_use = parse_sse_usage(raw)
            self._log(model, False, start, usage, stream=True)
            if not has_tool_use and b"message_stop" in raw:
                self._save_to_cache(request_hash, request_body,
                                    sse=raw.decode("utf-8", errors="replace"), usage=usage)
        return resp

    async def passthrough(self, request: web.Request) -> web.Response:
        """Forward other POST endpoints (e.g. count_tokens) without logging or caching."""
        body = await request.read()
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(self._upstream_url(request), content=body,
                                      headers=self._upstream_headers(request))
        except httpx.HTTPError as e:
            return web.Response(status=502, text=f"Proxy error: {e}")
        return web.Response(status=r.status_code, body=r.content,
                            headers={"content-type": r.headers.get("content-type",
                                                                   "application/json")})

    async def health_check(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "proxy": "claude-cost-proxy",
                                  "upstream": self.upstream})

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/v1/messages", self.handle_request)
        app.router.add_post("/v1/messages/count_tokens", self.passthrough)
        app.router.add_get("/health", self.health_check)
        return app

    def run(self) -> None:
        app = self.create_app()
        print(f"🚀 Claude cost proxy starting on http://{self.host}:{self.port}")
        print(f"📁 Logs: {self.log_dir}")
        print(f"💾 Cache: {self.cache_dir}")
        print(f"🔑 API key: {'configured' if self.api_key else 'not set (client key is forwarded)'}")
        print(f"\nForwarding requests to {self.upstream}")
        print("Press Ctrl+C to stop\n")
        web.run_app(app, host=self.host, port=self.port, print=None)


# Friendlier alias; ThrottleProxy is kept for existing imports.
CostProxy = ThrottleProxy


def main() -> None:
    import sys

    port = int(os.getenv("THROTTLE_PORT", "8080"))
    if len(sys.argv) > 1:
        if sys.argv[1] in ("-h", "--help"):
            print("Usage: claude-proxy [PORT]")
            print()
            print("Env: ANTHROPIC_API_KEY, CLAUDE_PROXY_HOME, CLAUDE_PROXY_UPSTREAM, THROTTLE_PORT")
            sys.exit(0)
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"Invalid port: {sys.argv[1]}", file=sys.stderr)
            sys.exit(1)

    proxy = ThrottleProxy(port=port)
    if not proxy.api_key:
        print("ERROR: no Anthropic API key found (env ANTHROPIC_API_KEY or "
              f"{config.config_file()})", file=sys.stderr)
        print("Run 'claude-proxy-setup' first, or try 'claude-proxy-demo' (no key needed).",
              file=sys.stderr)
        sys.exit(1)

    try:
        proxy.run()
    except KeyboardInterrupt:
        print("\n\n👋 Proxy stopped")


if __name__ == "__main__":
    main()
