# memoctl

Uncommitted shared knowledge that agents leave for each other: short notes about a repository that should not be committed, handed to the next session that works there.

## Status

Designed (interface 0.1.0-design); not yet implemented. The normative specification is [docs/interface.md](docs/interface.md); decisions are in [docs/design.md](docs/design.md).

## Layer

L1 (primitive). Works fully standalone; it does not need the `awkit` harness or any other tool.

## What it does

- Stores notes ("the integration tests need the local queue running", "module X is mid-refactor on another branch") keyed by repo and topic, with labels and a claimed author kind ([Note](docs/interface.md#note)).
- Keeps notes append-only: a changed fact is a new note that supersedes the old one, and the old one stays visible until it is pruned ([Identity and supersede](docs/interface.md#identity-and-supersede)).
- Lets notes expire (30 days by default, shorter with `--expires`) and prunes them on a schedule ([Lifetime](docs/interface.md#lifetime)).
- Prints a compact block of the relevant notes for a session start with `context`: only live notes of the repo whose author kind you allow, within a size budget, with every body line prefixed so no note can pose as a header or a fence ([Context selection](docs/interface.md#context-selection), [Context block](docs/interface.md#context-block)).
- Treats every note as untrusted text: it refuses hidden and control characters, high-confidence secrets and oversized bodies instead of rewriting them, and it limits how many notes can be written ([Body rules](docs/interface.md#body-rules), [Secret check](docs/interface.md#secret-check), [Limits](docs/interface.md#limits)).

## Quick start

```sh
echo 'The integration tests need the local queue running.' | memoctl add --repo github.com/example/project --topic integration-tests --body-file - --kind implement
memoctl list --repo github.com/example/project
memoctl show 1
memoctl context --repo=github.com/example/project
memoctl delete 1
```

## Commands

| Command | Purpose |
|---|---|
| `memoctl add --repo <id> --topic <t> (--body-file <path>\|-) [--label k=v]… [--kind <name>] [--supersedes <id>] [--expires <d>]` | Leave a note. Idempotent by content: the same note again returns the same id. |
| `memoctl list [--repo <id>] [--topic <t>] [--kind <k>] [--label k=v]… [--all]` | List notes (no bodies), newest first. |
| `memoctl show <id>` | Show one note in full. |
| `memoctl search (<text> \| --text-file <path>\|-) [--repo <id>] [--all]` | Substring search over topics and bodies. |
| `memoctl context --repo=<id> [--label=k=v]… [--fence]` | Print the notes block for a session start; exit 10 when there is nothing to show. |
| `memoctl delete <id>` | Delete a note after a confirmation on your terminal; a note it had replaced becomes live again. |
| `memoctl prune` | Delete expired and long-superseded notes. |
| `memoctl validate` | Offline checks of the config and the state; never prompts. |
| `memoctl cron install\|uninstall` | Install or remove the daily `prune` in your crontab. |

Global flags: `--json`, `--dry-run`, `--actor`, `--verbose`, `--quiet`, `--version` ([Global flags](docs/interface.md#global-flags)). There is no `--config`: the config and the state are found from your home directory in the password database.

## Using it with an agent session

Any tool can call `memoctl context --repo=<id> [--label=<key>=<value>]…` and inject its output; it prints plain text on stdout, never prompts, exits 0 (a block was printed) or 10 (nothing to show), and finishes within 2 seconds. Another tool's sandbox profile decides which sessions may run `memoctl add`; give third-party-reading sessions no access to it, and list in `context.author_kinds` only the author kinds you trust. See [Calling from automation](docs/interface.md#calling-from-automation).

## Safety

- The author kind is a claim: any process in a session can set it, so the allow list reduces risk and does not prove who wrote a note ([Author kind](docs/interface.md#author-kind), [Residual risks](docs/interface.md#residual-risks)).
- `context` only reads: it takes no write lock, creates no files and has a deadline, so a broken store never blocks a session start.
- Config and state are read only from your home directory in the password database, so an agent cannot redirect them with an environment variable ([Config](docs/interface.md#config)).

## Exit codes

0 done or a block printed, 1 internal error, 2 usage or invalid config (including a body over the limit or refused text), 10 not found or nothing to show, 12 failed (with a `reason` such as `db_locked` or `schema_newer`), 13 needs a human (`delete` without a terminal), 20 refused (`reason` such as `rate_limited`, `repo_full`, `secret_detected` or `unsafe_state`), 128+N a signal. See [Exit codes](docs/interface.md#exit-codes).

## Config and environment

- Config: `.config/memoctl/config.yaml` under your home directory plus `config.d/*.yaml` beside it; every key is documented in [config.example.yaml](config.example.yaml) and [Config](docs/interface.md#config).
- State: `.local/state/memoctl/` (`state.db` and `log.jsonl`, never any note text in the log).
- Environment: only the variable named by `kind_env` (default `CSESSION_KIND`) is read, for the claimed kind, and the variables named in `mask_env`; there are no `MEMOCTL_*` overrides ([Environment variables](docs/interface.md#environment-variables)).
- Hooks: none.

## Limits

- A secret in a form the [secret check](docs/interface.md#secret-check) does not recognize is stored and shown to later sessions in the repo.
- A flood from a session that claims an allowed kind cannot be stopped by memoctl; `delete` removes what it left.
- WAL is not reliable on network or Windows-backed mounts; `validate` warns.

## Dependencies

The Python standard library, Pydantic for config validation and PyYAML (with a strict loader) for the config file; memoctl calls no other tool and uses `crontab` only for `cron`.

## Requirements and install

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) to install it.

```sh
uv tool install git+https://github.com/agent-workflow-kit/memoctl
```

## Documentation

- [docs/interface.md](docs/interface.md): normative interface
- [docs/design.md](docs/design.md): decisions and internals
- [docs/plan.md](docs/plan.md): staged implementation plan
- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
