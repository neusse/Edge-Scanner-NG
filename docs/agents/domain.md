# Domain Docs

How engineering skills should consume this repository’s domain documentation.

## Before exploring, read these

- `CONTEXT.md` at the repository root.
- `CONTEXT-MAP.md` if it exists; read each linked context relevant to the work.
- Relevant architectural decisions under `docs/adr/`.

If these files do not exist, proceed silently. Do not request that they be created upfront. The domain-modeling workflow creates them when terminology or architectural decisions are actually resolved.

## File structure

Edge Scanner NG uses a single-context layout:

```
/
├── CONTEXT.md
├── docs/
│   └── adr/
├── scanner/
├── dashboard-v2/
├── scripts/
└── tests/
```

`CONTEXT.md` is the repository-wide glossary and domain model. `docs/adr/` contains durable architectural decisions.

## Use the glossary’s vocabulary

When output names a domain concept—in an issue title, proposal, hypothesis, or test—use the term defined in `CONTEXT.md`. Do not drift to synonyms the glossary explicitly avoids.

If a needed concept is absent, reconsider whether the language fits the project or note the genuine gap for domain modeling.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, surface that conflict explicitly rather than silently overriding the decision:

> Contradicts ADR-0007, but may be worth reopening because…
