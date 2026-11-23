# Task receipt contract, version 3

Receipts are attributed evidence under `.sortie/shifts/<id>/receipts/`. They do
not sign authority, run commands, admit a shift, or prove that tests ran. The
[driver lifecycle](lifecycle.md) consumes them: the host validates and records
a builder's receipt when the driver issues `consume`, and writes, validates
and records the receipt of every review itself.

## Create and check evidence

Print examples, then fill in the actual IDs, paths, hashes, revisions and results:

```sh
sortie receipt template review
sortie receipt template review --expected
sortie receipt validate --shift PATH/.sortie/shifts/SHIFT \
  --receipt receipts/RECEIPT_ID.json --expected expected.json
```

Templates also exist for `plan`, `build`, `acknowledgment`, and `stop`. Example
hashes and commits are placeholders; templates alone cannot pass a live check.
The expected-context file is separate, caller-owned coordination input. The
driver prepares it from its active brief, phase and policy, not from the received
receipt. The driver also passes the digest of the context it built to the
validator, which refuses any other file contents before checking or recording,
so a published `briefs/expected-*` file replaced by another writer cannot change
the signed review policy. The standalone CLI and unpinned Python callers trust
the file they name. Protect the consumed ledger, and expected files used without
a pin, from builder writes in the builder's own agent configuration. This is
a caller obligation: the toolkit ships no permission rules, and there is no
OS boundary against a malicious unrestricted same-user process.

Use `--record` when consuming evidence for a phase transition. Without it, valid
evidence with no blockers returns `diagnostic`, `satisfies_phase: false`,
`recorded: false`, and exit 5. Repeated diagnostics make no reservation and never
permit advancement. Only successful recording can return `satisfied`,
`satisfies_phase: true`, `recorded: true`, and exit 0. Recording uses a bounded
exclusive lock and atomic update of `receipts/.consumed.json`. The same receipt
ID or task/request/role/phase/round slot cannot be recorded twice, including after
restart or through a different receipt filename. Valid blocked evidence can be
recorded as observed, but never returns `satisfies_phase: true`; its slot is
used up all the same. The host records its review receipt for every outcome,
so the ledger holds one entry for each review that ended, accepted or not. A repair requires
a new receipt ID and the next caller-authorized round/context. No task scheduling
or round advancement is implemented here.

| Exit | JSON status | Meaning |
| --- | --- | --- |
| 0 | `satisfied` | Evidence satisfies this phase and its slot was recorded. |
| 1 | `blocked` | Valid evidence reports a failed or incomplete required outcome. |
| 2 | argparse diagnostic | Invalid CLI usage. |
| 3 | `invalid` | Malformed, stale, mismatched, missing or unsafe evidence; IO/deadline refusal. |
| 4 | `duplicate` | Receipt ID or phase slot consumed; cannot advance again. |
| 5 | `diagnostic` | Evidence checks passed without recording; cannot advance. |

`validate_receipt(shift, receipt_path, expected_path, record=False, seconds=10,
expected_sha256=None)` provides the same result to Python callers; invalid input
raises `Refused`. With `expected_sha256`, the expected file must hash to exactly
that value.
`write_receipt_atomic(shift, value, expected_path, seconds=10)` validates and
writes a new `receipts/<receipt_id>.json` without overwriting an existing file.
It does not reserve a phase. The command for it, and the one way a builder
seat writes a receipt, is:

```sh
sortie receipt write --shift PATH/.sortie/shifts/SHIFT \
  --expected logs/expected-REQUEST.json --from logs/draft-REQUEST.json
```

