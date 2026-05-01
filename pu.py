#!/usr/bin/env python3
"""
pu.py — portable agentic harness. Python port of pu.sh.
Usage: ./pu.py "task" | ./pu.py | ./pu.py --pipe "review"
"""

import argparse
import datetime
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# Threading lock for token tracking
TOKEN_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Globals / config
# ---------------------------------------------------------------------------

STATE = "idle"
CHILD_PROC: Optional[subprocess.Popen] = None
SPIN_THREAD: Optional[threading.Thread] = None
SPIN_STOP_EVENT = threading.Event()
SPIN_MSG = ""

MSGS: list = []
TOKEN_IN = 0
TOKEN_OUT = 0
COST_USD = 0.0

# ---------------------------------------------------------------------------
# Environment / config loading
# ---------------------------------------------------------------------------

def clean_key(v: str) -> str:
    v = v.strip()
    v = re.sub(r'^export\s*', '', v)
    v = re.sub(r'^OPENAI_API_KEY=', '', v)
    v = re.sub(r'^ANTHROPIC_API_KEY=', '', v)
    v = v.strip('"').strip("'").strip()
    return v


def load_env():
    env_file = Path.home() / ".pu.env"
    if not env_file.exists():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = re.sub(r'^export\s*', '', k.strip())
            v = clean_key(v)
            if k == 'OPENAI_API_KEY' and not os.environ.get('OPENAI_API_KEY'):
                os.environ['OPENAI_API_KEY'] = v
            elif k == 'ANTHROPIC_API_KEY' and not os.environ.get('ANTHROPIC_API_KEY'):
                os.environ['ANTHROPIC_API_KEY'] = v
            elif k == 'AGENT_PROVIDER' and not os.environ.get('AGENT_PROVIDER'):
                os.environ['AGENT_PROVIDER'] = v
            elif k == 'AGENT_MODEL' and not os.environ.get('AGENT_MODEL'):
                os.environ['AGENT_MODEL'] = v
            elif k == 'AGENT_EFFORT' and not os.environ.get('AGENT_EFFORT'):
                os.environ['AGENT_EFFORT'] = v
            elif k == 'OPENCODE_API_KEY' and not os.environ.get('OPENCODE_API_KEY'):
                os.environ['OPENCODE_API_KEY'] = v


load_env()


def _get(key, default=''):
    return os.environ.get(key, default)


# Determine provider
def resolve_provider_model():
    provider = _get('AGENT_PROVIDER')
    model = _get('AGENT_MODEL')
    if not provider:
        if re.match(r'^(gpt-|o1|o3|o4)', model):
            provider = 'openai'
        elif model.startswith('claude-'):
            provider = 'anthropic'
        elif model.startswith('big-pickle'):
            provider = 'opencode'
        elif _get('OPENCODE_API_KEY') and not _get('OPENAI_API_KEY') and not _get('ANTHROPIC_API_KEY'):
            provider = 'opencode'
        elif _get('OPENAI_API_KEY') and not _get('ANTHROPIC_API_KEY'):
            provider = 'openai'
        else:
            provider = 'anthropic'
    if not model:
        if provider == 'openai':
            model = 'gpt-5.5'
        elif provider == 'opencode':
            model = 'big-pickle'
        else:
            model = 'claude-opus-4-7'
    return provider, model


PROVIDER, MODEL = resolve_provider_model()
OPENAI_API_KEY = clean_key(_get('OPENAI_API_KEY'))
ANTHROPIC_API_KEY = clean_key(_get('ANTHROPIC_API_KEY'))
OPENCODE_API_KEY = clean_key(_get('OPENCODE_API_KEY', 'public'))

MAX_STEPS = int(_get('AGENT_MAX_STEPS', '100'))
MAX_TOKENS = int(_get('AGENT_MAX_TOKENS', '4096'))
AGENT_RESERVE = int(_get('AGENT_RESERVE', '16000'))
AGENT_KEEP_RECENT = int(_get('AGENT_KEEP_RECENT', '80000'))
AGENT_TOOL_TRUNC = int(_get('AGENT_TOOL_TRUNC', '100000'))
AGENT_READ_MAX = int(_get('AGENT_READ_MAX', '1000000'))
LOG_FILE = _get('AGENT_LOG', '.pu-events.jsonl')
HISTORY_FILE = _get('AGENT_HISTORY', '.pu-history.json')
CONFIRM = _get('AGENT_CONFIRM', '0') == '1'
CTX_LIMIT = int(_get('AGENT_CONTEXT_LIMIT', '400000'))
VERBOSE = _get('AGENT_VERBOSE', '1') == '1'
EFFORT = _get('AGENT_EFFORT', _get('AGENT_THINKING', 'medium'))
PIPE_MODE = False
COST_MODE = False
INTERACTIVE = 0

# Effort OK flags
EFFORT_OK = False

def _set_effort_ok():
    global EFFORT_OK, CTX_LIMIT
    pm = f"{PROVIDER}:{MODEL}"
    if pm.startswith('openai:gpt-5.5'):
        EFFORT_OK = True
        if not _get('AGENT_CONTEXT_LIMIT'):
            CTX_LIMIT = 400000
    elif pm.startswith('anthropic:claude-opus-4-7'):
        EFFORT_OK = True
        if not _get('AGENT_CONTEXT_LIMIT'):
            CTX_LIMIT = 272000
    elif re.match(r'anthropic:claude-(opus-4-6|sonnet-4-6|opus-4-5)', pm):
        EFFORT_OK = True

_set_effort_ok()

SYSTEM = _get('AGENT_SYSTEM', (
    f"You are an expert coding assistant. You can read, write, edit, grep, find, ls, and run bash.\n\n"
    f"Tools: read(path,offset,limit); bash(command); edit(path,oldText,newText); write(path,content); "
    f"grep(pattern,path); find(path,name); ls(path).\n\n"
    f"Guidelines: prefer grep/find/ls over bash for exploration; working directory is {os.getcwd()}, "
    f"do not cd in bash commands; combine related grep searches with alternation.\n\n"
    f"Use read instead of cat/sed; write only for new files or complete rewrites; edit only with exact "
    f"unique oldText, reading surrounding lines after failures and never retrying the same failed oldText.\n\n"
    f"Before tool calls briefly say what you are checking/changing; be concise; show file paths clearly.\n\n"
    f"Current date: {datetime.date.today().isoformat()}\n\n"
    f"Current working directory: {os.getcwd()}\n\n"
    f"Your source code is at {Path(__file__).resolve()}. Use read to inspect it if asked about your capabilities/configuration."
))

