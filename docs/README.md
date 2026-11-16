# doomscroller documentation

This directory is the technical reference for the codebase. The README is the tour and the
quick start; these documents go one level deeper and are meant to stay accurate as the
code changes.

| Document | Read it when you need |
|---|---|
| [architecture.md](architecture.md) | how the processes and modules fit together, the design invariants, how data flows |
| [data-model.md](data-model.md) | the nine tables, their columns, indexes, statuses and invariants |
| [cli.md](cli.md) | every command and flag, with examples and exit codes |
| [mcp.md](mcp.md) | the MCP tools, their inputs/outputs, transports and client setup |
| [scoring.md](scoring.md) | the rubric, prompt assembly, the scoring loop, channel trajectory, calibration |
| [development.md](development.md) | setup, tests, Docker, packaging, schema changes, troubleshooting |

Start elsewhere:

* [README](../README.md) — what the project is, quick start, per-client MCP config,
  the current state and roadmap.
* [AGENTS.md](../AGENTS.md) — operational notes for agents working on the owner's host
  (machine-specific; not part of the general documentation).
* [CONTRIBUTING.md](../CONTRIBUTING.md) — contribution rules.
* [scoring/](../scoring) — the rubric itself, as editable markdown.

## Diagram

```
Telegram ──MTProto──▶ poller/watcher ──▶ PostgreSQL ◀── scorer ──▶ LLM (NanoGPT/OpenAI-compatible)
                                          │
                                   MCP server (stdio/HTTP)
                                          │
                                   agent / CLI / webhooks
```

## Keeping these docs honest

The code is the source of truth. When you change a table, a CLI flag, an MCP tool or a
`SCORER_*` variable:

1. update the matching document in this directory,
2. re-check the cross-links (commands, tables and env vars are referenced from several
   places),
3. if the change is behaviour a test can pin, add or adjust the test first.

The documents deliberately avoid pasting large code blocks: they point at modules and
functions instead, so a refactor does not turn the documentation into fiction.
