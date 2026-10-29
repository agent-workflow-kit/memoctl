# memoctl

Uncommitted shared knowledge that agents leave for each other.

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L1 (primitive). Works fully standalone.

## What it does

Stores notes keyed by repo and topic, with labels: things other agents may need to know that should not be committed. Each note records a *claimed* author kind read from the env var named by `kind_env` (default `CSESSION_KIND`; best effort, spoofable; missing → `unknown`). `context` emits a compact, fenced block suitable for injection at session start and, by default, excludes notes whose claimed kind is third-party-reading or unknown.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md):

```text
memoctl add --body-file <path>|-
memoctl list | show | search
memoctl context --repo <repo-id> [--label k=v]...
memoctl prune
```

## Dependencies

- None (Uses: none in the architecture).

## Install

```text
uv tool install git+https://github.com/agent-workflow-kit/memoctl
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