# Tool definitions for prompt-based tool calling (used when API doesn't support native tools)
TOOL_DEFINITIONS = """
Available tools. When you need to use a tool, output ONLY the tool name and parameters in this EXACT format:

TOOL: tool_name
PARAM: {"param1": "value1", "param2": "value2"}

Example:
TOOL: bash
PARAM: {"command": "ls -la"}

Available tools:
1. bash - Run a shell command
   PARAM: {"command": "shell command string"}

2. read - Read file contents
   PARAM: {"path": "file path", "offset": optional line number, "limit": optional max lines}

3. write - Write content to file (creates dirs if needed)
   PARAM: {"path": "file path", "content": "file content"}

4. edit - Edit file with exact text replacement
   PARAM: {"path": "file path", "oldText": "exact text to find", "newText": "replacement text"}

5. grep - Search for pattern in files
   PARAM: {"pattern": "regex pattern", "path": "directory path"}

6. find - Find files by name glob
   PARAM: {"path": "directory path", "name": "glob pattern"}

7. ls - List directory
   PARAM: {"path": "directory path"}

IMPORTANT: Output ONLY the TOOL: and PARAM: lines when using a tool. I will execute it and return the result.
"""

# Check if current model supports native tool calling
def supports_native_tools() -> bool:
    if PROVIDER == 'opencode':
        # big-pickle and similar models don't support native tools via opencode Zen
        return not (MODEL.startswith('big-pickle') or MODEL.startswith('minimax') or MODEL.startswith('glm') or MODEL.startswith('qwen') or MODEL.startswith('kimi'))
    return True

# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

def _spinner_loop():
    frames = ['[   ]', '[=  ]', '[== ]', '[===]', '[ ==]', '[  =]']
    i = 0
    try:
        while not SPIN_STOP_EVENT.is_set():
            sys.stderr.write(f'\r\033[K{frames[i % len(frames)]} {SPIN_MSG}')
            sys.stderr.flush()
            i += 1
            time.sleep(0.15)
    except Exception:
        pass


def spin_start(msg=''):
    global SPIN_THREAD, SPIN_MSG
    if not sys.stderr.isatty():
        return
    SPIN_MSG = msg
    SPIN_STOP_EVENT.clear()
    SPIN_THREAD = threading.Thread(target=_spinner_loop, daemon=True)
    SPIN_THREAD.start()


def spin_stop():
    global SPIN_THREAD
    SPIN_STOP_EVENT.set()
    if SPIN_THREAD:
        SPIN_THREAD.join(timeout=1)
        SPIN_THREAD = None
    if sys.stderr.isatty():
        sys.stderr.write('\r\033[K')
        sys.stderr.flush()

# ---------------------------------------------------------------------------
# Logging / output helpers
# ---------------------------------------------------------------------------

def _mkparent(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def log_event(step, typ, content):
    _mkparent(LOG_FILE)
    entry = json.dumps({"s": step, "t": typ, "c": content})
    with open(LOG_FILE, 'a') as f:
        f.write(entry + '\n')


def info(msg):
    if not PIPE_MODE:
        sys.stderr.write(f'\r\033[K\033[36m[pu]\033[0m {msg}\n')
        sys.stderr.flush()


def err(msg):
    sys.stderr.write(f'\r\033[K\033[31m[!] {msg}\033[0m\n')
    sys.stderr.flush()


def dbg(msg):
    if VERBOSE:
        sys.stderr.write(f'\r\033[K[v] {msg}\n')
        sys.stderr.flush()


def _p(path):
    cwd = os.getcwd()
    home = str(Path.home())
    if path.startswith(cwd + '/'):
        return path[len(cwd)+1:]
    elif path.startswith(home + '/'):
        return '~' + path[len(home):]
    return path


def _tool_log(name, detail):
    if not PIPE_MODE:
        sys.stderr.write(f'\r\033[K\033[2m⏺\033[0m \033[36m{name}\033[0m \033[2m{detail}\033[0m\n')
        sys.stderr.flush()


def _say(text):
    if not PIPE_MODE:
        sys.stderr.write(f'\r\033[K{text}\n')
        sys.stderr.flush()

# ---------------------------------------------------------------------------
# Token tracking
# ---------------------------------------------------------------------------

def track_tokens(resp: dict):
    global TOKEN_IN, TOKEN_OUT, COST_USD
    usage = resp.get('usage', {})
    # Handle both Anthropic format (input_tokens/output_tokens) and OpenAI format (prompt_tokens/completion_tokens)
    a = usage.get('input_tokens', usage.get('prompt_tokens', 0))
    b = usage.get('output_tokens', usage.get('completion_tokens', 0))
    pi = float(_get('AGENT_PRICE_IN_PER_MTOK', '0'))
    po = float(_get('AGENT_PRICE_OUT_PER_MTOK', '0'))
    cost = ((a or 0) * pi + (b or 0) * po) / 1_000_000
    with TOKEN_LOCK:
        TOKEN_IN += a or 0
        TOKEN_OUT += b or 0
        COST_USD += cost


def _fmtk(n):
    if n >= 1_000_000:
        return f'{n/1_000_000:.1f}M'
    elif n >= 1000:
        return f'{n/1000:.0f}k'
    return str(n)


def _ctxp():
    msgs_len = len(json.dumps(MSGS))
    pct = (100 * msgs_len / CTX_LIMIT) if CTX_LIMIT else 0
    return f'{pct:.1f}%/{CTX_LIMIT//1000}k'


def _branch():
    try:
        r = subprocess.run(['git', 'branch', '--show-current'],
                           capture_output=True, text=True, timeout=2)
        b = r.stdout.strip()
        return f' ({b})' if b else ''
    except Exception:
        return ''


def _status():
    cwd = os.getcwd()
    home = str(Path.home())
    d = ('~/' + cwd[len(home)+1:]) if cwd.startswith(home + '/') else cwd
    s = f'{d}{_branch()} ↑{_fmtk(TOKEN_IN)} ↓{_fmtk(TOKEN_OUT)}'
    if COST_MODE:
        s += f' ${COST_USD:.3f}'
    s += f' {_ctxp()} ({PROVIDER}) {MODEL}'
    if EFFORT_OK:
        s += f' • {EFFORT}'
    return s

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

ANTHROPIC_TOOLS = [
    {"name": "bash", "description": "Run a shell command",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command"}}, "required": ["command"]}},
    {"name": "read", "description": "Read file contents",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "description": "Start line"}, "limit": {"type": "integer", "description": "Max lines"}}, "required": ["path"]}},
    {"name": "write", "description": "Write content to file, creates dirs",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit", "description": "Edit file with exact text replacement",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "oldText": {"type": "string", "description": "Exact text to find"}, "newText": {"type": "string", "description": "Replacement"}}, "required": ["path", "oldText", "newText"]}},
    {"name": "grep", "description": "Search for pattern in files",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "find", "description": "Find files by name glob",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "name": {"type": "string", "description": "Glob"}}, "required": ["path"]}},
    {"name": "ls", "description": "List directory",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}},
]

