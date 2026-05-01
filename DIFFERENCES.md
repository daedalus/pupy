# pu.py vs pu.sh - Differences Analysis

## Overview

| Aspect | pu.sh (Original) | pu.py (Python Port) |
|--------|-------------------|---------------------|
| Lines of code | 392 | 1602 |
| Language | Bash + awk + curl | Python 3 |
| Dependencies | sh, curl, awk (standard Unix tools) | Python standard library (urllib, json, threading) |
| File size | 43.1K | 54.5K |

## Major Differences

### 1. **Additional Provider Support**
**pu.py adds OpenCode provider support:**
- `opencode` provider with `OPENCODE_API_KEY`
- Models: `big-pickle`, `minimax`, `glm`, `qwen`, `kimi`
- Prompt-based tool calling for models that don't support native tools
- Separate handling for Anthropic-compatible vs OpenAI-compatible OpenCode endpoints

**pu.sh only supports:**
- `anthropic` (Claude models)
- `openai` (GPT models)

### 2. **No MCP Support in Either Version**
Neither pu.sh nor pu.py include MCP (Model Context Protocol) integrations. The MCP tools visible in the environment (mcp-ghidra, mcp-hashlib, semgrep, mempalace) are separate tools available in the opencode platform, not part of pu.py itself.

### 3. **Context Management**
**pu.sh:**
- Custom awk-based context compaction (`_ctx_entries()`, `_ctx_tail_start()`, etc.)
- Local memory compaction with `_ctx_local_memory()`
- Multiple fallback strategies

**pu.py:**
- Python-based context management
- Token-based tracking with `CTX_LIMIT`, `AGENT_RESERVE`, `AGENT_KEEP_RECENT`
- Automatic compaction based on context size

### 4. **Threading & Spinners**
**pu.sh:**
- Background process spinner using `&` and process management
- Uses `tput` for terminal control

**pu.py:**
- Python `threading` module for spinner
- `SPIN_LOCK` for thread safety
- Cleaner terminal output handling

### 5. **JSON Parsing**
**pu.sh:**
- Custom JSON parsing using awk (`jp()`, `jb()`, `each_tool_use()`, `o_items()`)
- Handles Unicode escapes, nested objects, arrays
- No external JSON parser dependency

**pu.py:**
- Uses Python's built-in `json` module
- `extract_json()` helper for extracting JSON from text
- `ParsedResponse` class for structured response handling

### 6. **Features Present in pu.sh but Missing in pu.py**

#### A. **Skill System**
```bash
# pu.sh has:
_skill()  # Load skills from .pi/skills or .agents/skills
# Command: /skill:name
```

#### B. **Template System**
```bash
# pu.sh has:
_tpl()  # Load prompt templates from .pi/prompts
# Command: /template-name
```

#### C. **Session Export**
```bash
# pu.sh has:
_export()  # Export session to markdown
# Command: /export [filename]
```

#### D. **Setup Wizard**
```bash
# pu.sh has:
_setup()  # Interactive API key setup
# Command: /login
```

#### E. **Log Replay**
```bash
# pu.sh has:
_replay()  # Replay last messages from log
```

### 7. **Token & Cost Tracking**
**pu.sh:**
- Shell-based arithmetic for token counting
- Awk-based cost calculation
- `track_tokens()` function

**pu.py:**
- Thread-safe token tracking with `TOKEN_LOCK`
- `track_tokens()` with support for both Anthropic and OpenAI formats
- Price calculation using `AGENT_PRICE_IN_PER_MTOK` and `AGENT_PRICE_OUT_PER_MTOK`

### 8. **Argument Parsing**
**pu.sh:**
- Manual argument parsing with `while` loop and `case`
- Inline help text

**pu.py:**
- Uses Python `argparse` module
- Cleaner argument definitions

### 9. **Error Handling**
**pu.sh:**
- Uses `set -u` for undefined variable detection
- Custom `_kill_tree()` for process cleanup
- Signal handling with `trap`

**pu.py:**
- Try/except blocks
- `subprocess` module for command execution
- Signal handling with `signal` module

### 10. **Code Structure**
**pu.sh:**
- Procedural, single-file script
- Functions defined inline
- Compact, minimal style

**pu.py:**
- More structured with classes (`ParsedResponse`)
- Separation of concerns (API calls, response parsing, tool execution)
- More verbose but more maintainable

## Summary

The Python port (pu.py) is approximately 4x larger due to:
1. Additional OpenCode provider support
2. More structured Python code with proper classes and error handling
3. Threading support for better UX
4. Python's verbosity compared to compact shell/awk constructs

**Missing features in pu.py (present in pu.sh):**
- ✅ Skill system (`/skill:name`) - NOW IMPLEMENTED
- ✅ Template system (`/template`) - NOW IMPLEMENTED  
- ✅ Session export (`/export`) - Already had this
- ✅ Setup wizard (`/login`) - Already had this
- ✅ Log replay functionality - Already had this

**Additional features in pu.py (not in pu.sh):**
- ✅ OpenCode provider support
- ✅ Thread-safe token tracking
- ✅ Prompt-based tool calling for non-native models
