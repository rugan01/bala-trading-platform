# Maintenance Map

This document answers one question clearly:

**Where should code be maintained now, and what still lives in `balas-product-os`?**

## Canonical Code Location

Primary staging repo:
- `/Users/rugan/Projects/bala-trading-platform`

This is the place to:
- organize reusable trading code
- prepare GitHub commits
- standardize paths, configs, and docs
- evolve the broker-agnostic platform design

## Operational Workspace

Existing operational workspace:
- `/Users/rugan/balas-product-os`

This remains the place for:
- active reports
- historical notes
- coaching context
- strategy notes
- day-to-day operational outputs
- legacy local runtime context until cutover is complete

## Practical Rule

Use this split for now:

- **Change code here first**:
  - `/Users/rugan/Projects/bala-trading-platform`
- **Keep live outputs and personal operational context there**:
  - `/Users/rugan/balas-product-os`

## Secrets

Secrets must remain local-only:
- `.env` in the repo root when running the staging repo
- `/Users/rugan/balas-product-os/.env` for the existing operational workspace until cutover

Do not commit:
- API keys
- tokens
- broker exports
- Notion snapshots
- SQLite archives

## Current Migration Status

Status: `staging repo created, code copied, primary defaults normalized`

What is already done:
- code groups copied into one monorepo
- repo-relative `.env` defaults added to primary entry points
- archive DB moved to repo-local `data/archive/`
- briefing outputs moved to repo-local `data/reports/premarket/`
- legacy analyzer outputs moved to repo-local `data/legacy-analyzers/`

What is still intentionally deferred:
- final GitHub remote creation and push
- full cutover away from the old operational workspace
- cleanup of every historical README/handover snapshot copied from older repos

## Before GitHub Push

Run these checks:
- verify `.env` is not present in `git status`
- verify no generated output folders are tracked
- verify no SQLite DB is tracked
- verify no broker export files are present
- run a compile check on key Python entry points

## Suggested Next Steps

1. Validate the staging repo entry points with a local `.env`.
2. Create the private GitHub repository.
3. Commit only code, curated docs, and templates.
4. Keep `balas-product-os` as the operational workspace until the first clean cutover milestone is complete.