OPENAI_TOOLS = [
    {"type": "function", "name": t["name"], "description": t["description"],
     "parameters": t["input_schema"], "strict": False}
    for t in ANTHROPIC_TOOLS
]

# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def think_param() -> dict:
    pm = f"{PROVIDER}:{MODEL}"
    if re.match(r'anthropic:claude-(opus-4-7|opus-4-6|sonnet-4-6|opus-4-5)', pm):
        return {"effort": EFFORT, "thinking": {"type": "adaptive"}}
    thinking = _get('AGENT_THINKING', '')
    if thinking == 'low':
        return {"thinking": {"type": "enabled", "budget_tokens": 1024}}
    elif thinking == 'medium':
        return {"thinking": {"type": "enabled", "budget_tokens": 4096}}
    elif thinking in ('high', 'xhigh', 'max'):
        return {"thinking": {"type": "enabled", "budget_tokens": 10000}}
    return {}


def _adjusted_max_tokens():
    mt = MAX_TOKENS
    eb = _get('AGENT_THINKING', '') or (EFFORT if EFFORT_OK else '')
    if eb in ('minimal', 'low'):
        mt = max(mt, 4096)
    elif eb == 'medium':
        mt = max(mt, 8192)
    elif eb == 'high':
        mt = max(mt, 16000)
    elif eb in ('xhigh', 'max'):
        mt = max(mt, 32000)
    return mt


def call_api(messages: list) -> dict:
    mt = _adjusted_max_tokens()
    extra = think_param()
    headers = {"Content-Type": "application/json", "User-Agent": "pu.py/1.0"}

    if PROVIDER == 'anthropic':
        headers['x-api-key'] = ANTHROPIC_API_KEY
        headers['anthropic-version'] = '2023-06-01'
        body = {
            "model": MODEL,
            "max_tokens": mt,
            "system": SYSTEM,
            "tools": ANTHROPIC_TOOLS,
            "messages": messages,
            **extra,
        }
        url = "https://api.anthropic.com/v1/messages"
    elif PROVIDER == 'opencode':
        headers['Authorization'] = f'Bearer {OPENCODE_API_KEY}'
        if MODEL.startswith('claude-'):
            # Anthropic-compatible endpoint
            headers['x-api-key'] = OPENCODE_API_KEY
            headers['anthropic-version'] = '2023-06-01'
            body = {
                "model": MODEL,
                "max_tokens": mt,
                "system": SYSTEM,
                "tools": ANTHROPIC_TOOLS,
                "messages": messages,
            }
            url = "https://opencode.ai/zen/v1/messages"
        else:
            # OpenAI-compatible endpoint (big-pickle, qwen, etc.)
            # Check if native tools are supported
            if supports_native_tools():
                body = {
                    "model": MODEL,
                    "max_tokens": mt,
                    "messages": [{"role": "system", "content": SYSTEM}] + messages,
                    "tools": OPENAI_TOOLS,
                }
            else:
                # Prompt-based tool calling: add tool definitions to system prompt
                tool_system = SYSTEM + "\n\n" + TOOL_DEFINITIONS
                body = {
                    "model": MODEL,
                    "max_tokens": mt,
                    "messages": [{"role": "system", "content": tool_system}] + messages,
                }
            url = "https://opencode.ai/zen/v1/chat/completions"
    else:
        headers['Authorization'] = f'Bearer {OPENAI_API_KEY}'
        rp = {}
        if EFFORT_OK and EFFORT and EFFORT != 'none':
            rp = {"reasoning": {"effort": EFFORT}}
        body = {
            "model": MODEL,
            "max_output_tokens": mt,
            "instructions": SYSTEM,
            "input": messages,
            "tools": OPENAI_TOOLS,
            **rp,
        }
        url = "https://api.openai.com/v1/responses"

    headers['User-Agent'] = 'pu.py/1.0'
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        try:
            return json.loads(body_text)
        except Exception:
            return {"error": {"message": f"HTTP {e.code}: {body_text[:200]}"}}
    except Exception as e:
        return {"error": {"message": str(e)}}

# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class ParsedResponse:
    def __init__(self):
        self.ty = ''       # 'T' = tool call, 'X' = text final
        self.tn = ''       # tool name
        self.ti = ''       # tool id
        self.tx = ''       # text content
        self.cb = None     # raw content block (anthropic) or output (openai)
        self.tinp = None   # tool input

def extract_json(text: str) -> dict:
    """Extract the first JSON object from text by balancing braces."""
    start = text.find('{')
    if start == -1:
        return {}
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < len(text):
        ch = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == '\\' and in_string:
            escape = True
            i += 1
            continue
        if ch == '"' and not escape:
            in_string = not in_string
        if not in_string:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i+1])
                    except Exception:
                        return {}
        i += 1
    return {}


def parse_response(resp: dict) -> ParsedResponse:
    pr = ParsedResponse()

    # If model doesn't support native tools, try prompt-based tool calling
    if not supports_native_tools():
        # Extract text from response
        text = ''
        if PROVIDER in ('opencode', 'openai'):
            choices = resp.get('choices', [])
            if choices:
                msg = choices[0].get('message', {})
                text = msg.get('content', '') or ''
                # Also check reasoning field
                if not text:
                    text = msg.get('reasoning', '') or ''

        if text:
            # Try to find TOOL: format
            tool_match = re.search(r'TOOL:\s*([\w-]+)', text)
            if tool_match:
                pr.ty = 'T'
                pr.tn = tool_match.group(1).strip()
                # Find PARAM: line and extract JSON
                param_idx = text.find('PARAM:')
                if param_idx != -1:
                    # Find the first '{' after PARAM:
                    brace_idx = text.find('{', param_idx)
                    if brace_idx != -1:
                        pr.tinp = extract_json(text[brace_idx:])
                    else:
                        pr.tinp = {}
                else:
                    pr.tinp = {}
                pr.tx = text  # Save some context
                return pr

            # No tool call found, treat as text
            pr.ty = 'X'
            pr.tx = text
            return pr

    # Determine if we should use Anthropic format (Claude models via opencode)
    use_anthropic_format = (PROVIDER == 'anthropic') or \
                           (PROVIDER == 'opencode' and MODEL.startswith('claude-'))
    if use_anthropic_format:
        content = resp.get('content', [])
        tool_use = next((c for c in content if c.get('type') == 'tool_use'), None)
        text_block = next((c for c in content if c.get('type') == 'text'), None)
        if tool_use:
            pr.ty = 'T'
            pr.tn = tool_use.get('name', '')
            pr.ti = tool_use.get('id', '')
            pr.tinp = tool_use.get('input', {})
            pr.tx = text_block.get('text', '') if text_block else ''
            pr.cb = content
        else:
            pr.ty = 'X'
            pr.tx = text_block.get('text', '') if text_block else ''
    else:
        output = resp.get('output', [])
        pr.cb = output
        fn_call = next((o for o in output if o.get('type') == 'function_call'), None)
        text_out = next((o for o in output if o.get('type') == 'message'), None)
        if fn_call:
            pr.ty = 'T'
            pr.ti = fn_call.get('call_id') or fn_call.get('id', '')
            pr.tn = fn_call.get('name', '')
            raw_args = fn_call.get('arguments', '{}')
            if isinstance(raw_args, str):
                try:
                    pr.tinp = json.loads(raw_args)
                except Exception:
                    pr.tinp = {}
            else:
                pr.tinp = raw_args
            if text_out:
                for item in text_out.get('content', []):
                    if item.get('type') == 'output_text':
                        pr.tx = item.get('text', '')
        else:
            pr.ty = 'X'
            if text_out:
                for item in text_out.get('content', []):
                    if item.get('type') == 'output_text':
                        pr.tx = item.get('text', '')
    return pr

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

