# Claude Cost Proxy

**See what every Claude Code request costs, and stop paying twice for the same answer.** A small local proxy that logs tokens and estimated USD cost per request and replays safe repeat responses from a cache.

[![Live demo](https://img.shields.io/badge/live%20demo-netlify-00C7B7?logo=netlify&logoColor=white)](https://kush-claude-cost-proxy.netlify.app)
[![Open in GitHub Codespaces](https://img.shields.io/badge/Open%20in-Codespaces-181717?logo=github)](https://codespaces.new/KushagraKanaujia/claude-cost-proxy?quickstart=1)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Try it in one click

| | What you get | Needs a key? |
|---|---|---|
| **[Live demo page](https://kush-claude-cost-proxy.netlify.app)** | In-browser simulator of the caching rules, plus real output from the demo below | No |
| **[Open in Codespaces](https://codespaces.new/KushagraKanaujia/claude-cost-proxy?quickstart=1)** | A ready Python 3.12 environment that installs the package and runs `claude-proxy-demo` automatically | No |
| **Locally** | `pip install git+https://github.com/KushagraKanaujia/claude-cost-proxy && claude-proxy-demo` | No |

`claude-proxy-demo` runs the **real proxy** against a local mock of the Anthropic API, sends six scripted requests (plain and streamed, with and without tool use), and prints the usage summary. It needs no API key or network access and writes nothing to your home directory. Its token counts come from the mock.

```
  [MISS] json    Ask a question
  [HIT ] json    Ask the same question again
  [MISS] stream  Streamed question (how Claude Code calls)
  [HIT ] stream  Same streamed question again
  [MISS] stream  Request that triggers a file edit (tool use)
  [MISS] stream  Same edit request again

Upstream received 4 of 6 requests; the rest were served from the proxy cache.
```

## What it does

- **Cost tracking.** Every `POST /v1/messages` is logged with model, latency, input/output/cache tokens and an estimated USD cost from a built-in price table.
- **Response caching.** Responses that contain no tool use (explanations, reviews, answers) are cached for 24 hours and replayed for identical requests, for both JSON and streamed (SSE) responses.
- **Safe by default.** Responses with tool use (file edits, shell commands) are never cached. A cached answer is skipped if a file path mentioned in the prompt has changed since it was cached. Error responses are passed through and never cached.
- **Usage summary.** `claude-proxy-summary` totals requests, hit rate, tokens, cost and savings for today and the last 7 days.

## How it works

```
Claude Code ──(ANTHROPIC_BASE_URL=http://localhost:8080)──▶ claude-proxy ──(cache miss only)──▶ api.anthropic.com
                                                              │
                                    ~/.throttle/logs/*.jsonl ◀┘▶ ~/.throttle/cache/*.json
```

1. The request body is hashed (SHA-256 of sorted JSON).
2. If a fresh cache entry exists, it is returned (JSON, or the recorded SSE stream replayed) and logged as a hit with the cost it saved.
3. Otherwise it is forwarded unchanged. Streamed responses are relayed chunk by chunk while usage is read from the `message_start` and `message_delta` events.
4. Successful responses without tool use are written to the cache.

`POST /v1/messages/count_tokens` is passed through untouched; `GET /health` reports status.

Note: cache hits need an exactly identical request. In real Claude Code sessions the conversation history grows each turn, so hits mostly come from repeated one-off questions rather than ongoing chats.

## Run it with Claude Code

Requires Python 3.11+ and an Anthropic Console API key (from https://console.anthropic.com/settings/keys). The proxy itself is free.

```bash
pip install git+https://github.com/KushagraKanaujia/claude-cost-proxy

claude-proxy-setup        # saves your key to ~/.throttle/config.json (mode 0600)
                          # and sets ANTHROPIC_BASE_URL in ~/.claude/settings.json
claude-proxy              # start the proxy and keep it running
# restart Claude Code, use it normally, then:
claude-proxy-summary
```

### Commands

| Command | Purpose |
|---|---|
| `claude-proxy [PORT]` | Start the proxy (default port 8080) |
| `claude-proxy-setup [PORT]` | Save your API key and configure Claude Code |
| `claude-proxy-summary` | Show cost and cache stats for today and the last 7 days |
| `claude-proxy-demo [--json]` | Run the zero-key demo against a mock upstream |

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | from `config.json` | Key the proxy sends upstream. If unset everywhere, the client's own `x-api-key` / `Authorization` header is forwarded. |
| `CLAUDE_PROXY_HOME` | `~/.throttle` | Where `config.json`, `logs/` and `cache/` live |
| `CLAUDE_PROXY_UPSTREAM` | `https://api.anthropic.com` | Upstream base URL |
| `THROTTLE_PORT` | `8080` | Default port |

### Turning it off

Stop the proxy with `Ctrl+C`. To stop Claude Code from using it, remove `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` from the `env` block in `~/.claude/settings.json` and restart Claude Code.

### Pricing table

Costs are estimates from list prices in [`claude_cost_proxy/pricing.py`](claude_cost_proxy/pricing.py), matched by model-ID prefix (unknown models fall back to Sonnet 4 rates). They are not your bill. Check https://www.anthropic.com/pricing and edit the table if rates change.

## Tech stack

Python 3.11+ · [aiohttp](https://github.com/aio-libs/aiohttp) (server) · [httpx](https://github.com/encode/httpx) (upstream client) · pytest · static demo page with [Pico CSS](https://picocss.com)

## Development

```bash
git clone https://github.com/KushagraKanaujia/claude-cost-proxy && cd claude-cost-proxy
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest                       # end-to-end tests against the mock upstream
claude-proxy-demo
```

The demo page lives in [`site/`](site). Regenerate its sample output with `python -m claude_cost_proxy.demo > site/demo-output.txt`, then deploy with `netlify deploy --prod --dir=site`.

This proxy was originally built inside [Throttle](https://github.com/KushagraKanaujia/throttle) to measure real-world cache effectiveness and was extracted into its own package.

## Credits

- [Pico CSS](https://github.com/picocss/pico) (MIT) for the demo page styling, loaded from jsDelivr
- [devcontainers/images](https://github.com/devcontainers/images) (MIT) Python image for Codespaces
- [shields.io](https://shields.io) (CC0) badges
- [aiohttp](https://github.com/aio-libs/aiohttp) (Apache-2.0) and [httpx](https://github.com/encode/httpx) (BSD-3-Clause)

Not affiliated with Anthropic.

## License

[MIT](LICENSE)