`--from` is a filled template and `--expected` the phase's expected context,
both kept in the shift and named relative to it. The draft gets the same
checks as `receipt validate` makes, and is then written once under its own
`receipt_id`. A review receipt is refused here: no seat writes one. Output is one JSON object. Exit 0 and `"status": "written"`
come with the `path`, the `sha256` of the written bytes, and `check`: the
status and reasons the receipt check gave (`diagnostic`, or `blocked` for a
well formed receipt whose content cannot satisfy its phase, such as a failed
build check; that is evidence too and is kept). Exit 3 and `"status":
"invalid"` come with `reasons`, and nothing is written; this includes a
`receipt_id` already in use, since no receipt is ever replaced. The command
records nothing in the ledger and advances no phase. A builder seat is given
the toolkit command to run it with in every prompt (`toolkit:`, the host's
interpreter and the package's `entry.py`, both by full path), so the installed
`sortie` need not be on its `PATH`; `sortie` above stands for that command.
The `check` it reports includes the ledger, as `receipt validate` does: a
receipt ID or phase slot already consumed, or a ledger that cannot be read,
is `invalid` and nothing is written. The host's own check when the driver
consumes the receipt remains the one that decides.
Both functions use a fixed finite default budget;
the writer's temporary file stays beside the destination. Disk errors are refusals;
a completed rename followed by an fsync failure may leave a recorded file, so
retry by inspection rather than assuming no write occurred.

## Common and phase-specific bindings

Every receipt requires `version`, `receipt_id`, `shift_id`, `task_id`, `request_id`,
`role`, `phase`, `round`, `brief`, and `data`. The caller's expected context binds
every common field except receipt ID. Initial round is 0, repair rounds are 1–3.
Plan and build receipts are the `builder`'s; a review receipt has the role
`reviewer` and is written by the host, and no other phase has that role;
acknowledgment/stop can be `driver` or `builder`.

An artifact is `{ "path": "logs/result.txt", "sha256": "<64 lowercase hex>" }`.
Artifact paths are relative to the shift root. Their existence, regular-file
type, containment and content hash are checked. Symlinks, traversal, absolute
paths and special files are refused. A target is either
`{ "kind": "commit", "commit": "<full object ID>" }` or
`{ "kind": "plan", "artifact": <artifact> }`. Commit objects must exist in the
target repository; moving refs and tags are refused. No fetch occurs.

| Phase | Required data and caller context |
| --- | --- |
| `plan` | Plan artifact, summary and `answers`; expected target binds its path and digest. |
| `build` | Exact candidate, touched source names, commands, bounded timeouts, expected/actual exit statuses and output artifacts; empty checks require `no_checks_reason`; `answers`. |
| `review` | The host's record of the review: target, the task's base commit, implementer family, the signed reviewer, the limit of one run, the outcome, the earlier findings it had to answer, and every attempt. Expected context binds target, required finding IDs, implementer family and the signed reviewer. |
| `acknowledgment` | `acknowledged`/`not_acknowledged`, evidence, reason; expected target is null. |
| `stop` | `stopped`/`unknown`, `seat`/`toolkit` scope, evidence, reason; expected target is null and `stop_scope` must match. |

Touched source names are lexical repository-relative Git paths and can name
deleted files; their claimed change is not independently attested by the validator.
Build checks satisfy the phase when actual exit equals the explicitly recorded
expected exit. This supports intentional failing-baseline checks without treating
unexpected failures as passes. Review is still required before task acceptance.
An acknowledgment is not progress, and a toolkit stop is not seat cessation.

## Answers to findings

After a review that asked for changes, the builder's next plan or build
receipt carries `answers`: one entry for each finding it was handed in
`briefs/findings-REQUEST.json`, and no other. Each is `{id, disposition,
reason, evidence}` with disposition `fixed` or `rejected`, a reason, and an
artifact of the shift or null as evidence. The expected context lists the
ids as `required_findings`. A receipt that leaves one out, answers one twice
or answers one it was not handed is refused. Outside a repair round the list
is empty. An answer is the builder's claim; the next review is given it and
says for each earlier finding whether it is resolved.

## The review receipt

The reviewer is whatever the developer signed, never a model compiled into the
toolkit. The grant's `review_policy` and its reviewer seat name the builder
model and family and the reviewer's agent, model and family. The lifecycle
copies them into the expected context as `implementer_family` and `review:
{agent, model, family, same_family, choice}`, and into `current-context.json`
(`builder`, `reviewer`) for the builder to read at every stage. The same file
carries `stop_expected`, the context a stop receipt of the builder seat is
checked against (`stop-evidence`), beside `expected` for the open phase, and
`findings`, the file with the findings the next receipt has to answer.

The review receipt is the host's record of what it ran and what it took from
the output. Nothing in it is a seat's claim, and no seat can write one:
`sortie receipt write` refuses a receipt of phase `review` or role
`reviewer`, and `consume` at a review stage takes nothing (`blocked` while a
review is open, `invalid` when none is). Its `data`:

- `target`, and `base`, the commit the repository was at when the task was
  briefed (the review is of the whole change from there);
- `implementer_family` and `reviewer`, which must equal the expected context;
- `limit_seconds`, the signed limit of one run;
- `prior`, the earlier findings this review had to answer, as `{id, severity}`;
- `outcome`: `ACCEPT`, `CHANGES` or `FAILED`;
- `attempts`, one or two, numbered in order. Each has the directory the run was
  given, the SHA256 of its instructions, the run's marker, the argv really
  started, start and end times, `elapsed_seconds`, `exit_status`, `ended_by`
  (`exit`, `signal`, `timeout`, a stop, or `not started`), `stdout` and
  `stderr` as artifacts of the kept output, `output_dropped_bytes`, `status`
  (`complete` or `failed`), `verdict`, `reasons`, `record` (the reviewer's
  object as parsed) and `repository` (`unchanged`, `changed` or `not
  checked`). `unchanged` means the commit, Git's status lines and the
  listing of `receipts/` were the same before and after the run; it does not
  say that no byte of an already modified file changed.

What the check makes of it, whoever presents it:

- only the last attempt can be complete; a complete attempt ended on its own
  with status 0, kept all its output, has its command and marker recorded, no
  reason for failure, and left the repository unchanged;
- the kept standard output of a complete attempt is read again and run
  through [the review contract](review.md) with the attempt's marker, the
  target and the earlier findings, and must give exactly the verdict and
  record the receipt states;
- a failed attempt has a reason and neither verdict nor record;
- the outcome is the last attempt's verdict when that attempt is complete,
  and `FAILED` otherwise.

`ACCEPT` satisfies the phase. `CHANGES` and `FAILED` are valid evidence that
blocks: they are recorded and advance nothing.

Same-family review is settled at signing (see
[the reviewer seat](agents.md#the-reviewer-seat)) and is carried as
`review.same_family` and `review.choice`. The driver cannot name a substitute
reviewer: no command has such a field. A signed reviewer that cannot
be started counts as a failed attempt; when a review fails twice, the task is
blocked for the developer. The reviewer's
identity and argv are what the host started; the model that answered behind
that CLI is not attested by anything here.

A reviewer started by the host runs no code. Reproductions stay with the
builder, as build checks with their output kept.

Limits of this check, said plainly: it rests on files in the shift. A process
that can write the kept output, the receipt and the ledger, and knows a run's
marker, can make a receipt this check takes. The host's own guard against
that is narrower: it takes a verdict only from output it read back through
the file it opened itself, it compares `receipts/` before and after each
run, and it moves a task only on the review receipt it built itself in that
process, never on one it finds on record.

## Format, safety and limits

Packaged `receipt-v3.schema.json` and `expected-v3.schema.json` describe closed
JSON structures using the [JSON Schema 2020-12 validation vocabulary](https://json-schema.org/draft/2020-12/json-schema-validation).
The standard-library checker implements only the keywords used by these packaged
schemas; it rejects unsupported keywords and external schema references. It is
not a general JSON Schema implementation. Wire JSON additionally forbids duplicate
keys, floating-point number syntax (including `1.0`), and non-finite values.
Cross-field bindings, file checks, provenance, phase satisfaction and duplicate
consumption are semantic checks beyond the schemas.

Context/receipt/ledger limit is 1 MiB each; artifact limit is 16 MiB; total bytes
read per operation are capped at 64 MiB. Arrays and strings are bounded in the
schemas, the ledger holds at most 4096 entries, Git calls have at most five seconds,
and lock waits/read loops honor the operation deadline. Regular-file reads use
no-follow descriptors and refuse detected changes during a read. Kernel filesystem
calls still depend on a functioning local filesystem. A later file mutation or
forged host evidence is not ruled out by a successful validation.

Every receipt of a new shift is version 3, and validation takes no other.
Version 1 receipts (fixed three-name family list, driver-supplied fallback) and
version 2 receipts (the review made by the builder through a review script,
with its invocations and reproductions recorded by the builder) are no longer
written or accepted. `receipt-v1.schema.json` and `receipt-v2.schema.json` are
retained only so a shift report can still read the receipts of an older shift;
it names such a shift's reviews as made through that script. The command
`sortie receipt check-review` went with version 2.
