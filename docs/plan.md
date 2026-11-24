# memoctl: implementation plan

Status: 0.1.0-design, 2026-11-18. This plan implements [interface.md](interface.md) (the single normative source) and [design.md](design.md) (the decisions and the open live checks). Each stage is expanded into bite-sized test-first steps when its issue is picked up; this plan fixes the order, the boundaries and the exit criteria, not the code.

## Conventions for every stage

- **Test first.** Write the failing test from the interface, make it pass with the smallest code, then refactor with the tests green. A rule in interface.md without a test does not exist yet.
- **Gateway boundary.** Every read of the clock, the password database, the terminal, the standard descriptors, the filesystem handles, standard input, randomness and `crontab` goes through `src/memoctl/gateway.py`, so tests substitute fakes. No test touches a real home, a real state directory or a real crontab. The gateway fake gives a fixed clock the test can move, a fake home in a temporary directory, a fake terminal and a recording crontab.
- **Pinned sibling facts.** memoctl never imports a sibling. The facts it relies on live in `tests/pinned/` as copies pinned to a version: claude-session's label, repo-id and kind grammars (copied as test data from its documented patterns), its `notes_context_cmd` call shape (`--repo=<id>` and one `--label=<key>=<value>` per label, exit 0 and 10 success) and a verbatim copy of its sanitizer (v0.38.4, commit 50b4b25) used only for a differential test of memoctl's own output. A change in a sibling's contract is a new pinned copy and a changed test, never a silent edit.
- **Process tests and live tests.** Unit tests run the library with the gateway fake. Process tests (`tests/process/`, marked `process`, part of `make verify`) run the real command in a subprocess against a temporary home with a fake `crontab` binary from `tests/fakebin/` and cover concurrency, signals, closed descriptors and the deadline. Live tests (`tests/live/`, marked `live`, deselected by default, never in CI) need a real `claude` and a real claude-session (V6) or a Windows-backed mount (V8).
- **Golden files.** Every `--json` output and every human output has a golden file under `tests/golden/`, validated against `schemas/`. The schemas are the contract: a test that builds output must validate it, and the golden files are regenerated only by an explicit flag and reviewed in the diff.
- **Spike regressions.** The design experiments become regression tests: the `BEGIN IMMEDIATE` loss test (eight writers and four readers, no lost add), the read-only open of a WAL database and the `-wal` and `-shm` files it may leave, the `context` robustness matrix (missing, empty, large, locked, corrupt, newer schema, unreadable, closed stdout, closed stdin), the bounded body reader on special files (FIFO, symlink, `/dev/zero`, endless stdin), the grammar fuzz against an oracle including a trailing newline, the sanitizer corpus (closing-tag forms, hidden text, compatibility forms of `<`), the flood fixtures with their documented outcomes and the NFKC cost fixtures (long runs of combining marks, large compatibility expansions).
- **`make verify`** runs ruff, pyright, pytest (including the process tests) and markdown lint, and must pass before every commit. Before a push, also run the CI `schemas` job's command locally (`check-jsonschema --check-metaschema` at CI's pinned version over `schemas/*.json schemas/cli/*.json`), because `make verify` does not, and a test compiles every schema pattern with the `regress` engine that CI uses.
- **Writing rules.** Docs and comments in American spelling; markdown paragraphs and list items on one line; no private identifiers or paths in any file; the repository stays free of organization specifics.
- **Dependencies.** Only `pyyaml` and `pydantic` are added to the scaffold, as design.md justifies. Adding any other package needs a design.md entry first.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `gateway.py` | 0 and 1 | The only module that touches the clock (wall and monotonic), the home lookup, the terminal, argv, descriptors, stdin, randomness, `crontab`, signals and threads (`alarm_arm`, `exit_now`); the test fake. |
| `grammar.py` | 0 | Every grammar, duration parsing with integer arithmetic, `fullmatch` helpers. |
| `config.py` | 0 | Home lookup, drop-in loading with the strict YAML loader, the Pydantic model, per-repo overrides, cross-key checks. |
| `cli.py` | 0 | Entry point: the descriptor check, the `--json` pre-scan, dispatch, the last-resort handler. |
| `cli_args.py` | 0 | argparse with its printing and exiting neutralized, global flags, the `Command` record, the pre-scan. |
| `cli_commands.py` | 0 | The table of subcommands (one line per command). |
| `cli_errors.py` | 0 | Exit codes, the error envelope (schema rules enforced), the exception-to-exit-code table. |
| `statedir.py` | 1 | The state directory check on a descriptor, creation modes, `lstat` of `state.db`. |
| `deadline.py` | 1 | The command deadline: arm, re-arm and disarm points, the pre-built timeout report, the progress callback and busy-timeout cap for the store. |
| `store.py` | 1 | Opening SQLite (modes, pragmas, the progress handler, `setlimit`), migrations, row checks, every query. |
| `bodyread.py` | 2 | The bounded reader for `--body-file` and standard input (size cap, symlink and special-file refusals). |
| `body.py` | 2 | Body validation (six classes, combining runs, NFKC bound). |
| `secretscan.py` | 2 | The secret check (patterns, `mask_env`). |
| `cmd_add.py` | 3 | The `add` command: the 14 ordered steps, one function each. |
| `cmd_delete.py` | 6 | The `delete` command: ids, lookup, confirmation on `/dev/tty`, one write transaction, log records, output. |
| `cmd_prune.py` | 6 | The `prune` command: one clock read, the read-only dry run, one write transaction, the busy outcome, the deadline disarm, log records, output. |
| `notes.py` | 3 and 6 | The rules `add` calls (dedupe, supersede, limits: #17, #18), `delete` (chain repointing), `prune`. |
| `logfile.py` | 3 | `log.jsonl` append and rotation. |
| `searchmatch.py` | 4 | Search term parsing and matching. |
| `cmd_search.py` | 4 | The `search` command. |
| `cmd_context.py` | 5 | The `context` command: argv, exit paths, the deadline re-arm, one read snapshot, render, disarm, the complete write. |
| `cmd_validate.py` | 6 | The `validate` command: argv, the checks, output, status line, deadline disarm. |
| `sanitize.py` | 4 and 5 | The read-time sanitizer. |
| `render.py` | 4 | Terminal rendering of stored text for `list`, `show` and the other human output (not `context`). |
| `context_render.py` | 5 | The context block (plain and fenced) and the `--json` document, pure functions over a `Selection`. |
| `selection.py` | 5 | Context selection: candidates, order, the two-pass kind-share walk, the reserved lines. |
| `crontab.py` | 6 | The managed block: parsing, the three exact lines, install and uninstall outcomes (pure; calls go through the gateway). |

## Stage 0: Scaffold, gateway, config, grammars, command line

**Goal**: A package that parses every command line of the interface, loads config, validates every grammar and prints the error envelope, with all outside access behind the gateway fake.
**Success Criteria**: `memoctl --version`, `--help` and every usage error behave as in [Output](interface.md#output) (exit codes, envelope, flags only after the subcommand, standard descriptors reopened on `/dev/null`); config loads from the password-database home with drop-ins, the strict loader, the per-repo override whitelist and the cross-key checks ([Config](interface.md#config)); `config.example.yaml` validates against `schemas/config.json`; every grammar passes the oracle fuzz including a trailing newline.
**Tests**: gateway fake unit tests; grammar tests and fuzz against an oracle; config tests (unknown key, duplicate YAML key, `no`/`off` stay strings, `8000.0` refused, drop-in order and symlink refusal, unsafe ownership, per-repo override of a refused key); argument-parsing tests for every row of the Usage `field` table; golden error envelopes; the pinned claude-session grammars as test data.
**Status**: Not Started

## Stage 1: State directory, store, migrations, deadlines

**Goal**: A store that opens safely in every mode, initializes and migrates, never trusts a row, and a deadline that ends a command cleanly.
**Success Criteria**: the state directory checks of [State](interface.md#state) (symlink, wrong owner, group or world access, missing parents at 0700); `state.db` created `0600` with exclusive creation, opened by path after an `lstat` check; uninitialized, newer and foreign databases behave as in [Database](interface.md#database), [Migrations](interface.md#migrations) and [Missing store](interface.md#missing-store); every connection sets the pragmas, the length limit and the progress handler; the `SIGALRM` deadline writes its pre-built bytes and exits 12 as in [Deadlines](interface.md#deadlines).
**Tests**: state-directory matrix on a temporary home; migration tests (new store, repeated migration, a racing second process, a newer `user_version`, a wrong `application_id`); a database with triggers, views or a malicious schema opened read-only; row-check tests (a hand-built row failing each grammar); deadline tests (a lock held by another process, a long statement interrupted, no partial stdout, `COMMIT` window behavior) with a 250 ms tolerance; the read-only open leaving `-wal` and `-shm`.
**Status**: Not Started

## Stage 2: Body, secrets, durations

**Goal**: Everything `add` needs before it touches the store: bounded reading, the body rules and the secret check, and the lifetime arithmetic.
**Success Criteria**: the bounded reader and every Body rules item ([Body rules](interface.md#body-rules)) with their classes, lines and limits; the secret check with every pattern and `mask_env` ([Secret check](interface.md#secret-check)) over the repo id, topic, labels, kind and body, raw and NFKC; duration parsing with integer arithmetic (delivered by #4, `memoctl.grammar.parse_duration`); the author-kind resolution ([Author kind](interface.md#author-kind)).
**Tests**: reader tests on every special file and an endless stdin; a refusal golden for each of the six classes and the length cases (including a 4000-mark body and a 30-mark body accepted, and a body whose NFKC form exceeds the limit); a pattern test per secret class plus linear-time checks on adversarial input (the JWT prefix repeated, the private-key header repeated); duration edge cases (`59.5s`, an eleven-digit overflow, non-ASCII digits, a trailing newline); kind from flag, variable and malformed variable.
**Status**: Not Started

## Stage 3: `add` and the log

**Goal**: `add` complete: the 14-step order, dedupe, supersede, lifetime, the limits and the rate table, `--dry-run`, the warnings and the log.
**Success Criteria**: the order of [`add`](interface.md#add) exactly, including dedupe before the supersede checks and hits only on live notes; supersede checks and the lifetime rule ([Identity and supersede](interface.md#identity-and-supersede), [Lifetime](interface.md#lifetime)); every limit in the order and arithmetic of [Limits](interface.md#limits) with `retry_after_s`; `--dry-run` validating only; one log record per created note ([Log](interface.md#log)); `add` output and human output match the goldens.
**Tests**: process tests with eight concurrent writers (no lost add, no limit overshoot); retry of a superseding `add` after a lost output returns the same id; `--supersedes` of an identical note creates a fresh note; refusal tests for each reason; rate-window boundary tests with the gateway clock; log record content and rotation under concurrent writers; a store at exactly `total`.
**Status**: Not Started

## Stage 4: `list`, `show`, `search`

**Goal**: The read commands with their output, the hidden count and terminal-safe rendering.
**Success Criteria**: [`list`](interface.md#list), [`show`](interface.md#show) and [`search`](interface.md#search) with filters, `--all`, `--limit`, `hidden`, streaming search (memory does not grow with the store) and the terminal rendering of [Terminal rendering](interface.md#terminal-rendering); human and JSON outputs match the goldens; a missing store behaves as in [Missing store](interface.md#missing-store).
**Tests**: golden outputs for each command and status; label AND filtering; `hidden` under each filter; a search over 5000 worst-case notes within the deadline; a stored row with a control character rendered visibly; a terminal never receives an escape from a note.
**Status**: Not Started

## Stage 5: `context`

**Goal**: The command claude-session calls: read-only, bounded, exact.
**Success Criteria**: the order of [`context`](interface.md#context) with every exit path and its `--json` form; the sanitizer ([Sanitizer](interface.md#sanitizer)); the selection walk, reserved lines and counting of [Context selection](interface.md#context-selection) giving the same selection as the pseudocode; the block and the `--fence` form of [Context block](interface.md#context-block); the `=` and spaced argument forms; no write lock, no log, no directory created.
**Tests**: the robustness matrix as process tests with exit codes and times; golden blocks (a cut single line, a body ending in LF, nothing shown exits 10, a 255-character repo id at the minimum budget, a note whose header alone exceeds the budget); the walk against the pseudocode on random fixtures; the flood fixtures with their documented outcomes (kind-share survivors; the cases where 0 real notes survive); 200 worst-case notes within the deadline; a differential test against the pinned copy of claude-session's sanitizer (output unchanged by it); closed stdout and stdin leave the database files unchanged.
**Status**: Not Started

## Stage 6: `delete`, `prune`, `validate`, `cron`

**Goal**: Maintenance and integration commands.
**Success Criteria**: [`delete`](interface.md#delete) with several ids, the confirmation on `/dev/tty`, chain repointing and reinstatement; [`prune`](interface.md#prune) expired-only with its dry run and `busy` outcome and the rate-row cleanup; [`validate`](interface.md#validate) with the four checks, reasons and the warning; [`cron`](interface.md#cron) with the exact three lines, the parsing rules, every outcome and the dry run.
**Tests**: chain tests (`A → B → C`: `delete A`, `delete B`, `delete A B`) and a model-based random test of add, supersede, delete and expire against an in-memory model; `delete` without a terminal exits 13; `prune` against concurrent writers and readers with no torn read; `validate` golden output for each failing check; the cron block parser on malformed blocks (start without end, two ends, a CR) and a recording `crontab` fake; the block bytes pinned by a golden file that the harness's tests can copy.
**Status**: Not Started

## Stage 7: Integration, live checks, release readiness

**Goal**: The live checks of design.md, the README and release readiness for 0.1.0.
**Success Criteria**: the `live` suite runs: V6 (a real claude-session start with notes injected fenced, a disallowed kind omitted, the 10,000 code-unit cap respected), V7 (claude-session rejects a trailing newline in its grammars), V8 (WAL on a Windows-backed mount), V10 (a cold `uv tool` start of `context` inside the deadline); the macOS checklist (V9) is done by hand; the README matches the shipped behavior; `awkit-docs/interface-registry.md` carries the released version.
**Tests**: the live tests above; the full `make verify`; the CI schemas command; an install from the Git URL on a clean machine.
**Status**: Not Started

## Version roadmap

- **0.x:** stages 0 to 7. Each merged stage is a `feat` or `fix` PR; breaking changes before 1.0 are `feat` commits whose title names the break, with an update to the registry.
- **1.0 qualifies when:** the live suite passes against a real claude-session (V6 and V7 closed); the interface has survived a few weeks of real use without a breaking change; the harness has a tested adapter (`list --json`, `show --json`, `delete`, `validate --json`, the cron block reproduced byte for byte); the open items of design.md's Live verification table are closed or accepted. The 1.0 PR is a `!` commit.
- **1.x (additive):** global notes (`--global`), `export`, `compact` (the manual `VACUUM`), a `browse` picker (after a feature inventory of the sibling TUIs), FTS5 search with ranking, `post-add` and `pre-add` hooks.
- **2.0:** nothing planned. Anything that changes a rule of the interface is a major version.