PRUNE_DIRS = {'.git', 'node_modules', 'dist', 'build', 'target', '.venv'}


def _safe_path(p: str) -> str:
    if p.startswith('-'):
        return './' + p
    return p


def _resolve_symlink(fp: str):
    if os.path.islink(fp):
        target = os.readlink(fp)
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(fp), target)
        return target
    return fp


def truncate_tool_output(out: str, tool_name: str) -> str:
    M = AGENT_TOOL_TRUNC
    if tool_name == 'read' or len(out) <= M:
        return out
    lines = out.splitlines()
    if len(lines) <= 40:
        return out
    head = lines[:30]
    tail = lines[-10:]
    hidden = len(lines) - 40
    return '\n'.join(head) + f'\n...[{hidden} lines hidden; call read with offset/limit to view a specific range]...\n' + '\n'.join(tail)


def run_tool(tool_name: str, inp: dict) -> str:
    global CHILD_PROC

    if CONFIRM:
        if not sys.stdin.isatty():
            return "[denied: no tty]"
        preview = json.dumps(inp)[:80]
        sys.stderr.write(f'\033[33m[?] {tool_name}: {preview}\033[0m [y/N] ')
        sys.stderr.flush()
        yn = input()
        if yn.lower() != 'y':
            return "[denied]"

    out = ''
    rc = 0

    if tool_name == 'bash':
        cmd = inp.get('command', '')
        _tool_log('bash', cmd)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as tf:
            tf.write(cmd + '\n')
            tfname = tf.name
        try:
            result = subprocess.run(
                [shutil.which('bash') or 'sh', tfname],
                capture_output=True, text=True, timeout=300
            )
            out = result.stdout + result.stderr
            rc = result.returncode
        except subprocess.TimeoutExpired:
            out = '[timeout]'
            rc = 1
        except Exception as e:
            out = str(e)
            rc = 1
        finally:
            os.unlink(tfname)
        if rc != 0:
            out += f'\n[exit:{rc}]'

    elif tool_name == 'read':
        fp = _safe_path(inp.get('path', ''))
        offset = inp.get('offset')
        limit = inp.get('limit')
        _tool_log('read', _p(fp))
        if not os.path.isfile(fp):
            return f'Error: file not found: {fp}'
        if offset == 0:
            offset = None
        sz = os.path.getsize(fp)
        if offset is None and limit is None and sz > AGENT_READ_MAX:
            return f'Error: {fp} is {sz} bytes — pass offset/limit to read a range'
        with open(fp, 'r', errors='replace') as f:
            lines = f.readlines()
        if offset is not None and limit is not None:
            out = ''.join(lines[offset-1:offset-1+limit])
        elif offset is not None:
            out = ''.join(lines[offset-1:])
        elif limit is not None:
            out = ''.join(lines[:limit])
        else:
            out = ''.join(lines)

    elif tool_name == 'write':
        fp = _safe_path(inp.get('path', ''))
        if not fp:
            return 'Error: path is required for write tool'
        content = inp.get('content', '')
        _tool_log('write', _p(fp))
        fp = _resolve_symlink(fp)
        Path(fp).parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode='w', dir=str(Path(fp).parent),
            prefix='.pu.', delete=False, suffix='.tmp'
        ) as tmp:
            tmp.write(content)
            tmpname = tmp.name
        if os.path.exists(fp):
            try:
                mode = os.stat(fp).st_mode & 0o777
                os.chmod(tmpname, mode)
            except Exception:
                pass
        os.replace(tmpname, fp)
        out = f'Wrote to {fp}'

    elif tool_name == 'edit':
        fp = _safe_path(inp.get('path', ''))
        if not fp:
            return 'Error: path is required for edit tool'
        old_text = inp.get('oldText', '')
        new_text = inp.get('newText', '')
        _tool_log('edit', _p(fp))
        fp = _resolve_symlink(fp)
        if not old_text:
            return 'Error: oldText must not be empty'
        if not os.path.isfile(fp):
            return f'Error: file not found: {fp}'
        with open(fp, 'r', errors='replace') as f:
            original = f.read()
        count = original.count(old_text)
        if count == 0:
            return f'Error: oldText not found in {fp}. Read exact surrounding lines before retrying; do not retry the same oldText.'
        if count > 1:
            return f'Error: oldText matched multiple times in {fp}. Use a larger unique oldText block.'
        new_content = original.replace(old_text, new_text, 1)
        mode = os.stat(fp).st_mode & 0o777
        with tempfile.NamedTemporaryFile(
            mode='w', dir=str(Path(fp).parent),
            prefix='.pu.', delete=False, suffix='.tmp'
        ) as tmp:
            tmp.write(new_content)
            tmpname = tmp.name
        os.chmod(tmpname, mode)
        os.replace(tmpname, fp)
        out = f'Edited {fp}'

    elif tool_name == 'grep':
        pattern = inp.get('pattern', '')
        gp = inp.get('path', '.')
        if not gp:
            gp = '.'
        _tool_log('grep', f'{pattern} {_p(gp)}')
        if gp.startswith('-'):
            gp = './' + gp
        try:
            cmd = ['grep', '-rnIE',
                   '--exclude-dir=.git', '--exclude-dir=node_modules',
                   '--exclude-dir=dist', '--exclude-dir=build',
                   '--exclude-dir=target', '--exclude-dir=.venv',
                   '--', pattern, gp]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            out = result.stdout + result.stderr
            if result.returncode == 1 and not out.strip():
                out = 'No matches'
            lines = out.splitlines()
            out = '\n'.join(lines[:100])
        except Exception as e:
            out = f'Error: {e}'

    elif tool_name == 'find':
        fp = inp.get('path', '.')
        if not fp:
            fp = '.'
        fn = inp.get('name', '')
        _tool_log('find', f'{_p(fp)} {fn}')
        if fp.startswith('-'):
            fp = './' + fp
        results = []
        for root, dirs, files in os.walk(fp):
            dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
            rel_root = os.path.relpath(root, fp)
            depth = 0 if rel_root == '.' else rel_root.count(os.sep) + 1
            if not fn:
                if depth < 3:
                    results.append(rel_root if rel_root != '.' else fp)
                    for f in files:
                        results.append(os.path.join('' if rel_root == '.' else rel_root, f))
            else:
                for f in files:
                    if fnmatch.fnmatch(f, fn):
                        results.append(os.path.join(root, f))
        out = '\n'.join(results[:100])

    elif tool_name == 'ls':
        lp = inp.get('path', '.')
        if not lp:
            lp = '.'
        _tool_log('ls', _p(lp))
        if lp.startswith('-'):
            lp = './' + lp
        try:
            result = subprocess.run(['ls', '-la', lp], capture_output=True, text=True)
            out = result.stdout + result.stderr
        except Exception as e:
            out = str(e)

    else:
        return f'Error: unknown tool: {tool_name}'

    return truncate_tool_output(out.rstrip('\n'), tool_name)

