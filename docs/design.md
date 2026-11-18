# memoctl: design

Status: 0.1.0-design, 2026-11-18. Designed against architecture v1.26. [interface.md](interface.md) is the single normative source for the tool's rules; this document records the decisions behind them, the live checks that are still open, and the shape of the code. It links to interface headings and does not restate them.

## Role and layer

Uncommitted shared knowledge that agents leave for each other. Layer: L1 (primitive). Works fully standalone: `memoctl add`, `list`, `show`, `search` and `delete` are useful at a terminal with nothing else installed, and `context` is a plain-text command that any consumer can call.

## Uses

| Tool | Binding | Why |
|---|---|---|
| none | | memoctl depends on no other awkit tool, on no third-party CLI except `crontab` (only for `cron`), and on no network. It is called by claude-session through that tool's `notes_context_cmd` (binding B on claude-session's side; memoctl does not know its caller). |

## Dependencies (justified)

- `pyyaml`: the config is YAML (design guide section 3). Used with `safe_load` through a duplicate-key-rejecting loader ([Config](interface.md#config)).
- `pydantic`: validates the config and the `--json` output models (guide section 3). Importing it costs about 100 ms on a cold start, well inside the `context` deadline; a design-time measurement put `context` on a 10 000-note store at 30 to 40 ms without it.
- Everything else is the standard library: `sqlite3` (WAL), `argparse`, `unicodedata`, `hashlib`, `secrets`, `signal`, `fcntl`.

## Decisions

| # | Decision | Rationale | Interface |
|---|---|---|---|
| D-1 | Notes are append-only with an `AUTOINCREMENT` id; `add --supersedes` replaces explicitly and keeps the old note visible. | An upsert on (repo, topic) lets any writer, including an injected one, silently overwrite a real note, and hands two agents the same id. | [Identity and supersede](interface.md#identity-and-supersede) |
| D-2 | Default lifetime 30 days, optional shorter `--expires`, superseded notes kept 7 days, `context` ignores expired notes before `prune` runs. | A stale note injected into a new session is worse than none. | [Lifetime](interface.md#lifetime) |
| D-3 | A repo is required on `add`; `context` without one shows nothing. | One writer must not be able to push text into every session on the machine; claude-session omits `--repo` for session-directory kinds. | [`add`](interface.md#add), [`context`](interface.md#context) |
| D-4 | `context` matches a note's labels as a subset of the session's labels. | A session carries many labels and few notes carry all of them, so the guide's AND filter is the wrong way round here. | [Context selection](interface.md#context-selection) |
| D-5 | An allow list of author kinds (`context.author_kinds`) instead of a deny list. | User-defined and unknown kinds start from the review profile (third-party-reading), so the safe default is to exclude what is not named. `context` cannot know the reader's kind: claude-session gives it no kind. | [Author kind](interface.md#author-kind) |
| D-6 | The body limit is 8 KiB and over-limit input is refused, never truncated; reading never follows a symlink and is bounded. | A naive read hung on a FIFO or terminal, followed symlinks and read `/dev/zero` into gigabytes in the design experiments; the context budget is about 8000 characters. | [Body rules](interface.md#body-rules) |
| D-7 | `context` prints plain text with no fence; `--fence` wraps it. | claude-session fences and sanitizes the output itself and rewrites any closing-tag look-alike, so a fence of memoctl's own would be mangled. A second consumer still has a safe form. | [Context block](interface.md#context-block) |
| D-8 | Header lines plus every body line prefixed with `\| `. | A body can then never begin a header, a separator, a fence or the omitted-notes line. | [Context block](interface.md#context-block) |
| D-9 | Selection by label match then newest `id`, at most 2 per topic, a per-kind share of the budget, a per-note cap and a note cap; whole notes only. | A flood of 5000 bogus notes pushed all real notes out of a newest-first budget in the design experiments; only read-side fairness kept them visible. A slow flood under an allowed kind is a residual risk. | [Context selection](interface.md#context-selection) |
| D-10 | Writes refuse at limits and never evict; limits are counted inside the write transaction. | Eviction ("keep the newest N") deletes real notes during a flood. | [Limits](interface.md#limits) |
| D-11 | Config and state are read only from the password-database home; no `--config`, no `--state-dir`, no environment layer. | The write limits and the kind allow list live in config; a session allowed to run `add` could otherwise raise them through an environment variable while the state stays the real store. Same deviation as the notifiers and notice-route. | [Config](interface.md#config), [State](interface.md#state) |
| D-12 | Commands: `add`, `list`, `show`, `search`, `context`, `delete`, `prune`, `validate`, `cron`. Notes are immutable. | A person must be able to remove an injected note; `prune` needs a schedule; `awkit doctor` needs an offline check. `export`, `compact` and global notes are 1.x. | [Commands](interface.md#commands) |
| D-13 | No hooks in 0.x. | No consumer; the claimed kind makes a policy gate weak; a hook runs a user command per write by a possibly injected agent. | [Hooks](interface.md#hooks) |
| D-14 | `log.jsonl` only (hashes and ids, never text); no `audit.jsonl`. | memoctl takes no external action; a hash-only log suffices (as for the notifiers). `context` writes nothing so it can run read-only. | [Log](interface.md#log) |
| D-15 | `list` and `search` default to all repos, and memoctl never derives the repo from the working directory or runs `git`. | No hardened-git dependency and no config read from a repository an agent may have modified. | [`list`](interface.md#list), [`search`](interface.md#search) |
| D-16 | Search is a substring match by the tool (casefolded, NFKC), not FTS5 or SQL `LIKE`. | A `LIKE` scan took about a millisecond at 10 000 notes, but SQLite case-folds ASCII only and FTS5 needs triggers, a rebuild on migration and query quoting. FTS5 with ranking is a 1.x candidate. | [`search`](interface.md#search) |
| D-17 | Exit codes keep the guide's meanings; `context` returns 0, 10, 12, 20, 2 or 1 only. | claude-session logs any other exit as a warning; the guide reserves 11 and 13 for other meanings. | [Exit codes](interface.md#exit-codes) |
| D-18 | Strict grammars for every metadata field, copied from claude-session; `fullmatch`, never `$`. | `$` accepted a trailing newline in about one string in ten of a fuzz corpus; a stricter memoctl would make `context` fail for legal labels. | [Identifiers and grammars](interface.md#identifiers-and-grammars) |
| D-19 | Validate at write (refuse hidden and control characters, never rewrite), sanitize at read (NFKC, strip, escape `<`). | NFKC at write expands bytes up to eleven times and breaks some scripts; hidden text must be visible to the person reviewing a note. The `<` rewrite is last, so no later step can assemble a tag. | [Body rules](interface.md#body-rules), [Sanitizer](interface.md#sanitizer) |
| D-20 | `add` is idempotent by content. | Retries and re-runs would otherwise fill the limits with identical notes. | [`add`](interface.md#add) |
| D-21 | No TUI in 0.x. | About 200 notes per repo; `list`, `show` and `delete` cover the human tasks. | [TUI](interface.md#tui) |
| D-22 | A short list of high-confidence secret patterns plus `mask_env` values is refused on `add`. | Notes are re-injected into every later session of the repo; no filter catches everything, so the residual risk is documented. | [Secret check](interface.md#secret-check) |
| D-23 | The `context` deadline is a `SIGALRM` timer whose handler exits 12; all waits are bounded below it. | A deadline that only a Python-level check could notice does not fire while SQLite waits on a lock; a default-action signal would exit with a signal status and no `reason`. | [Deadlines](interface.md#deadlines) |
| D-24 | The state directory is checked on an `O_NOFOLLOW` descriptor and `state.db` is created `0600` with `O_EXCL` before SQLite opens it. | SQLite gives `-wal` and `-shm` the database's mode, not the umask's; a symlinked `state.db` is silently followed. | [State](interface.md#state) |
| D-25 | Ordering uses `id` only; timestamps come from SQL `unixepoch()`; a caller can supply none. | A forged future timestamp sorted first and survived age pruning; a clock set back made a fresh note look old. | [Lifetime](interface.md#lifetime) |

## Resolved open topics

The five open topics of the seeded design are resolved as follows.

- **Storage:** one SQLite database in WAL mode, two tables ([Database](interface.md#database)); migrations additive only ([Migrations](interface.md#migrations)).
- **Format:** the note fields, the body rules and the sanitizer ([Note](interface.md#note), [Body rules](interface.md#body-rules), [Sanitizer](interface.md#sanitizer)).
- **Keys:** an integer id, grouped by repo and topic, with labels ([Identity and supersede](interface.md#identity-and-supersede)).
- **Pruning:** age, supersede grace and the clock-fault rule, run by `prune` and a daily cron block ([Lifetime](interface.md#lifetime), [`prune`](interface.md#prune), [`cron`](interface.md#cron)).
- **Context selection:** [Context selection](interface.md#context-selection) and [Context block](interface.md#context-block).

## Live verification

Design-time experiments (free, local, offline) settled the items marked verified. Each open item becomes a `live` or process test in the plan; until it is checked the conservative default in the last column applies.

| # | Behavior | State | Default until verified |
|---|---|---|---|
| V1 | FTS5, `STRICT` tables, upsert and `RETURNING` exist in every interpreter the tool supports (SQLite 3.45 or newer). | Verified on four interpreters. | No feature beyond what was tested is used. |
| V2 | `BEGIN IMMEDIATE` with a busy timeout loses no write under eight concurrent writers and four readers; a deferred transaction loses most. | Verified. | Writers always use `BEGIN IMMEDIATE`. |
| V3 | A read-only open of a WAL database can leave `-shm` and an empty `-wal`, and fails in a read-only directory. | Verified. | `context` catches the error and exits 12 `db_unreadable`. |
| V4 | The `context` deadline holds with a locked, corrupt, unreadable or newer-schema database and a closed stdout. | Verified in a prototype (20 cases). | The process tests repeat the matrix. |
| V5 | claude-session's sanitizer, as documented, lets no closing-tag form through for the corpus used; memoctl's own output is unchanged by it. | Verified against a reimplementation of the documented rules; not against claude-session's code. | A differential test pins the reimplementation; V6 checks the real tool. |
| V6 | A real claude-session start with `notes_context` on shows an allowed note fenced in the injected context and omits a note whose kind is not allowed; the 10 000 code-unit cap leaves the block intact at the default budget. | Not verified. Needs a real `claude` run with a cheap model. | The budget stays at 8000. |
| V7 | claude-session's label, repo and kind checks reject a trailing newline. | Not verified (its code was not read). | memoctl uses `fullmatch` everywhere. |
| V8 | WAL behavior on a Windows-backed or network mount under WSL. | Not verified. | `validate` warns `unsafe_filesystem`. |
| V9 | macOS: paths, `crontab`, and the interpreter. | Not verified. | Linux and WSL are required platforms. |
| V10 | A cold `uv tool install` start of `context` stays well under the deadline with `pydantic` and `pyyaml` imported. | Imports measured (about 100 ms for pydantic). End to end not measured. | The deadline is 2 s. |
| V11 | A profile that denies `memoctl add` to third-party-reading kinds blocks a session in the sandbox, including writes to the state directory. | Not verified here; belongs to claude-session's live suite. | A cross-tool note asks claude-session to confirm it. |

## Modules

Package `src/memoctl/` (design guide section 15):

| Module | Responsibility |
|---|---|
| `cli.py` | argparse, `--json` pre-scan, exit-code mapping, output. |
| `config.py` | Home lookup, YAML loading, the Pydantic config model, per-repo overrides. |
| `grammar.py` | Every grammar, duration parsing, `fullmatch` helpers. |
| `body.py` | Body reading and validation, the secret check. |
| `store.py` | SQLite open modes, migrations, the queries (all SQL lives here). |
| `notes.py` | The rules of `add`, `delete`, `prune`: supersede, dedupe, limits, reinstatement. |
| `selection.py` | Context selection and fairness. |
| `render.py` | The sanitizer and the block (plain and fenced). |
| `search.py` | Term parsing and matching. |
| `statedir.py` | The state directory check and the `0600` creation. |
| `logfile.py` | `log.jsonl` append and rotation. |
| `crontab.py` | The managed block logic (pure; the calls are in the gateway). |
| `gateway.py` | The only module that reads the clock, the password database, the terminal, the filesystem handles, stdin and `crontab`; tests replace it. |

## Data model

The tables are in [Database](interface.md#database). `status` is derived at read time from `superseded_by` and `expires`, so no row needs updating when a note expires. The live-note count per repo and the add-rate count are queries on `(repo, id)` and `created`, inside the write transaction.

## Concurrency

All writes (`add`, `delete`, `prune`, migration) take `BEGIN IMMEDIATE`; the limits, the dedupe check, the supersede checks and the insert happen under that one lock, so two concurrent adds cannot both pass a limit. `context` reads in one read transaction on a WAL snapshot and never takes a write lock, so it never blocks a writer and sees a note and its labels consistently. `prune` is one transaction; a second `prune` that cannot get the lock within the busy timeout reports `already_running`. The log append is a single `O_APPEND` write; rotation takes `log.lock` with `flock`.

## Error handling

Errors fail fast with a code and a fixed message that never contains note text or an input value ([Exit codes](interface.md#exit-codes)). `context` is the one command that must never block a session start: every failure is an exit code and empty stdout inside the deadline. A write failure after the commit (the log) warns and never changes the result.

## Test strategy

- All access to the clock, the home, the terminal, file handles, stdin and `crontab` goes through `gateway.py`; tests replace it. No test touches the real home or state.
- Golden files for every `--json` output, validated against `schemas/`; the config example is validated against `schemas/config.json`.
- Property and fuzz tests: the grammars against an oracle (including a trailing newline), the body reader on special files (FIFO, symlink, `/dev/zero`, endless stdin), the sanitizer on a corpus of hidden text and closing-tag forms, differential against a pinned reimplementation of claude-session's documented sanitizer.
- Process tests (`tests/process`): eight writers and four readers on one database; `prune` against writers and readers with no torn read; the `context` matrix (missing, empty, large, locked, corrupt, newer, unreadable, closed stdout, closed stdin) with exit codes and times; a flood test (5000 bogus notes leave at least half the real notes visible in the default budget).
- `live` tests (never in CI, deselected by default): V6, V8 and V10 above, and the macOS checklist (V9).
- `make verify` runs ruff, pyright, pytest and markdown lint; CI also runs the `schemas` job, whose metaschema check must be run locally before a push.

## Architecture and guide changes recorded by this design

Architecture v1.26 (the memoctl design session): the section 5.1 memoctl entry (commands, append-only notes with supersede, the `=` argument form, `context --fence`), rule 8 of section 13.2 (claude-session fences; an allow list decides kinds; a profile denies `add` to third-party-reading kinds), section 16 (storage and format settled; the context contract states the `=` form and that `context` is told no kind) and the section 20 memoctl entry. Design guide v1.14: memoctl joins the home-only config deviation (section 5), has no audit file and no hooks (sections 6 and 8) and prints its context block unfenced by default (section 12). The interface registry row and the roadmap's hook candidates (`post-add`, `pre-add`) are updated.
