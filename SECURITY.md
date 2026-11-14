# Security Policy

## Reporting

Please report vulnerabilities privately via GitHub's **Security → Report a
vulnerability** tab, or by email to the maintainer. Do not open a public issue.

## In scope

- The MCP server's tool surface (especially `send_message` and its allowlist)
- Any path where archived Telegram content could be turned into instructions
- Credential handling (Telegram session files, API keys, `DATABASE_URL`)

## Design notes

- `send_message` acts as the **real user account**, not a bot, and is refused
  unless the target chat is in `SEND_ALLOWLIST`. Read tools need no allowlist.
- Archived messages are **untrusted input**. They are data to analyse, never
  instructions to follow.
- Secrets (`TELEGRAM_API_HASH`, session files, `.env`) must never be committed.
