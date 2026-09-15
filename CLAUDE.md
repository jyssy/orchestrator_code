@AGENTS.md

# Claude Code-specific guidance

- Use `claude-mcp.json` as this repository's orchestrator MCP registration when
  the guarded workflow requires orchestrator tools.
- Use plan mode for read-only planning. Enter an edit-capable session only through
  the separately approved execution lifecycle described in the imported guidance.
- Do not configure broad persistent `additionalDirectories`. The guarded CLI may
  provide approval-bound context repositories for a single invocation; its deny
  rules and sandbox configuration make those repositories read-only.
- Treat visibility of another repository as context only, never as authorization
  to edit it or broaden the approved target-repository scope.
