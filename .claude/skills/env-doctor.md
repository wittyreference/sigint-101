---
name: env-doctor
description: Diagnose shell and .env credential conflicts. Use when hitting auth errors, credential failures after switching projects, or mysterious environment variable issues.
---

# Environment Doctor

Diagnose shell vs `.env` conflicts that cause mysterious auth failures. Run this when:
- A new user clones the repo and gets credential errors
- API calls fail after switching between projects or accounts
- Env vars seem to have wrong values despite correct `.env` file
- MCP tools return auth failures but CLI works (or vice versa)

## Usage

Run the diagnostic script:

```bash
./scripts/env-doctor.sh
```

## Configuration

The env-doctor reads from `ff.config.json`:

```json
{
  "envDoctor": {
    "criticalVars": ["API_KEY", "API_SECRET", "DATABASE_URL"],
    "dangerousVars": ["AWS_REGION", "NODE_ENV"]
  }
}
```

- **criticalVars**: Checked for shell-vs-.env mismatches. A mismatch means the shell value will silently win over your `.env` in Node.js processes (dotenv default behavior).
- **dangerousVars**: Checked for orphaned shell values — set in your shell but absent from `.env`. These can cause silent routing or config issues.

## What It Checks

| Check | What It Detects |
|-------|-----------------|
| **1. Project .env** | Missing `.env` file |
| **2. Credential conflicts** | Shell env var differs from `.env` value for critical vars |
| **3. Dangerous inherited vars** | Vars set in shell but absent from `.env` that cause silent failures |
| **4. Environment isolation** | Whether `direnv` is installed and `.envrc` is allowed |

## Common Scenarios

### New user gets auth errors after cloning
They have credentials from a previous project in their shell. The `.env` has different credentials. Shell wins → wrong account.

**Fix:** `unset VAR_NAME` or install direnv with the provided `.envrc` template.

### dotenv doesn't override shell vars
`require('dotenv').config()` skips vars already in `process.env`. Use `{ override: true }` to make `.env` always win.

### MCP tools fail but CLI works
The MCP server inherits env at launch. If shell had stale vars when Claude Code started, MCP uses those. CLI may read `.env` fresh.

**Fix:** Restart Claude Code entirely after fixing env vars.

## Exit Codes

- `0` — Clean (or warnings only)
- `1` — Conflicts detected that will cause failures
