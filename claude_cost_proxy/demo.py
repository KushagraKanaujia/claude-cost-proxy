"""Zero-key demo: run the real proxy against a local mock of the Anthropic API.

``claude-proxy-demo`` starts a fake upstream that returns canned Messages API
responses (JSON and SSE), points the proxy at it, sends a short scripted
session, then prints the same summary ``claude-proxy-summary`` would.
No API key and no network access are needed; nothing is written to ~/.throttle.
"""
import argparse
import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import httpx
from aiohttp import web

from .server import ThrottleProxy
from .summary import parse_log_file, print_summary

DEMO_MODEL = "claude-sonnet-4-5"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_usage(body: Dict[str, Any]) -> Dict[str, int]:
    text = json.dumps(body.get("messages", [])) + json.dumps(body.get("system", ""))
    return {"input_tokens": 1200 + len(text) * 3, "output_tokens": 640,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 9000}


def _wants_tool(body: Dict[str, Any]) -> bool:
    return bool(body.get("tools"))


def _message(body: Dict[str, Any]) -> Dict[str, Any]:
    if _wants_tool(body):
        content = [{"type": "tool_use", "id": "toolu_demo", "name": "Edit",
                    "input": {"file_path": "app.py"}}]
    else:
        content = [{"type": "text", "text": "(mock) Here is an explanation of the code."}]
    return {"id": "msg_demo", "type": "message", "role": "assistant",
            "model": body.get("model"), "content": content,
            "stop_reason": "tool_use" if _wants_tool(body) else "end_turn",
            "usage": _fake_usage(body)}


def _sse(body: Dict[str, Any]) -> str:
    msg = _message(body)
    usage = msg["usage"]
    start_usage = dict(usage, output_tokens=1)
    events = [("message_start", {"type": "message_start",
                                 "message": dict(msg, content=[], usage=start_usage)})]
    block = msg["content"][0]
    if block["type"] == "text":
        events += [("content_block_start", {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": block["text"]}})]
    else:
        events += [("content_block_start", {"type": "content_block_start", "index": 0,
                                            "content_block": dict(block, input={})})]
    events += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
               ("message_delta", {"type": "message_delta",
                                  "delta": {"stop_reason": msg["stop_reason"]},
                                  "usage": {"output_tokens": usage["output_tokens"]}}),
               ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


class MockUpstream:
    """Tiny stand-in for api.anthropic.com that counts the calls it receives."""

    def __init__(self) -> None:
        self.calls = 0

    async def messages(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.calls += 1
        if body.get("stream"):
            return web.Response(text=_sse(body), content_type="text/event-stream")
        return web.json_response(_message(body))

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/messages", self.messages)
        return app


SCRIPT: List[Dict[str, Any]] = [
    {"label": "Ask a question", "stream": False, "tools": False,
     "prompt": "Explain what the retry loop in the client does."},
    {"label": "Ask the same question again", "stream": False, "tools": False,
     "prompt": "Explain what the retry loop in the client does."},
    {"label": "Streamed question (how Claude Code calls)", "stream": True, "tools": False,
     "prompt": "Summarize the architecture of this repo."},
    {"label": "Same streamed question again", "stream": True, "tools": False,
     "prompt": "Summarize the architecture of this repo."},
    {"label": "Request that triggers a file edit (tool use)", "stream": True, "tools": True,
     "prompt": "Rename the function in app.py."},
    {"label": "Same edit request again", "stream": True, "tools": True,
     "prompt": "Rename the function in app.py."},
]


async def run_demo(home: Path, quiet: bool = False) -> Dict[str, Any]:
    mock = MockUpstream()
    up_runner = web.AppRunner(mock.app())
    await up_runner.setup()
    up_port = _free_port()
    await web.TCPSite(up_runner, "127.0.0.1", up_port).start()

    proxy = ThrottleProxy(port=_free_port(), log_dir=home / "logs", cache_dir=home / "cache",
                          api_key="demo-key-not-real", upstream=f"http://127.0.0.1:{up_port}")
    px_runner = web.AppRunner(proxy.create_app())
    await px_runner.setup()
    await web.TCPSite(px_runner, "127.0.0.1", proxy.port).start()

    results = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{proxy.port}") as client:
            for step in SCRIPT:
                body: Dict[str, Any] = {"model": DEMO_MODEL, "max_tokens": 1024,
                                        "stream": step["stream"],
                                        "messages": [{"role": "user", "content": step["prompt"]}]}
                if step["tools"]:
                    body["tools"] = [{"name": "Edit", "description": "Edit a file",
                                      "input_schema": {"type": "object"}}]
                r = await client.post("/v1/messages", json=body,
                                      headers={"anthropic-version": "2023-06-01"})
                cache = r.headers.get("x-claude-proxy-cache", "?")
                results.append({"label": step["label"], "stream": step["stream"],
                                "status": r.status_code, "cache": cache})
                if not quiet:
                    mode = "stream" if step["stream"] else "json  "
                    print(f"  [{cache.upper():4}] {mode}  {step['label']}")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()

    entries = []
    for f in sorted((home / "logs").glob("*.jsonl")):
        entries.extend(parse_log_file(f))
    return {"steps": results, "log": entries,
            "upstream_calls": mock.calls}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="print the step results and log entries as JSON")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="claude-proxy-demo-") as tmp:
        home = Path(tmp)
        if not args.json:
            print("🧪 claude-cost-proxy demo (mock upstream, no API key, no network)\n")
            print("Sending 6 requests through the proxy:\n")
        result = asyncio.run(run_demo(home, quiet=args.json))
        if args.json:
            json.dump(result, sys.stdout, indent=2)
            print()
            return
        print(f"\nUpstream received {result['upstream_calls']} of {len(SCRIPT)} requests; "
              "the rest were served from the proxy cache.")
        print("Token counts come from the mock and are illustrative.\n")
        print_summary(home / "logs")
        print("Next: run `claude-proxy-setup` with your own key to use it with Claude Code.")


if __name__ == "__main__":
    main()
