# memoctl: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Uncommitted shared knowledge that agents leave for each other. Layer: L1 (primitive). Works fully standalone.

## Responsibilities

From architecture §5.1 (verbatim):

Uncommitted shared knowledge for agents: things other agents may need to know that should not be committed.

- Commands: `add --body-file <path>|-`, `list`, `show`, `search`, `context --repo <repo-id> [--label k=v]...`, `prune`.
- Uses: none.
- Notes: Keyed by repo and topic, with labels. Each note records a *claimed* author kind read from the env var named by config `kind_env` (default `CSESSION_KIND`; best effort, spoofable). A missing value is recorded as `kind=unknown`, which is treated like a third-party-reading kind (section 13.2 rule 8). `context` emits a compact, fenced block suitable for injection at session start (QR-19); by default it excludes notes whose claimed kind is third-party-reading or unknown.

From architecture §16 (Shared notes):

memoctl holds uncommitted knowledge that agents leave for each other (e.g. "the integration tests need the local queue running", "module X is mid-refactor on another branch"). Agents write with `memoctl add --body-file`; sessions of kinds with `notes_context: true` receive relevant notes at startup (not on resume or clear, by default) via the injected SessionStart hook calling `memoctl context`, fenced as untrusted, and notes with a third-party-reading or unknown claimed kind are excluded by default (section 13.2 rule 8). Storage, format and pruning are decided in the memoctl session.

## Uses

- None.

## Relevant architecture sections

Read these sections of `../awkit-docs/architecture.md` before designing:

- §3.4: cross-cutting principles (labels, untrusted text in files)
- §6: conventions summary
- §8.2: the session wrapper: SessionStart hook runs `notes_context_cmd`
- §13.2: prompt injection: rule 1 fencing, rule 2 transport, rule 8 notes
- §16: shared notes
- §20: this tool's constraints and open topics

## Constraints (decided)

From architecture §20:

- No dependency on claude-session (Uses: none).
- The author kind is a claimed kind read from the env var named by `kind_env`, and its defenses are those of architecture §13.2 rule 8.

## Open topics

From architecture §20:

- storage
- format
- keys
- pruning
- context selection

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. How are notes stored (state.db layout, migrations)?
2. What is a note's format (body, topic, labels, claimed kind, timestamps, size limits)?
3. What keys identify a note (repo id, topic, labels), and how do `show` and `search` address notes?
4. What is the pruning policy (age, count, explicit expiry)?
5. How does `context` select and order notes (repo, labels, kinds, size budget), and what exactly does the fenced block look like?
