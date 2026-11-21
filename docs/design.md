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
| D-1 | Notes are append-only with an `AUTOINCREMENT` id; `add --supersedes <id>` replaces one note explicitly and hides it from `context` and default `list`; deleting the only replacement of a note restores it (after a stack of replacements, restoring the original takes one `delete` that names every member). | An upsert on (repo, topic) lets any writer, including an injected one, silently overwrite a real note, and hands two agents the same id. Deleting a superseded note repoints the note below it to the nearest remaining replacement, so a deleted middle note never resurrects a stale one. | [Identity and supersede](interface.md#identity-and-supersede) |
| D-2 | Default lifetime 30 days, optional shorter `--expires` (at least 60 seconds), superseded notes kept until their own expiry, and a replacement never expires before its target; `context` ignores expired notes before `prune` runs. | A stale note injected into a new session is worse than none. A grace period followed by `prune` let a supersede destroy a real note a few days later, and an expiring replacement resurrected a stale one. | [Lifetime](interface.md#lifetime) |
| D-3 | A repo is required on `add`; `context` without one shows nothing. | One writer must not be able to push text into every session on the machine; claude-session omits `--repo` for session-directory kinds. | [`add`](interface.md#add), [`context`](interface.md#context) |
| D-4 | `context` matches a note's labels as a subset of the session's labels. | A session carries many labels and few notes carry all of them, so the guide's AND filter is the wrong way round here. | [Context selection](interface.md#context-selection) |
| D-5 | An allow list of author kinds (`context.author_kinds`) instead of a deny list. | User-defined and unknown kinds start from the review profile (third-party-reading), so the safe default is to exclude what is not named. `context` cannot know the reader's kind: claude-session gives it no kind. | [Author kind](interface.md#author-kind) |
| D-6 | The body limit is 8 KiB by default, configurable to 64 KiB, and the NFKC form must fit the same limit; over-limit input is refused, never truncated; reading never follows a symlink and is bounded. | A naive read hung on a FIFO or terminal, followed symlinks and read `/dev/zero` into gigabytes in the design experiments; the context budget is about 8000 characters. The NFKC form must fit the limit too, and a run of more than 30 combining marks is refused, so read-time length and cost are linear in the stored size (NFKC is quadratic in such a run: 110 notes of 4000 marks made `context` miss its 2 s deadline in the safety experiments). | [Body rules](interface.md#body-rules) |
| D-7 | `context` prints plain text with no fence; `--fence` wraps it. | claude-session fences and sanitizes the output itself and rewrites any closing-tag look-alike, so a fence of memoctl's own would be mangled. A second consumer still has a safe form. | [Context block](interface.md#context-block) |
| D-8 | Header lines plus every body line prefixed with `` \| ``; the cut line and the omitted-notes line are unprefixed `[memoctl: ...]` lines. | A body can then never begin a header, a separator, a fence, the cut line or the omitted-notes line. | [Context block](interface.md#context-block) |
| D-9 | Selection by newest `id` within a subset label filter, at most 2 per topic, a per-kind share of characters and of notes in a first pass, a per-note cap and a note cap; header and omitted lines reserved. | A flood of 5000 bogus notes pushed all real notes out of a newest-first budget in the design experiments; read-side fairness kept them visible when the flood claimed a kind other than the real notes'; a flood that claims the real notes' kind, or several allowed kinds, still takes every slot with about 20 notes (safety experiment). Label-first ordering let notes carrying the label every session has (`kind=<kind>`) outrank newer notes, and a character-only share let tiny notes take every slot. A flood under a real kind is a residual risk. | [Context selection](interface.md#context-selection) |
| D-10 | Writes refuse at limits (per repo and store-wide) and never evict; limits are counted inside the write transaction. | Eviction ("keep the newest N") deletes real notes during a flood. The cost is that a full store refuses everyone; a person clears it with a multi-id `delete`. | [Limits](interface.md#limits) |
| D-11 | Config and state are read only from the password-database home; no `--config`, no `--state-dir`, no environment layer. | The write limits and the kind allow list live in config; a session allowed to run `add` could otherwise raise them through an environment variable while the state stays the real store. This closes the environment route; a same-user process that can write files can still edit the config itself. Same deviation as the notifiers and notice-route. | [Config](interface.md#config), [State](interface.md#state) |
| D-12 | Commands: `add`, `list`, `show`, `search`, `context`, `delete`, `prune`, `validate`, `cron`. Notes are immutable. | A person must be able to remove an injected note; `prune` needs a schedule; `awkit doctor` needs an offline check. `export`, `compact` and global notes are 1.x. | [Commands](interface.md#commands) |
| D-13 | No hooks in 0.x. | No consumer; the claimed kind makes a policy gate weak; a hook runs a user command per write by a possibly injected agent. | [Hooks](interface.md#hooks) |
| D-14 | `log.jsonl` records each change that completed with its append (per note, with supersede targets), never text or refusals; no `audit.jsonl`. | memoctl takes no external action; a hash-only change log suffices (as for the notifiers). A looping writer could roll a log of refusals over and erase the record of real changes. `context` writes nothing so it can run read-only. | [Log](interface.md#log) |
| D-15 | `list` and `search` default to all repos, and memoctl never derives the repo from the working directory or runs `git`. | No hardened-git dependency and no config read from a repository an agent may have modified. | [`list`](interface.md#list), [`search`](interface.md#search) |
| D-16 | Search is a substring match by the tool (casefolded, NFKC), not FTS5 or SQL `LIKE`. | A `LIKE` scan took about a millisecond at 10 000 notes, but SQLite case-folds ASCII only and FTS5 needs triggers, a rebuild on migration and query quoting. FTS5 with ranking is a 1.x candidate. The scan is streamed; its cost is one NFKC and casefold pass over the store, linear in its size (measured: 5000 worst-case 8 KiB notes in about 1.8 s against the 30 s deadline). | [`search`](interface.md#search) |
| D-17 | Exit codes keep the guide's meanings; `context` returns 0, 10, 12, 20, 2 or 1 only. | claude-session logs any other exit as a warning; the guide gives 11 and 13 other meanings, so `context` does not use them. | [Exit codes](interface.md#exit-codes) |
| D-18 | Strict grammars for every metadata field; `fullmatch`, never `$`. The repo, label and kind grammars are claude-session's (the repo pattern with `\Z` for `$`, which accepts the same strings under `fullmatch`); topic, id, actor, `kind_env` and durations are memoctl's own. All are matched with `fullmatch`. | `$` accepts a trailing newline; a stricter memoctl would make `context` fail for legal labels. memoctl adds only a length cap on `add`. | [Identifiers and grammars](interface.md#identifiers-and-grammars) |
| D-19 | Validate at write (refuse hidden and control characters, never rewrite; every Default_Ignorable code point is refused, variation selectors included), sanitize at `context` read (NFKC, strip, escape every `<`); `list`, `show` and `search` show the stored text. | NFKC at write expands bytes many times over and breaks some scripts, so a body whose NFKC form exceeds the limit is refused instead. Spike B kept the variation selectors; that was reversed so that hidden text and the secret-check bypass through them are impossible. People see the stored text with invisible characters made visible. The `<` rewrite is last, so no later step can assemble a tag. Runs of more than 30 combining marks are refused because they make NFKC quadratic. | [Body rules](interface.md#body-rules), [Sanitizer](interface.md#sanitizer) |
| D-20 | `add` is idempotent by content. | Retries and re-runs would otherwise fill the limits with identical notes. Dedupe runs before the supersede checks, so a retry of a supersede returns the note it created; `--supersedes` of an identical note is the refresh. A dedupe hit is always a live note. | [`add`](interface.md#add) |
| D-21 | No TUI in 0.x. | About 200 notes per repo; `list`, `show` and `delete` cover the human tasks. | [TUI](interface.md#tui) |
| D-22 | A short list of high-confidence secret patterns plus `mask_env` values is refused on `add`. | Notes are re-injected into every later session of the repo; no filter catches everything, so the residual risk is documented. Patterns are searched in raw and NFKC text, and the repo id and kind are checked too. | [Secret check](interface.md#secret-check) |
| D-23 | Every command has a deadline (`context.deadline`, 2 s by default, for `context`; 30 s for the others): a `SIGALRM` timer whose handler exits 12; all waits are bounded below it. | A deadline that only a Python-level check could notice does not fire while SQLite waits on a lock; a default-action signal would exit with a signal status and no `reason`. The handler prints pre-built bytes, and the timer is disarmed before stdout is written, so partial output cannot happen. A deadline that fires while a commit runs reports a stored change as `timeout`; a retry of `add` is safe and the log record is missing, the same window a signal has. Disarming before the commit was rejected because a hung commit would then have no bound. | [Deadlines](interface.md#deadlines) |
| D-24 | The state directory is checked on an `O_NOFOLLOW` descriptor; `state.db` is checked with `lstat`, created `0600` with `O_EXCL` by `add` only, and opened by path. | SQLite gives `-wal` and `-shm` the database's mode, not the umask's; a symlinked `state.db` is silently followed. Python's `sqlite3` takes no descriptor, so the check is against accidents and other users, not against a same-user swap. Rows are checked before use and `trusted_schema` is off. | [State](interface.md#state) |
| D-25 | Ordering uses `id` only; timestamps come from one gateway clock read per command, bound as a parameter; a caller can supply none. | A forged future timestamp sorted first and survived age pruning; a clock set back made a fresh note look old. | [Lifetime](interface.md#lifetime) |

## Resolved open topics

The five open topics of the seeded design are resolved as follows.

- **Storage:** one SQLite database in WAL mode, three tables ([Database](interface.md#database)); migrations additive only ([Migrations](interface.md#migrations)).
- **Format:** the note fields, the body rules and the sanitizer ([Note](interface.md#note), [Body rules](interface.md#body-rules), [Sanitizer](interface.md#sanitizer)).
- **Keys:** an integer id, grouped by repo and topic, with labels ([Identity and supersede](interface.md#identity-and-supersede)).
- **Pruning:** age, run by `prune` and a daily cron block ([Lifetime](interface.md#lifetime), [`prune`](interface.md#prune), [`cron`](interface.md#cron)).
- **Context selection:** [Context selection](interface.md#context-selection) and [Context block](interface.md#context-block).

## Live verification

Design-time experiments (free, local, offline) settled the items marked verified. Each open item becomes a `live` or process test in the plan; until it is checked the conservative default in the last column applies.

| # | Behavior | State | Default until verified |
|---|---|---|---|
| V1 | FTS5, `STRICT` tables, upsert and `RETURNING` exist in every interpreter the tool supports (the floor is 3.37 for `STRICT`; 3.45 was verified). | Verified on four interpreters. | No feature beyond what was tested is used. |
| V2 | `BEGIN IMMEDIATE` with a busy timeout loses no write under eight concurrent writers and four readers; a deferred transaction loses most. | Verified. | Writers always use `BEGIN IMMEDIATE`. |
| V3 | A read-only open of a WAL database can leave `-shm` and an empty `-wal`, and fails in a read-only directory. | Verified. | `context` catches the error and exits 12 `db_unreadable`. |
| V4 | The `context` deadline holds with a locked, corrupt, unreadable or newer-schema database and a closed stdout. | Verified in a prototype (20 cases). | The process tests repeat the matrix, including a deadline that fires during a commit. |
| V5 | claude-session's sanitizer, as documented, lets no closing-tag form through for the corpus used; memoctl's own output is unchanged by it. | Verified against a reimplementation of the documented rules; not against claude-session's code. | A differential test pins the reimplementation; V6 checks the real tool. |
| V6 | A real claude-session start with `notes_context` on shows an allowed note fenced in the injected context and omits a note whose kind is not allowed; the 10 000 code-unit cap leaves the block intact at the default budget. | Not verified. Needs a real `claude` run with a cheap model. | The budget stays at 8000. |
| V7 | claude-session's label, repo and kind checks reject a trailing newline. | Not verified (its code was not read). | memoctl uses `fullmatch` everywhere. |
| V8 | WAL behavior on a Windows-backed or network mount under WSL. | Not verified. | `validate` warns `unsafe_filesystem`. |
| V9 | macOS: paths, `crontab`, and the interpreter. | Not verified. | Linux and WSL are required platforms. |
| V10 | A cold `uv tool install` start of `context` stays well under the deadline with `pydantic` and `pyyaml` imported. | Imports measured (about 100 ms for pydantic). End to end not measured. | The deadline is 2 s. |
| V11 | A profile that denies `memoctl add` to third-party-reading kinds blocks a session in the sandbox, including writes to the state directory. | Not verified here; belongs to claude-session's live suite. | A cross-tool note asks claude-session to confirm it. |
| V12 | The entry `PATH=<home>/.local/bin:... memoctl prune ...` finds `memoctl` through the inline `PATH` under `dash`, `bash` and macOS `/bin/sh`, and `crontab -` accepts an empty input. | Not verified. | Linux with `dash` and `bash` is required; macOS is covered by V9. |

## Modules

Package `src/memoctl/` (design guide section 15):

| Module | Responsibility |
|---|---|
| `cli.py` | argparse, `--json` pre-scan, exit-code mapping, output. |
| `config.py` | Home lookup, YAML loading, the Pydantic config model, per-repo overrides, the effective config for a repo. |
| `grammar.py` | Every grammar, duration parsing, `fullmatch` helpers. |
| `bodyread.py` | The bounded reader for `--body-file` and standard input. |
| `body.py` | Body validation (six classes, combining runs, NFKC bound), the secret check. |
| `store.py` | SQLite open modes, migrations, the queries (all SQL lives here). |
| `notes.py` | The rules of `add`, `delete`, `prune`: supersede, dedupe, limits, chain repointing and reinstatement on `delete`. |
| `selection.py` | Context selection and fairness. |
| `render.py` | The sanitizer and the block (plain and fenced). |
| `search.py` | Term parsing and matching. |
| `statedir.py` | The state directory check and the `0600` creation. |
| `logfile.py` | `log.jsonl` append and rotation. |
| `crontab.py` | The managed block logic (pure; the calls are in the gateway). |
| `gateway.py` | The only module that reads the clock, randomness, the password database, the terminal, the filesystem handles, stdin and `crontab`; tests replace it. |

## Data model

The tables are in [Database](interface.md#database). `status` is derived at read time from `superseded_by` and `expires`, so no row needs updating when a note expires. The live-note count per repo is a query on `(repo, id)`; the rate counts come from the `rate` table, so deleting notes does not reopen the window; all counts run inside the write transaction. Schema versions: a version newer than the binary exits 12 for every command, and because read commands never migrate, every later migration must be additive and each later version must keep its reader for older ones. There is no `superseded_at`: status is derived from `superseded_by` alone.

## Concurrency

All writes (`add`, `delete`, `prune`, migration) take `BEGIN IMMEDIATE`; the limits, the dedupe check, the supersede checks and the insert happen under that one lock, so two concurrent adds cannot both pass a limit. `context` reads in one read transaction on a WAL snapshot and never takes a write lock, so it never blocks a writer and sees a note and its labels consistently. `prune` is one transaction (`prune --dry-run` is a read transaction and takes no write lock); a second `prune` that cannot get the lock within the busy timeout reports `busy` (another writer holds it). The log append is a single `O_APPEND` write; rotation takes `log.lock` with `flock`.

## Error handling

Errors fail fast with a code and a fixed message that never contains note text or an input value ([Exit codes](interface.md#exit-codes)). `context` is the one command that must never block a session start: every failure is an exit code and empty stdout inside the deadline. A write failure after the commit (the log) warns and never changes the result.

## Test strategy

- All access to the clock, randomness, the home, the terminal, file handles, stdin and `crontab` goes through `gateway.py`; tests replace it. Deadline tests allow 250 ms beyond `context.deadline` for the busy timeout. No test touches the real home or state.
- Golden files for every `--json` output, validated against `schemas/`; the config example is validated against `schemas/config.json`. Context golden cases include the cut of a single over-long line, a body ending in LF, a store where nothing is shown (exit 10), a 255-character repo id at `max_chars` 1000 and a note whose header alone exceeds the budget (never shown, exit 10). Golden refusal for a 4000-mark body (class `combining`) and a 30-mark body accepted; the sanitizer is pinned as vectorized (`str.translate`, no per-character Python loop).
- Property and fuzz tests: the grammars against an oracle (including a trailing newline), the body reader on special files (FIFO, symlink, `/dev/zero`, endless stdin), the sanitizer on a corpus of hidden text and closing-tag forms, differential against a pinned reimplementation of claude-session's documented sanitizer.
- Process tests (`tests/process`): eight writers and four readers on one database; `prune` against writers and readers with no torn read; the `context` matrix (missing, empty, large, locked, corrupt, newer, unreadable, closed stdout, closed stdin; the closed-descriptor cases cover every subset of descriptors 0, 1 and 2 closed, assert that the startup rule opens `/dev/null` in ascending order and that each open returned the descriptor it was meant to fill, that the first file opened afterwards gets 3 or higher, and that `state.db`, `-wal` and `-shm` are unchanged) with exit codes and times; 200 notes of 30-mark runs at the default config finish `context` within the deadline; a flood test (20 real notes of kind `audit` in 10 topics with older ids and 5000 bogus notes of kind `implement` in 2500 topics with newer ids, defaults: at least 10 real notes shown; two fixtures with their documented outcomes: 20 real `implement` notes and 20 newer `implement` flood notes in 10 topics show 0 real, and 20 real `audit` notes and a flood of 20 newer notes split over `implement` and `draft-ticket` show 0 real, so the passing fixture does not suggest more); chain A → B → C: `delete B` leaves C superseded by A, `delete A B` reinstates C, `delete A` reinstates B; retry of `add --supersedes` after a lost output returns the same id; `--supersedes <self-identical>` creates a new note.
- `live` tests (never in CI, deselected by default): V6, V8, V10 and V12 above, and the macOS checklist (V9).
- `make verify` runs ruff, pyright, pytest and markdown lint; CI also runs the `schemas` job, whose metaschema check must be run locally before a push.

## Architecture and guide changes recorded by this design

Architecture v1.26 (the memoctl design session): the section 5.1 memoctl entry (commands, append-only notes with supersede, the `=` argument form, `context --fence`), rule 8 of section 13.2 (claude-session fences; an allow list decides kinds; a profile denies `add` to third-party-reading kinds; read-side fairness and the refuse-only limits caveat), the isolation bullet of section 13.1 (memoctl's state directory), the hooks bullet of section 3.4 (a tool with no consumer omits an event), section 16 (storage and format settled; the context contract states the `=` form and that `context` is told no kind) and the section 20 memoctl entry (pruning is age and a daily cron block), and section 7.7 (awkit's memoctl adapter reproduces `memoctl cron install`'s three lines, pinned by a golden file, and `awkit doctor` reads the block's state with `memoctl cron install --dry-run --json`). Design guide v1.14: memoctl joins the home-only config deviation (section 5; the per-repo layer takes a fixed subset of keys), has no audit file and no hooks (sections 6 and 8) and prints its context block unfenced by default (section 12). The interface registry row and the roadmap's hook candidates (`post-add`, `pre-add`) are updated.
