---
name: context-refresh
description: Assemble a fresh, revision-bound evidence packet for a code symbol or issue.
minimum_tier: deterministic
---
# Context refresh
Check tenant and issue permissions before retrieval. Read only relevant changed files, symbols, and dependency edges. Bind evidence to issue revision and exact source hash or commit. Fail closed if a requested source is absent or stale. Return evidence IDs and invalidation reasons; do not invent missing evidence.