# ---------------------------------------------------------------------------
# History persistence
# ---------------------------------------------------------------------------

def save_history():
    if not HISTORY_FILE:
        return
    _mkparent(HISTORY_FILE)
    with open(HISTORY_FILE, 'w') as f:
        json.dump(MSGS, f)
    with open(HISTORY_FILE + '.meta', 'w') as f:
        f.write(f'{PROVIDER}:{MODEL}')


def load_history() -> bool:
    global MSGS
    if not HISTORY_FILE:
        return False
    meta = HISTORY_FILE + '.meta'
    if not (os.path.exists(HISTORY_FILE) and os.path.exists(meta)):
        return False
    with open(meta) as f:
        if f.read().strip() != f'{PROVIDER}:{MODEL}':
            return False
    with open(HISTORY_FILE) as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        return False
    MSGS = data
    return True

# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------

def _ctx_msgs_len():
    return len(json.dumps(MSGS))


def _ctx_tail_start(entries: list, keep_bytes: int) -> int:
    s = 0
    for i in range(len(entries) - 1, 0, -1):
        s += len(json.dumps(entries[i]))
        if s > keep_bytes:
            return i + 1
    return 2


def _ctx_adjust_start(entries: list, c: int) -> int:
    guard = 0
    while c > 1 and guard < 20:
        h = json.dumps(entries[c-1]) if c <= len(entries) else ''
        if any(k in h for k in ['tool_result', 'function_call_output', '"type":"function_call"']):
            c -= 1
        else:
            break
        guard += 1
    if c < 2:
        c = len(entries) - 2
    return c


def _ctx_local_memory(mid_entries: list, focus: str = '') -> str:
    lines = []
    if focus:
        lines.append(f'Focus: {focus}')
    lines.append('Goal:')
    lines.append('User constraints/directives:')
    user_count = 0
    for e in mid_entries:
        s = json.dumps(e)
        if '"role":"user"' in s and user_count < 8:
            content = e.get('content', '')
            if isinstance(content, str) and len(content) > 260:
                content = content[:260] + '...'
            lines.append(f'- {content}')
            user_count += 1
    lines.append('Files read:')
    for e in mid_entries:
        s = json.dumps(e)
        for m in re.finditer(r'"path":"([^"]+)"', s):
            lines.append(f'- {m.group(1)}')
    lines.append('Files changed:')
    lines.append('Commands run:')
    for e in mid_entries:
        s = json.dumps(e)
        for m in re.finditer(r'"command":"([^"]+)"', s):
            lines.append(f'- {m.group(1)}')
    lines.append('Errors/failures:')
    for e in mid_entries:
        s = json.dumps(e)
        if any(k in s for k in ['Error:', '[exit:', '[denied]', 'failed', 'Failed']):
            lines.append(f'- (error in step)')
    lines.append('Decisions made:\n- See retained recent transcript for latest decisions.')
    lines.append('Important snippets/facts:\n- Older bulky tool output may have been omitted; re-read files from disk as needed.')
    lines.append('Open TODOs / next steps:\n- Continue from the latest retained user request and recent transcript.')
    return '\n'.join(lines[:120])


def trim_context(msgs: list, focus: str = '') -> list:
    global MSGS
    cap = CTX_LIMIT - AGENT_RESERVE
    current_len = len(json.dumps(msgs))
    if not focus and current_len <= cap:
        return msgs
    info(f'Compacting ({current_len}b > {cap}b){(" focus: " + focus) if focus else ""}')
    if len(msgs) < 6:
        return msgs if current_len <= cap else [{"role": "user", "content": f"[Earlier context was compacted. Continue from the latest user request.]"}]

    kb = AGENT_KEEP_RECENT
    half = cap // 2
    if kb > half:
        kb = half
    if kb < 2000:
        kb = 2000

    first = msgs[0]
    c = _ctx_tail_start(msgs, kb)
    c = _ctx_adjust_start(msgs, c)

    mid = msgs[1:c-1]
    mid_text = '\n'.join(json.dumps(e)[:4000] for e in mid[-160:])

    prompt = (
        f"{'Focus: ' + focus + '. ' if focus else ''}"
        f"Summarize the earlier transcript into this exact compact memory card. Be concise. "
        f"Preserve actionable coding-agent state: user intent, constraints, files touched, edits, errors, decisions, and next steps. "
        f"Prefer paths/ranges over copied bulk. Do not call tools.\n\n"
        f"Goal:\nUser constraints/directives:\nFiles read:\nFiles changed:\nCommands run:\n"
        f"Errors/failures:\nDecisions made:\nImportant snippets/facts:\nOpen TODOs / next steps:\n\n"
        f"Transcript/facts:\n{mid_text}"
    )
    try:
        sum_resp = call_api([{"role": "user", "content": prompt}])
        pr = parse_response(sum_resp)
        summary_text = pr.tx if pr.tx else None
    except Exception:
        summary_text = None

    mode = 'normal'
    if not summary_text:
        err("Summarization failed; using local compaction memory")
        summary_text = _ctx_local_memory(mid, focus)
        mode = 'local'

    label = '[Earlier compacted locally:\n' if mode == 'local' else '[Earlier compacted memory:\n'
    summary_msg = {"role": "user", "content": f"{label}{summary_text}]"}

    while True:
        c = _ctx_tail_start(msgs, kb)
        c = _ctx_adjust_start(msgs, c)
        tail = msgs[c-1:]
        new_msgs = [first, summary_msg] + tail
        new_len = len(json.dumps(new_msgs))
        log_event(0, 'compact', f'old={current_len} new={new_len} cap={cap} mode={mode} tail={kb}')
        if new_len <= cap:
            return new_msgs
        if kb <= 2000:
            break
        kb //= 2
        if kb < 2000:
            kb = 2000

    # Fallback: drop tail entirely
    new_msgs = [first, summary_msg]
    new_len = len(json.dumps(new_msgs))
    if new_len <= cap:
        log_event(0, 'compact', f'old={current_len} new={new_len} cap={cap} mode={mode}-no-tail')
        return new_msgs

    # Last resort
    last_msg = {"role": "user", "content": f"[Earlier context was compacted.{' Focus: ' + focus + '.' if focus else ''} Continue from the latest user request.]"}
    return [last_msg]

