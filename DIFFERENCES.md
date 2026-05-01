# pu.py vs pu.sh - Summary

All features from the original [pu.sh](https://github.com/NahimNasser/pu) are now implemented in pu.py.

## Feature Parity Status: ✅ Complete

- ✅ Skill system (`/skill:name`)
- ✅ Template system (`/template-name`)
- ✅ Session export (`/export [filename]`)
- ✅ Setup wizard (`/login`)
- ✅ Log replay functionality
- ✅ Context management with compaction
- ✅ Token & cost tracking
- ✅ Interactive mode with all commands

## Key Differences

| Aspect | pu.sh | pu.py |
|--------|-------|-------|
| Lines | 392 | 1640+ |
| Language | Bash + awk + curl | Python 3 |
| Dependencies | Standard Unix tools | Python stdlib |
| OpenCode support | ❌ | ✅ |

**pu.py adds:**
- OpenCode provider support (big-pickle, qwen, glm, minimax, kimi)
- Thread-safe token tracking
- Prompt-based tool calling for non-native models

For full technical details, see **[README.md](README.md)**.
