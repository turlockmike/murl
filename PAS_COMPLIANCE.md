# POSIX Agent Standard (PAS) Compliance Analysis

## Current Status: Level 2 (Agent-Optimized) ✅

murl currently implements **Level 2: Agent-Optimized** compliance with the POSIX Agent Standard.

## Level 2 Compliance Features

### ✅ Level 1 Requirements (Agent-Safe)
- **`--agent` flag**: Global mode switch for agent-compatible behavior
- **Non-interactive execution**: No prompts or user input when `--agent` is active
- **Structured errors**: JSON error objects to stderr with error codes
- **Output purity**: Success data to stdout, errors to stderr
- **State suppression**: No progress bars, spinners, or ANSI colors in agent mode

### ✅ Level 2 Requirements (Agent-Optimized)
- **JSON Lines (NDJSON) output**: List operations output one JSON object per line in agent mode
- **Agent-optimized help**: `--agent --help` provides concise usage information
- **Semantic exit codes**:
  - `0` = Success
  - `1` = General error (connection, timeout, server error)
  - `2` = Invalid arguments (malformed URL, invalid data format)
  - `100` = MCP server error (in JSON `code` field, not exit code)
- **Compact JSON**: Agent mode outputs compact JSON without indentation

## Level 3 Analysis: Navigation Contract

### What Level 3 Requires

Level 3 is designed for tools that expose **external datasets** (APIs, databases, cloud storage) with Unix-style navigation commands:
- `ls` - List children of a path
- `cat` - Read content of a resource
- `stat` - Get metadata about a resource

### murl's Current Model

murl is a **client tool** for MCP (Model Context Protocol) servers, not a tool that directly manages external datasets. It translates HTTP requests to MCP JSON-RPC calls:

| MCP Endpoint | Purpose | Maps to |
|--------------|---------|---------|
| `/tools` | List available tools | Similar to `ls` |
| `/tools/<name>` | Call a specific tool | Similar to command execution |
| `/resources` | List resources | Similar to `ls` |
| `/resources/<path>` | Read a resource | Similar to `cat` |
| `/prompts` | List prompts | Similar to `ls` |
| `/prompts/<name>` | Get a prompt | Similar to template retrieval |

### Level 3 Applicability Assessment

**Does murl need Level 3 compliance?**

The answer depends on perspective:

1. **As an MCP client (current model)**: ❌ **No**
   - murl is a curl-like tool for MCP servers
   - It doesn't directly manage hierarchical datasets
   - MCP protocol already defines resource access patterns
   - Adding Unix-style commands would duplicate MCP functionality

2. **As a generic remote resource tool**: ⚠️ **Possibly**
   - If murl were repositioned as a general remote resource browser
   - If it supported non-MCP protocols (HTTP, S3, databases)
   - If it provided filesystem-like abstraction over MCP servers

### Potential Level 3 Implementation

If Level 3 were to be implemented, it would add subcommands:

```bash
# Level 3 navigation commands
murl ls <server-url> <path>        # List resources at path
murl cat <server-url> <path>       # Read resource content
murl stat <server-url> <path>      # Get resource metadata

# Examples
murl ls http://localhost:3000 /tools
murl cat http://localhost:3000 /tools/weather
murl stat http://localhost:3000 /resources/config.json
```

This would provide a Unix-like interface that maps to MCP operations:
- `ls /tools` → `tools/list`
- `cat /tools/echo` → `tools/call` with tool name
- `ls /resources` → `resources/list`
- `cat /resources/file` → `resources/read`
- `stat /tools/tool` → Get tool schema/description

**Challenges:**
1. MCP tools are *callable* (verbs) not *files* (nouns)
2. Tool calls require arguments, which don't fit pure `cat` semantics
3. Would need to maintain two UX paradigms (URL-based and command-based)
4. Limited value-add over existing URL interface

## Level 4 Analysis: State Contract

### What Level 4 Requires

Level 4 extends Level 3 with write operations and synchronization:
- `cp` - Write/upload resources
- `rm` - Delete resources
- `sync` - One-way synchronization from remote to local
- `mount` - FUSE filesystem virtualization (optional)
- `feedback` - Report tool issues

### MCP Protocol Limitations

The MCP protocol (as of v1.0) is primarily **read-only**:
- ✅ List tools, resources, prompts
- ✅ Call tools (which may have side effects)
- ✅ Read resources
- ❌ No standard write/update resource methods
- ❌ No delete operations
- ❌ No bidirectional sync protocol

### Level 4 Applicability

**Can murl implement Level 4?** ❌ **Not without MCP protocol extensions**

The MCP specification does not define write operations for resources. While tools can be called (and may perform writes internally), there's no standard way to:
- Upload a resource to an MCP server
- Delete a resource from an MCP server
- Synchronize local and remote resources

**What would be needed:**
1. MCP protocol extension for resource mutations
2. Server-side support for write operations
3. Conflict resolution for sync operations
4. FUSE driver for mount functionality

### Feedback Command

The `feedback` command is more feasible:

```bash
murl feedback <server-url> <path> --level <error|warn|info> --message "text"
```

This could:
- Report MCP server errors
- Log issues with tool responses
- Submit diagnostic information
- Create GitHub issues for murl bugs

## Recommendations

### Short-term (Current Release)

1. ✅ **Maintain Level 2 compliance** - This is appropriate for murl's purpose
2. 📝 **Document PAS compliance** - Add this analysis to repository
3. 📝 **Update README** - Clarify Level 2 status and why Level 3/4 don't apply

### Medium-term (If Level 3 is desired)

1. **Add subcommand interface**:
   ```bash
   murl ls <url> <path>
   murl cat <url> <path>
   murl stat <url> <path>
   ```
2. **Keep URL interface** for backward compatibility
3. **Document trade-offs** between navigation and curl-like interfaces
4. **Create integration tests** for Level 3 compliance

### Long-term (Level 4 consideration)

1. **Advocate for MCP protocol extensions** to support write operations
2. **Implement feedback command** as it's protocol-independent
3. **Consider mount command** if MCP supports bidirectional sync
4. **Evaluate creating an MCP filesystem proxy** separate from murl

## Conclusion

**murl is Level 2 compliant and should remain so** unless there's a compelling use case for repositioning it as a general remote resource navigation tool rather than an MCP client.

The PAS Level 3/4 requirements are designed for tools that directly manage hierarchical datasets. murl is a protocol client (like curl) rather than a resource manager (like s3cmd or rclone).

If Level 3 compliance is desired for discoverability or agent compatibility, it should be implemented as an **additive interface** (subcommands) rather than replacing the existing URL-based approach.

## Testing Level 3/4 Compliance

If Level 3/4 features are added, use the PAS conformance test suite:
- Repository: `github.com/posix-agent-standard/tests`
- Run: `pas-test murl --level 3`

For self-validation:
```bash
murl --validate-pas  # Proposed command to check compliance
```

## References

- [POSIX Agent Standard Specification](https://github.com/turlockmike/posix-agent-standard/blob/master/SPECIFICATION.md)
- [MCP Protocol Specification](https://modelcontextprotocol.io/specification/)
- murl README (Level 2 compliance)