# ---------------------------------------------------------------------------
# Context files (AGENTS.md / CLAUDE.md)
# ---------------------------------------------------------------------------

def load_context():
    global SYSTEM
    ctx_parts = []
    d = Path.cwd()
    while d != d.parent:
        for fname in ('AGENTS.md', 'CLAUDE.md'):
            f = d / fname
            if f.exists():
                ctx_parts.append(f.read_text())
        d = d.parent
    agents_pi = Path.home() / '.pi' / 'agent' / 'AGENTS.md'
    if agents_pi.exists():
        ctx_parts.insert(0, agents_pi.read_text())
    if ctx_parts:
        info("Loaded context files")
        SYSTEM = SYSTEM + '\n' + '\n'.join(ctx_parts)

# ---------------------------------------------------------------------------
# Interrupt handling
# ---------------------------------------------------------------------------

def _interrupt_handler(sig, frame):
    global STATE, CHILD_PROC
    spin_stop()
    if STATE == 'busy' and CHILD_PROC:
        try:
            CHILD_PROC.terminate()
            time.sleep(0.2)
            CHILD_PROC.kill()
        except Exception:
            pass
        CHILD_PROC = None
        STATE = 'idle'
    else:
        sys.exit(130)

signal.signal(signal.SIGINT, _interrupt_handler)

# ---------------------------------------------------------------------------
# Key / setup
# ---------------------------------------------------------------------------

def have_key() -> bool:
    if PROVIDER == 'anthropic':
        return bool(ANTHROPIC_API_KEY)
    elif PROVIDER == 'openai':
        return bool(OPENAI_API_KEY)
    elif PROVIDER == 'opencode':
        return True  # public key, always available
    return False


def setup():
    global PROVIDER, MODEL, EFFORT, ANTHROPIC_API_KEY, OPENAI_API_KEY, EFFORT_OK

    def safe_input(prompt):
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return None

    print("\nWelcome to pu.py.\n\nProvider:\n 1) Anthropic (Claude)\n 2) OpenAI (GPT)\n 3) OpenCode (big-pickle)", file=sys.stderr)
    p = safe_input('> ')
    if p is None:
        sys.exit(0)
    if p in ('2', 'openai', 'OpenAI'):
        PROVIDER = 'openai'
        km = 'OPENAI_API_KEY'
        url = 'https://platform.openai.com/api-keys'
        dm = 'gpt-5.5'
    elif p in ('3', 'opencode', 'OpenCode'):
        PROVIDER = 'opencode'
        km = None
        url = 'https://opencode.ai'
        dm = 'big-pickle'
    else:
        PROVIDER = 'anthropic'
        km = 'ANTHROPIC_API_KEY'
        url = 'https://console.anthropic.com/settings/keys'
        dm = 'claude-opus-4-7'

    try:
        import subprocess as sp
        sp.run(['open', url], check=False, capture_output=True)
    except Exception:
        pass

    if km:
        print(f'Get a key at {url}', file=sys.stderr)
        import getpass
        k = getpass.getpass('Paste API key (hidden): ')
        k = clean_key(k)
        if not k:
            err("No key entered")
            sys.exit(1)
    else:
        k = 'public'
        info(f'OpenCode uses a public API key — no key needed.')

    m = input(f'Model [{dm}]: ').strip() or dm
    MODEL = m
    _set_effort_ok()

    e = input('Effort [medium]: ').strip() or 'medium'
    short = {'n': 'none', 'min': 'minimal', 'l': 'low', 'm': 'medium', 'h': 'high', 'x': 'xhigh', 'xh': 'xhigh'}
    EFFORT = short.get(e, e)

    if km:
        os.environ[km] = k
    os.environ['AGENT_PROVIDER'] = PROVIDER
    os.environ['AGENT_MODEL'] = MODEL
    os.environ['AGENT_EFFORT'] = EFFORT
    if PROVIDER == 'anthropic':
        ANTHROPIC_API_KEY = k
    elif PROVIDER == 'openai':
        OPENAI_API_KEY = k
    # opencode: key is always 'public', no env var needed

    s = input('Save to ~/.pu.env so next time is automatic? [Y/n] ').strip()
    if s.lower() not in ('n', 'no'):
        env_file = Path.home() / '.pu.env'
        env_file.parent.mkdir(exist_ok=True)
        with open(env_file, 'w') as f:
            if km:
                f.write(f"{km}='{k}'\n")
            f.write(f"AGENT_PROVIDER='{PROVIDER}'\nAGENT_MODEL='{MODEL}'\nAGENT_EFFORT='{EFFORT}'\n")
        env_file.chmod(0o600)
        info("Saved ~/.pu.env")


def _tpl(name: str) -> str:
    """Load prompt template from .pi/prompts/ or ~/.pi/agent/prompts/"""
    for d in (Path('.pi/prompts'), Path.home() / '.pi' / 'agent' / 'prompts'):
        fp = d / f'{name}.md'
        if fp.exists():
            return fp.read_text()
    return name


def _skill(name: str):
    """Load skill from .pi/skills/, .agents/skills/, ~/.pi/agent/skills/, or ~/.agents/skills/"""
    global SYSTEM
    for d in (Path('.pi/skills'), Path('.agents/skills'),
             Path.home() / '.pi' / 'agent' / 'skills',
             Path.home() / '.agents' / 'skills'):
        for skill_file in ('SKILL.md', 'skill.md'):
            fp = d / name / skill_file
            if fp.exists():
                info(f"Loaded skill: {name}")
                SYSTEM = SYSTEM + '\n' + fp.read_text()
                return
    err(f"Skill not found: {name}")


def ensure_key() -> bool:
    if have_key():
        return True
    if sys.stdin.isatty():
        setup()
        return have_key()
    err("No API key. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or use AGENT_PROVIDER=opencode")
    return False

# ---------------------------------------------------------------------------
# Main task runner
# ---------------------------------------------------------------------------

