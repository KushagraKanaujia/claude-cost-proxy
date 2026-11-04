"""End-to-end tests: the real proxy against the bundled mock upstream."""
import asyncio

from claude_cost_proxy.demo import SCRIPT, run_demo
from claude_cost_proxy.pricing import cost_usd, prices_for
from claude_cost_proxy.server import parse_sse_usage


def test_demo_session_hits_and_misses(tmp_path):
    result = asyncio.run(run_demo(tmp_path, quiet=True))
    caches = [s["cache"] for s in result["steps"]]
    assert caches == ["miss", "hit", "miss", "hit", "miss", "miss"]
    assert all(s["status"] == 200 for s in result["steps"])
    assert result["upstream_calls"] == 4
    assert len(result["log"]) == len(SCRIPT)


def test_streamed_usage_is_logged(tmp_path):
    result = asyncio.run(run_demo(tmp_path, quiet=True))
    streamed_miss = result["log"][2]
    assert streamed_miss["stream"] is True
    assert streamed_miss["usage"]["output_tokens"] == 640
    assert streamed_miss["usage"]["input_tokens"] > 0
    assert streamed_miss["cost_usd"] > 0
    streamed_hit = result["log"][3]
    assert streamed_hit["cache_hit"] is True
    assert streamed_hit["saved_usd"] == streamed_miss["cost_usd"]


def test_parse_sse_usage_detects_tool_use():
    raw = (
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t","name":"Bash","input":{}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{},"usage":{"output_tokens":42}}\n\n'
    )
    usage, tool = parse_sse_usage(raw)
    assert usage == {"input_tokens": 10, "output_tokens": 42}
    assert tool is True


def test_pricing_prefix_match():
    assert prices_for("claude-opus-4-20250514")["input"] == 15.00
    assert prices_for("claude-opus-4-5-20251101")["input"] == 5.00
    assert prices_for("claude-sonnet-4-5-20250929")["input"] == 3.00
    assert prices_for("something-unknown")["input"] == 3.00
    assert round(cost_usd("claude-haiku-4-5", {"input_tokens": 1_000_000}), 6) == 1.0
