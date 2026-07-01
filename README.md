# hydroai

Estimate data center water consumption from AI CLI tool usage.

Tracks bytes sent/received over the network by AI tools (opencode, Claude Code, AntiGravity, or any script using the Anthropic/OpenAI APIs) and estimates the water used for data center cooling.

## How it works

```
AI Tool (opencode, Claude, etc.)
  |
  |--- connect() / send() / recv() ---> API server (Anthropic/OpenAI)
  |
  |  LD_PRELOAD hook intercepts socket calls
  |  Writes byte counts to shared memory (mmap file)
  |
  v
hydroai monitor reads shared memory
  |
  v
Converts bytes -> tokens -> water mL
  |
  v
Stores in SQLite (~/.hydroai/hydroai.db)
```

Two tracking strategies, used together:

**1. IP-based** — Resolves known API hostnames (`api.anthropic.com`, `api.openai.com`) to IPs and tracks traffic to those IPs.

**2. Process-name-based** — Tracks all non-localhost socket traffic from processes matching known tool names (`opencode`, `claude`). Catches tools that route through proxies/CDNs (e.g., opencode via Cloudflare).

### Components

| Layer | File | Role |
|-------|------|------|
| C shared library | `tracker_ldpreload.c` | Hooks `connect`, `send`, `recv`, `sendto`, `recvfrom`, `sendmsg`, `recvmsg`, `write`, `writev`, `read`, `close` via `LD_PRELOAD` |
| Shared memory | `/tmp/watermeter_shm` | mmap'd file — 256 PID slots with per-process byte counters |
| Python monitor | `tracker_hook.py` | Compiles `.so`, creates SHM, resolves IPs, reads byte counts |
| CLI | `__main__.py` | `hydroai run`, `status`, `stats`, `hook`, etc. |
| Estimation | `watercalc.py` | Bytes → tokens → water mL conversion |
| Storage | `storage.py` | SQLite persistence at `~/.hydroai/hydroai.db` |
| Proxy mode | `proxy.py` | Alternative HTTP/HTTPS proxy approach (legacy) |
| Opencode plugin | `opencode-plugin.mjs` | Server plugin appending water stats to each AI response |

### Water calculation

```
encrypted bytes on wire  /  5.2  =  estimated tokens
input tokens  *  3.0 mL  / 1000  =  water for input
output tokens * 15.0 mL  / 1000  =  water for output
```

- **5.2 bytes/token**: average token size including TLS framing overhead
- **3.0 mL / 1K input tokens**: cooling water for processing input
- **15.0 mL / 1K output tokens**: cooling water for generating output (5x more compute)

Rates are based on published data-center water efficiency research (typical evaporative cooling in US data centers).

## Installation

```sh
pip install git+https://github.com/mr-mister007/hydroai.git
```

Or from source:

```sh
git clone https://github.com/mr-mister007/hydroai.git
cd hydroai
pip install -e .
```

Requires: Python 3.10+, GCC (for compiling the LD_PRELOAD library).

## Usage

### Track any command

```sh
# Run a command under the hook
hydroai run opencode run "explain how transformers work"

# Run interactive TUI
hydroai run opencode

# Run any Python script using Anthropic/OpenAI
hydroai run python3 my_script.py
```

### Check results

```sh
# Last session stats
hydroai status

# All-time stats with tool/API breakdown
hydroai stats

# Reset all data
hydroai reset
```

### Commands

| Command | Description |
|---------|-------------|
| `run <command> [args...]` | Run a command under the LD_PRELOAD hook |
| `status` | Show current session stats |
| `stats` | Show all-time statistics |
| `hook` | Print environment variables for manual LD_PRELOAD setup |
| `estimate <tokens>` | Estimate water for N tokens |
| `watch` | Live monitor (pairs with `start`/proxy mode) |
| `reset` | Delete all stored data |

## Opencode plugin

The opencode plugin appends a water usage footer after each AI response:

```
Hello

—— 🌊 12.5 mL for this response · 158.3 mL total ——
```

### Install

Add the plugin to your opencode config (`~/.config/opencode/opencode.jsonc`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["/path/to/hydroai/opencode-plugin.mjs"]
}
```

The plugin reads the same SHM file created by `hydroai run`. Run opencode through hydroai for the footer to appear:

```sh
hydroai run opencode
```

## Architecture details

### LD_PRELOAD hook (`tracker_ldpreload.c`)

The C shared library intercepts libc socket functions. On `connect()`, it checks the destination against tracked IPs or the process's own name (via `/proc/self/comm`). If matched, the file descriptor is marked as tracked. All subsequent `send`/`recv`/`write`/`read` calls on that fd accumulate byte counts in shared memory.

Key design decisions:

- **Constructor init** (`__attribute__((constructor))`): resolves function pointers and reads env vars before any application code runs. Avoids deadlocks from hooking `write`/`read` during lazy resolution.
- **Per-PID hash table**: 256 slots indexed by `PID % 256` with linear probing for collisions. Each slot holds PID, comm name, bytes_sent, bytes_received, last_active timestamp.
- **Shared memory (mmap file)**: file at `/tmp/hydroai_shm` is mmap'd by both the C hook (write) and Python monitor (read). Atomic operations for thread-safe updates.

### Process-name tracking

The hook's constructor reads `/proc/self/comm` and compares against `WATERMETER_PROC_NAMES` (comma-separated list). If matched, all non-localhost connections are tracked regardless of destination IP. This catches AI tools that route through shared/CDN infrastructure (e.g., opencode through Cloudflare).

### Why not a proxy?

A proxy approach was prototyped first (`proxy.py`) but requires manual configuration of every tool to route through the proxy. The LD_PRELOAD approach works transparently with any tool without configuration changes.

## Project structure

```
hydroai/
├── pyproject.toml              # Package metadata
├── README.md
└── hydroai/
    ├── __init__.py
    ├── __main__.py             # CLI entry point
    ├── tracker_ldpreload.c     # C shared library (LD_PRELOAD hook)
    ├── tracker_hook.py         # Python bindings for the C hook
    ├── watercalc.py            # Water estimation logic
    ├── storage.py              # SQLite persistence
    ├── proxy.py                # Legacy HTTP/HTTPS proxy
    └── opencode-plugin.mjs     # Opencode server plugin
```

## Limitations

- **TLS overhead**: encrypted bytes overcount actual token sizes by ~20-30%, leading to slight overestimation
- **Process-name matching**: tracking all non-localhost traffic from matched processes may include non-API network activity
- **opencode plugin**: modifies response text in conversation history (the AI sees its own water usage on subsequent turns)
- **IPv6-only connections**: not currently tracked by the C hook
- **Water rates**: based on US average; actual water intensity varies by region, data center cooling technology, and seasonal factors
