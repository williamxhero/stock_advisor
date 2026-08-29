# Domain Docs

This repository uses a single domain context.

## Before exploring

- Read the root `CONTEXT.md`.
- Read the ADRs under `docs/adr/` that affect the area being changed.
- Proceed silently if a referenced document does not exist; create domain documentation only when a term or durable decision is actually resolved.

## Vocabulary

Use terms exactly as defined in `CONTEXT.md`, including its `_Avoid_` guidance. If a required concept is missing or overloaded, resolve it through domain modeling before adding competing terminology.

## Decisions

Surface conflicts with existing ADRs explicitly. New decisions append an ADR or supersede the conflicting ADR; they do not silently rewrite historical decisions.
