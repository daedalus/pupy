# pu.py

![pu.py logo](logos/logo.png)

**Portable agentic harness** — A Python port of [pu.sh](https://github.com/NahimNasser/pu) that provides an intelligent coding assistant with tool-calling capabilities.

## Features

✅ **Feature parity with pu.sh** — All original pu.sh features are implemented:
- Multi-provider support (Anthropic, OpenAI, OpenCode)
- Agentic tools (bash, read, write, edit, grep, find, ls)
- Interactive mode with commands (/model, /effort, /login, /logout, /flush, /compact, /export, /session, /skill:name, /template)
- Pipe mode for CI/automation
- Token & cost tracking with real-time monitoring
- Session persistence and history
- Context management with automatic compaction
- Skill system (`/skill:name`)
- Template system (`/template-name`)
- Session export to markdown (`/export`)
- Setup wizard (`/login`)
- Log replay functionality

**Additional pu.py features:**
- Thread-safe token tracking
- Prompt-based tool calling for non-native models (OpenCode)
- Structured Python code with proper error handling

## Implementation Details

| Aspect | pu.sh (Original) | pu.py (Python Port) |
|--------|-------------------|---------------------|
| Lines of code | 392 | 1640+ |
| Language | Bash + awk + curl | Python 3 |
| Dependencies | sh, curl, awk (standard Unix tools) | Python standard library |
| File size | 43.1K | 54.5K+ |

### Key Differences

**pu.py adds:**
- OpenCode provider support (big-pickle, qwen, glm, minimax, kimi)
- Thread-safe token tracking with TOKEN_LOCK
- Prompt-based tool calling for models without native tool support
- Python structured code with classes (ParsedResponse)
- Better error handling with try/except blocks

**pu.sh advantages:**
- More compact (392 lines vs 1640+)
- No Python dependency
- Uses standard Unix tools (curl, awk)
- Custom JSON parsing without external dependencies

## Quick Start

```bash
# Make executable
chmod +x pu.py

# Run with a task
./pu.py "list all Python files in this directory"

# Interactive mode
./pu.py

# Pipe mode (for CI/automation)
echo "review this code" | ./pu.py --pipe

# Show cost tracking
./pu.py --cost "optimize this function"
```

## Configuration

Create `~/.pu.env` for persistent configuration:

```bash
export ANTHROPIC_API_KEY="your-key-here"
export OPENAI_API_KEY="your-key-here"
export OPENCODE_API_KEY="public"
export AGENT_PROVIDER="anthropic"        # or "openai", "opencode"
export AGENT_MODEL="claude-opus-4-7"     # or "gpt-5.5", "big-pickle"
export AGENT_EFFORT="medium"              # low, medium, high, xhigh, max
export AGENT_MAX_STEPS="100"
export AGENT_MAX_TOKENS="4096"
export AGENT_CONTEXT_LIMIT="400000"
export AGENT_VERBOSE="1"
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key | - |
| `OPENAI_API_KEY` | OpenAI API key | - |
| `OPENCODE_API_KEY` | OpenCode API key | `public` |
| `AGENT_PROVIDER` | AI provider | auto-detect |
| `AGENT_MODEL` | Model to use | provider-specific |
| `AGENT_EFFORT` | Reasoning effort | `medium` |
| `AGENT_MAX_STEPS` | Max agent steps | `100` |
| `AGENT_MAX_TOKENS` | Max tokens per response | `4096` |
| `AGENT_CONTEXT_LIMIT` | Context window size | `400000` |
| `AGENT_LOG` | Event log file | `.pu-events.jsonl` |
| `AGENT_HISTORY` | History file | `.pu-history.json` |
| `AGENT_VERBOSE` | Verbose output | `1` |
| `AGENT_CONFIRM` | Confirmation mode | `0` |
| `AGENT_RESERVE` | Token reserve | `16000` |
| `AGENT_KEEP_RECENT` | Recent tokens to keep | `80000` |
| `AGENT_TOOL_TRUNC` | Tool output truncation | `100000` |
| `AGENT_READ_MAX` | Max file read size | `1000000` |
| `AGENT_PRICE_IN_PER_MTOK` | Input token price | `0` |
| `AGENT_PRICE_OUT_PER_MTOK` | Output token price | `0` |

## Interactive Commands

When running in interactive mode, use these commands:

- `/model [name]` — Switch AI model
- `/effort` — Change reasoning effort
- `/login` / `/logout` — Manage API keys
- `/flush` — Clear conversation history
- `/compact` — Compact context
- `/export` — Export conversation
- `/session` — Show session info
- `/quit` — Exit
- `!cmd` — Run shell command directly

## Supported Models

### Anthropic
- `claude-opus-4-7` (default for Anthropic)
- `claude-sonnet-4-6`
- `claude-opus-4-6`
- `claude-opus-4-5`

### OpenAI
- `gpt-5.5` (default for OpenAI)
- `o1`, `o3`, `o4` series

### OpenCode
- `big-pickle` (default for OpenCode)
- `minimax`
- `glm`
- `qwen`
- `kimi`

## Technical Comparison

### Context Management

**pu.sh:**
- Custom awk-based context compaction (`_ctx_entries()`, `_ctx_tail_start()`, etc.)
- Local memory compaction with `_ctx_local_memory()`
- Multiple fallback strategies

**pu.py:**
- Python-based context management
- Token-based tracking with `CTX_LIMIT`, `AGENT_RESERVE`, `AGENT_KEEP_RECENT`
- Automatic compaction based on context size

### Threading & Spinners

**pu.sh:**
- Background process spinner using `&` and process management
- Uses `tput` for terminal control

**pu.py:**
- Python `threading` module for spinner
- `TOKEN_LOCK` for thread safety
- Cleaner terminal output handling

### JSON Parsing

**pu.sh:**
- Custom JSON parsing using awk (`jp()`, `jb()`, `each_tool_use()`, `o_items()`)
- Handles Unicode escapes, nested objects, arrays
- No external JSON parser dependency

**pu.py:**
- Uses Python's built-in `json` module
- `extract_json()` helper for extracting JSON from text
- `ParsedResponse` class for structured response handling

### Token & Cost Tracking

**pu.sh:**
- Shell-based arithmetic for token counting
- Awk-based cost calculation
- `track_tokens()` function

**pu.py:**
- Thread-safe token tracking with `TOKEN_LOCK`
- `track_tokens()` with support for both Anthropic and OpenAI formats
- Price calculation using `AGENT_PRICE_IN_PER_MTOK` and `AGENT_PRICE_OUT_PER_MTOK`

### Error Handling

**pu.sh:**
- Uses `set -u` for undefined variable detection
- Custom `_kill_tree()` for process cleanup
- Signal handling with `trap`

**pu.py:**
- Try/except blocks
- `subprocess` module for command execution
- Signal handling with `signal` module

## Requirements

- Python 3.7+
- Internet connection
- API key for your chosen provider

## License

MIT