def run_task(task: str) -> int:
    global STATE, MSGS

    # Shell passthrough
    if task.startswith('!'):
        cmd = task[1:]
        result = subprocess.run(cmd, shell=True)
        return result.returncode

    if not ensure_key():
        return 1

    if not MSGS:
        load_history()

    MSGS.append({"role": "user", "content": task})
    log_event(0, 'start', task)

    step = 0
    empty_final = False
    ctx_retry = False

    while step < MAX_STEPS:
        step += 1
        MSGS = trim_context(MSGS)
        STATE = 'busy'

        # API call with retry
        resp = None
        for retry in range(3):
            spin_start(_status())
            resp = call_api(MSGS)
            spin_stop()

            if STATE == 'idle':
                err("[interrupted]")
                return 130

            if 'error' in resp and resp['error']:
                em = ''
                if isinstance(resp['error'], dict):
                    em = resp['error'].get('message', '')
                else:
                    em = str(resp['error'])
                em_lower = em.lower()
                if any(k in em_lower for k in ['incorrect api key', 'invalid api key', 'unauthorized', 'authentication']):
                    err(f"API: {em}")
                    return 1
                if any(k in em_lower for k in ['invalid body', 'parse json']):
                    err(f"API: {em}")
                    return 1
                if any(k in em_lower for k in ['model not found', 'model does not exist', 'access model']):
                    err(f"API: {em}")
                    return 1
                if any(k in em_lower for k in ['context', 'token limit', 'too large']):
                    if not ctx_retry:
                        ctx_retry = True
                        err("Context full; compacting and retrying")
                        MSGS = trim_context(MSGS, "recover from context overflow")
                        continue
                err(f"API error: {em}")
                if retry < 2:
                    time.sleep((retry + 1) * 3)
                    continue
                return 1
            break

        if resp is None:
            err("API failed")
            return 1

        track_tokens(resp)
        pr = parse_response(resp)

        if pr.ty == 'T' and pr.tn:
            if pr.tx:
                _say(pr.tx)

            # Check if this is a prompt-based tool call (no cb)
            if pr.cb is None and not supports_native_tools():
                # Prompt-based tool call
                log_event(step, 'tool_call', f'{pr.tn}: {json.dumps(pr.tinp)[:200]}')
                tout = run_tool(pr.tn, pr.tinp or {})

                if STATE == 'idle':
                    err("[interrupted]")
                    return 130

                log_event(step, 'tool_result', tout)
                if tout.startswith('Error:') or tout.startswith('[exit:') or tout.startswith('[denied'):
                    if not PIPE_MODE:
                        err(tout)

                # Append assistant message and tool result
                MSGS.append({"role": "assistant", "content": pr.tx or f"TOOL: {pr.tn}"})
                MSGS.append({"role": "user", "content": f"Tool result: {tout}"})
                save_history()
                continue

            # Determine if we should use Anthropic format (Claude models via opencode)
            use_anthropic_format = (PROVIDER == 'anthropic') or \
                                   (PROVIDER == 'opencode' and MODEL.startswith('claude-'))

            if use_anthropic_format:
                tool_calls = [c for c in (pr.cb or []) if c.get('type') == 'tool_use']
            else:
                tool_calls = [c for c in (pr.cb or []) if c.get('type') == 'function_call']

            if not tool_calls:
                err("No valid tool calls parsed")
                log_event(step, 'error', 'No valid tool calls parsed')
                return 1

            tool_results_anthropic = []
            tool_results_openai = []

            for tc in tool_calls:
                if use_anthropic_format:
                    tn = tc.get('name', '')
                    ti = tc.get('id', '')
                    tinp = tc.get('input', {})
                else:
                    tn = tc.get('name', '')
                    ti = tc.get('call_id') or tc.get('id', '')
                    raw = tc.get('arguments', '{}')
                    if isinstance(raw, str):
                        try:
                            tinp = json.loads(raw)
                        except Exception:
                            tinp = {}
                    else:
                        tinp = raw

                if not ti or not tn:
                    log_event(step, 'error', f'Bad tool call: {tc}')
                    continue

                log_event(step, 'tool_call', f'{tn}: {json.dumps(tinp)[:200]}')
                tout = run_tool(tn, tinp)

                if STATE == 'idle':
                    err("[interrupted]")
                    return 130

                log_event(step, 'tool_result', tout)
                if tout.startswith('Error:') or tout.startswith('[exit:') or tout.startswith('[denied'):
                    if not PIPE_MODE:
                        err(tout)

                tool_results_anthropic.append({
                    "type": "tool_result",
                    "tool_use_id": ti,
                    "content": tout,
                })
                tool_results_openai.append({
                    "type": "function_call_output",
                    "call_id": ti,
                    "output": tout,
                })

            if not tool_results_anthropic:
                err("No tool results produced")
                return 1

            if use_anthropic_format:
                MSGS.append({"role": "assistant", "content": pr.cb})
                MSGS.append({"role": "user", "content": tool_results_anthropic})
            else:
                # OpenAI: append all output items (reasoning + function_call) then results
                for item in (pr.cb or []):
                    MSGS.append(item)
                for item in tool_results_openai:
                    MSGS.append(item)

            save_history()

        elif pr.ty == 'X':
            if not pr.tx:
                if not empty_final:
                    empty_final = True
                    MSGS.append({"role": "user", "content": "Please summarize your findings and next steps."})
                    continue
                err("Empty final response")
                return 1

            if not PIPE_MODE and INTERACTIVE == 1:
                _say(pr.tx)
            else:
                print(pr.tx)

            log_event(step, 'response', pr.tx)
            MSGS.append({"role": "assistant", "content": pr.tx})
            info(f'done · {_status()}')
            save_history()
            return 0
        else:
            err("Parse failed")
            log_event(step, 'error', 'Parse fail')
            return 1

    err(f"Max steps ({MAX_STEPS})")
    info(f'stopped · {_status()}')
    log_event(step, 'max_steps', 'Limit')
    return 1

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def handle_cmd(cmd: str) -> bool:
    global PROVIDER, MODEL, EFFORT, EFFORT_OK, MSGS, CTX_LIMIT

    if cmd.startswith('/model'):
        nm = cmd[6:].strip()
        if nm:
            if re.match(r'^(gpt-|o1|o3|o4)', nm):
                PROVIDER = 'openai'
            elif nm.startswith('claude-'):
                PROVIDER = 'anthropic'
            elif nm.startswith('big-pickle'):
                PROVIDER = 'opencode'
            MODEL = nm
            _set_effort_ok()
            info(f'Model: {MODEL} ({PROVIDER})')
        else:
            info(f'Current: {MODEL} ({PROVIDER})')
        return True

    if cmd.startswith('/effort'):
        ef = cmd[7:].strip()
        if ef:
            short = {'n': 'none', 'min': 'minimal', 'l': 'low', 'm': 'medium', 'h': 'high', 'x': 'xhigh', 'xh': 'xhigh'}
            EFFORT = short.get(ef, ef)
        info(f'Effort: {EFFORT}')
        return True

    if cmd == '/flush':
        MSGS = []
        if HISTORY_FILE:
            with open(HISTORY_FILE, 'w') as f:
                json.dump([], f)
        info("Flushed conversation memory")
        return True

    if cmd in ('/quit', '/exit'):
        sys.exit(0)

    if cmd == '/login':
        setup()
        return True

    if cmd == '/logout':
        env_file = Path.home() / '.pu.env'
        if env_file.exists():
            env_file.unlink()
            info("Removed ~/.pu.env")
        else:
            info("No ~/.pu.env to remove")
        os.environ.pop('ANTHROPIC_API_KEY', None)
        os.environ.pop('OPENAI_API_KEY', None)
        os.environ.pop('OPENCODE_API_KEY', None)
        info("Logged out.")
        return True

    if cmd.startswith('/compact'):
        focus = cmd[8:].strip()
        MSGS = trim_context(MSGS, focus)
        save_history()
        info(f'Compacted ({len(json.dumps(MSGS))}b)')
        return True

    if cmd.startswith('/export'):
        out_file = cmd[7:].strip() or 'session.md'
        with open(out_file, 'w') as f:
            f.write('# Session Export\n\n')
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE) as lf:
                    for line in lf:
                        try:
                            e = json.loads(line)
                            t, c = e.get('t', ''), e.get('c', '')
                            if t == 'start':
                                f.write(f'## Task\n{c}\n\n')
                            elif t == 'tool_call':
                                f.write(f'### Tool: {c}\n')
                            elif t == 'tool_result':
                                f.write(f'```\n{c}\n```\n\n')
                            elif t == 'response':
                                f.write(f'## Response\n{c}\n\n')
                        except Exception:
                            pass
        info(f'Exported to {out_file}')
        return True

    if cmd == '/session':
        info(f'Log: {LOG_FILE} | Model: {MODEL} ({PROVIDER}) | Max steps: {MAX_STEPS}')
        return True

    if cmd.startswith('/skill:'):
        skill_name = cmd[7:].strip()
        _skill(skill_name)
        return True

    if cmd.startswith('/'):
        # Check for template
        cn = cmd[1:].split()[0]
        tp = _tpl(cn)
        if tp != cn:
            info(f'Template: {cn}')
            run_task(tp)
            return True
        err(f'Unknown command: {cmd}')
        return True

    return False

# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def _replay():
    if INTERACTIVE != 1 or not os.path.exists(LOG_FILE):
        return
    info(f'Last messages from {LOG_FILE}:')
    lines = []
    with open(LOG_FILE) as f:
        for line in f:
            lines.append(line)
    # show last session
    session = []
    for line in lines[-200:]:
        try:
            e = json.loads(line)
            t, c = e.get('t', ''), e.get('c', '')
            if t == 'start':
                session = [(t, c)]
            else:
                session.append((t, c))
        except Exception:
            pass
    for t, c in session:
        if t == 'start':
            sys.stderr.write(f'\033[36m>\033[0m {c}\n')
        elif t == 'response':
            sys.stderr.write(f'{c}\n')
        elif t == 'tool_call':
            sys.stderr.write(f'⏺ {c}\n')
        elif t in ('error', 'max_steps'):
            sys.stderr.write(f'\033[31m[!] {c}\033[0m\n')
    sys.stderr.flush()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global PIPE_MODE, COST_MODE, INTERACTIVE, MSGS

    parser = argparse.ArgumentParser(description='pu.py — portable agentic harness', add_help=False)
    parser.add_argument('-h', '--help', action='store_true')
    parser.add_argument('-v', '--version', action='store_true')
    parser.add_argument('--pipe', '-p', action='store_true')
    parser.add_argument('--cost', action='store_true')
    parser.add_argument('-i', action='store_true')
    parser.add_argument('-n', '--no-interactive', action='store_true')
    parser.add_argument('task', nargs='*')
    args = parser.parse_args()

    if args.help:
        print('pu.py — portable agentic harness (Python port of pu.sh)')
        print('Usage: ./pu.py "task" | ./pu.py (interactive) | --pipe | --cost | -v')
        print('Env: ANTHROPIC_API_KEY OPENAI_API_KEY AGENT_MODEL AGENT_PROVIDER AGENT_SYSTEM AGENT_MAX_STEPS AGENT_MAX_TOKENS AGENT_LOG AGENT_CONFIRM AGENT_VERBOSE AGENT_CONTEXT_LIMIT AGENT_RESERVE AGENT_TOOL_TRUNC AGENT_READ_MAX AGENT_HISTORY AGENT_THINKING/AGENT_EFFORT AGENT_PRICE_* ~/.pu.env')
        sys.exit(0)

    if args.version:
        print('pu.py 1.0.0')
        sys.exit(0)

    PIPE_MODE = args.pipe
    COST_MODE = args.cost

    if args.i:
        INTERACTIVE = 1
    if args.no_interactive:
        INTERACTIVE = -1

    # Determine task
    task = ''
    if PIPE_MODE and not sys.stdin.isatty():
        stdin_data = sys.stdin.read()
        extra = ' '.join(args.task)
        task = f'{stdin_data}\n{extra}' if (stdin_data and extra) else (stdin_data or extra)
    elif args.task:
        task = ' '.join(args.task)
    elif not sys.stdin.isatty():
        task = sys.stdin.read()

    if not task and sys.stdin.isatty() and INTERACTIVE != -1:
        INTERACTIVE = 1

    if not task and INTERACTIVE != 1:
        err('No task. Usage: ./pu.py "task" or ./pu.py -i')
        sys.exit(1)

    if not ensure_key():
        sys.exit(1)

    load_context()

    if not MSGS:
        if load_history():
            _replay()
            info(f'Resumed memory: {HISTORY_FILE} (/flush to clear)')

    if task:
        info(task)
        info(f'{MODEL} ({PROVIDER}) max steps: {MAX_STEPS}')
        rc = run_task(task)
        if INTERACTIVE != 1:
            sys.exit(rc)

    # Interactive loop
    info(f'{MODEL} ({PROVIDER}) | /model /effort /login /logout /flush /compact /export /session /skill:name /quit | /template | !cmd')
    while True:
        STATE = 'idle'
        try:
            sys.stderr.write('\033[36m> \033[0m')
            sys.stderr.flush()
            line = input()
        except (EOFError, KeyboardInterrupt):
            break
        line = line.strip()
        if not line:
            continue
        if line in ('quit', 'exit', 'q'):
            break
        if handle_cmd(line):
            continue
        run_task(line)


if __name__ == '__main__':
    main()
