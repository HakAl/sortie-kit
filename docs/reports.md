# Shift reports

The foreground host writes `closeout.json`, `report.json` and `report.md` during
bounded finalization. An admitted start that fails before host creation also
attempts a local report. Writing a report requires only retained local files: no
network, LLM, Git command, test execution or signature verification.
What the report says about the repository comes from two looks the host took
while the shift ran and kept in `closeout.json`; `sortie report` on an old
shift reads those and looks at nothing itself.
The reporting lock and writes remain inside the finalization budget. An expired
budget or unavailable output/lock can prevent publication; reporting never
extends the signed shift or authorizes another task.

A developer can summarize an existing shift, including partial preparation:

```sh
sortie report --shift PATH/.sortie/shifts/SHIFT --for 5s
```

This local diagnostic is capped at five seconds. Exit 0 means both report files
were emitted, including a partial report. Exit 3 means unsafe/unavailable output
or an exhausted budget; usage errors exit 2. It does not admit or resume work,
consume receipts, run queued checks, or install agent permissions. Report
commands remain host/developer-owned.

`report.md` is the file for a person. In short prose and lists it says how the
shift ended and how long it ran, repeats the direction, and for each task says
whether it was accepted and at which commit, what each consumed receipt
reported (plan, review verdict, tool, model and findings, build commit and
checks), each builder turn with its exit and log, what was not done and why,
what was left uncommitted in the repository, which driver commands were answered `blocked` or `invalid`, any problem with
the evidence, what the kept grant says about starting the seats, what the
host saw of its processes, and who is next. The grant is called "kept", not
"signed": the report does not check a signature, and when no `grant.json.sig`
lies beside `grant.json` it says so. Text that came
from a seat is shown as one line of inert text: line breaks and control
characters become spaces, Markdown and HTML characters are escaped, and
long text is cut with a pointer to `report.json` (600 characters for most fields). A URL in
such text stays plain text, though a Markdown viewer may still make it
clickable. If the readable part cannot be built from what was kept, a short
`report.md` says so and points at `report.json`.

`report.json` is the machine file, schema identifier `sortie-kit.report.v2`. It includes:

- Retained grant hash, window, actions, seats and review choice, with a
  `provenance`: `retained grant; signature not reverified` when a signature
  file is kept beside it (its validity is not rechecked),
  `retained grant without a signature file; not shown to be signed` when none
  is, and `unsigned preparation only` for a draft.
- The direction: its SHA256, whether that matches the grant, and its first 2000 characters.
- Host-observed start/end, monotonic elapsed whole seconds (rounded down),
  reason, grant/direction pins and stop provenance from `closeout.json`.
  Missing observations stay unknown; grant times do not substitute for them.
  The start and end are stamps of the wall clock, and the elapsed seconds are
  counted on a clock that stands still while the machine sleeps. When the
  stamps are more than a minute further apart than the seconds counted,
  `report.md` says so in the "Ran for" line: the machine slept, or its clock
  was changed, for the difference.
- `actual_window.wake`: the closeout's `wake`, the host's hold against idle
  sleep (see [the lifecycle](lifecycle.md#keeping-the-machine-awake)), or no
  such entry for an earlier shift. `report.md` says under Outcome whether the
  machine was kept awake for the whole shift and by what means, and when not,
  the refusals and the lapses the closeout kept (eight of each at most, with
  the number of the rest), each refusal with its return code when the system
  gave one. `held` in that entry may be `null`: the system could not be
  asked whether it still held the assertion, or gave nothing for it, and the
  report then says that whether it was held when the report was written is
  not known. A report written at once by a command that closed or blocked the
  shift, before the host's own, has no such entry and no line on it.
- `open_findings`: the findings that a review which ended in ACCEPT left
  open and that no later review marked resolved: `count` (how many there
  are), `items` (those listed, at most 256, each with `task`, `review`,
  `stage`, `round`, `receipt`, `id`, `severity`, `location`, `claim` and
  `earlier`), `more` (how many of the count are not listed: the report keeps
  16 findings of one review) and `unread` (accepted reviews whose receipt
  could not be read). There are two kinds. The accepting review's own
  findings have `earlier` false. An earlier finding that the accepting
  review was asked about and answered as not resolved, which an ACCEPT may
  do for a minor one, has `earlier` true; its `location` and `claim` are
  those of the earlier review receipt of the same task and stage that made
  it, and when that receipt cannot be read or the report did not keep that
  finding of it, `location` is `null` and `claim` is the reviewer's reason. An ACCEPT moves the task on whatever was left
  open and nobody is asked to act on it, so `report.md` gives the number
  under Outcome and lists each under "Findings left open on accepted work".
  A finding counts as resolved when any later review of the same task and
  stage, whose last attempt completed, was asked about it and answered that
  it is; the host hands on no finding of an ACCEPT today, so each one is
  listed. `count` is `null`, and the report says "not known", when the
  driver's record of the tasks could not be read in full or the report was
  cut to its short form at the output limit. `count` is of the reviews
  whose receipts could be read: with `unread` above zero it is not the whole
  number, and `report.md` says for how many reviews it is not known. The
  whole entry is `null` when the kept grant has no reviewer seat (an earlier
  shift, whose reviews were the builder's reports, or a shift that was
  prepared and never signed) or could not be read.
- `actual_window.repo`: the closeout's `repo`, the host's two looks at the
  target repository (`start` and `end`, each with `head` and `changes`; see
  [the lifecycle](lifecycle.md#what-was-left-in-the-repository) for what a
  look covers and misses). `report.md` gives the two HEADs in one "Repository
  HEAD" line under Outcome, and a second line when the look at the start saw
  uncommitted paths: the repository was not clean when the shift began, and
  whether a commit made during the shift holds those paths. That comes from
  `repo.held`, a third look the host makes at its end when the first saw
  uncommitted paths: `{paths, complete}`, the paths a commit between the
  HEAD at the start and the HEAD at the end added or changed, or `{error}`.
  The report says `may hold them` only when there is no such look, it
  failed, or it was not finished (`complete` false: the look at the start
  was cut, or the Git command was). Commits on a branch that HEAD is not on
  at the end are not looked at.
- `leftovers`, present when the closeout has `repo`: `new` holds the `end`
  rows that `start` did not have, `before` counts the `end` rows that were
  already there, and `known` is false when the look at the end failed or was
  skipped. `since_start` is false when the look at the start failed or was
  cut at a limit: the `end` rows it did not have are in `new` (all of them
  when it failed), and the report says that whether they were there before
  is not known, since a path a cut look left out can show up at the end. `complete` is false when a look that was
  compared was cut at one of its limits. `report.md` lists the rows after
  the tasks under "Not done": up to 50 paths with their state, or one line
  saying nothing new was seen, or that the repository could not be read and
  `git status` should be run by hand. When `complete` is false it says the
  look was cut and to run `git status`, and never that nothing was left. A
  closeout from before this field has no `leftovers` and the report says
  nothing about the repository.
  `worktrees`, present only when there are some, names up to 20 linked Git
  working trees registered under the repository's `.sortie/` at the end,
  as paths from the repository root. A builder makes them under the shift's
  `scratch/` for its checks; receipts point into them, so the host removes
  none. `report.md` names them under "Not done" and says how to clear them:
  remove `.sortie/`, then run `git worktree prune`. Like the rest of the
  look, this is what Git answered and not evidence; when Git did not answer
  in the time left, none are named.
- The `Tasks` line under Outcome counts briefed tasks and says so (`1 accepted
  locally of 1 briefed`). The host cannot read a direction, so what the
  direction held beyond the briefed tasks comes only from the driver: the
  `not_started` list it may give with `finish`, kept in `report.json` as
  `not_started` (`null` when none was given). The line ends with `the driver
  names K more as not started`, `the driver says the direction held no more`
  or `whether the direction held more is not known to the host` (with no task
  briefed the first two read `names K as not started` and `held nothing to
  brief`). "Not done"
  lists each named item as `Not started, as the driver stated it: NAME:
  REASON.` It is the driver's statement and not something the host observed.
  When "Not done" would otherwise be empty (no briefed task left undone, no
  named item, no problem with the evidence), it says `No briefed task is
  recorded as left undone.` followed by which case holds: the driver stated
  that nothing in the direction was left unstarted, or the host does not know
  whether the direction held more. So an empty section is never read as "the
  whole direction was done". A `not_started` kept in the state in another
  shape is not shown and is listed under "Problems with the evidence".
- Under "Not done", a task that was not accepted is followed by the owner
  recorded on the task while the shift ran (the `next_owner` the driver gave
  with its brief or last repair, usually `builder`). That is not the "Next"
  line under Outcome, which says who has everything now that the shift is
  over (the owner named with `finish`, otherwise `developer`). The line says
  which is which.
- Tasks, candidates, repair rounds, exact review targets, tool/model/arguments,
  raw-output references, verdicts and reproductions, plus reported tests and
  their log references. Receipt bodies remain attributed claims.
- Command results, refusals and builder turn rows (process start, end, outcome,
  exit status and log path; never acceptance). A review that asked for
  changes (verdict not ACCEPT or a finding left open, every review run
  complete) is answered `blocked` by the host like a refusal, and `report.md`
  tells the two apart: under the task it says the review asked for changes
  and which later round, if any, satisfied the phase, and "Driver commands"
  counts such answers apart from commands that were blocked or invalid.
  Any other returned receipt (a duplicate, a failed review run) is not
  called a repair round. A task whose turn is still recorded as running has
  `turn_running: true`. For a headless builder with no stop receipt,
  `report.md` says how many of the task's started turns the host saw end
  (`N of N`), and says the stop is not known only for the others;
  `cessation` in `report.json` is unchanged;
  input SHA256/byte counts, damaged or missing evidence, blockers and next owner. A blocker row carries the `stage` at which its
  task stopped; `report.md` names that first and any reason still recorded
  second. A repair reason is cleared when a later receipt advances the task,
  so an old one is not read as the cause. A receipt the driver returned (a
  CHANGES review, for example) is kept in the consumed ledger with
  `satisfies_phase` false, which reserves its phase slot. The report's
  ledger check looks for a satisfying entry, so it applies only to receipts
  recorded as satisfied; a returned one is described as returned, and a
  missing or changed file is still reported as a problem either way.
- Task and acceptance metrics, explicit unknowns, and comparison deltas.

Version 2 dropped the `journal` section and the `journal_rows` metric of
version 1. `events.jsonl` is no longer written; a shift prepared by an
earlier version may hold one with a single preparation row, which the report does not read
or describe as coverage of the shift. The
record of what happened is `driver-state.json`: commands and their answers,
builder turns, and consumed receipts. A version 1 report is not accepted as
`--previous`.

Recorded acceptance is corroborated only against intact, schema-valid,
identity-bound receipts, consumed-ledger entries and a matching completed
ACCEPT review of the recorded candidate. Current-round build/review evidence
must remain intact. This is a local evidence check, not code-quality review or
independent proof of execution. Historical rejected reviews remain visible.
Changed/missing files and stale reviews leave uncertainty and blockers. A
reported seat stop is attributed to its retained receipt; toolkit finalization
alone does not prove any developer-owned seat stopped, nor that the finalizer
itself has exited.

Missing totals are
unknown, never zero. Input bounds are 1 MiB per document, 16 MiB per artifact,
64 MiB total, 512 cached inputs, 128 tasks and 256 receipts.
Detail retention is capped at 600 KB,
and each output at 1 MiB. Extraction reserves publication time. Limits produce
explicit partial reports when output can still be published.

The 600 KB are counted over what the report repeats of the kept files: the
direction, the closeout, the driver's list of what was not started, then for
each task the data of its receipts, its turns, reviews, any send records and
its fields, and last each driver command. What does not fit is left out and
listed once for each place under "Problems with the evidence" as left out at
the report's own size limit; the file was still read and checked. The status
is still `partial`, since the report is not whole, but when every problem
listed is of that kind `report.md` says that the report reached its own size
limit and that nothing that was read was found missing or damaged. It says
that some evidence is missing or damaged when a problem of another kind is
listed, and a problem of another kind is never dropped from a full list to
keep one of these. When the fields of a task do not fit, the reading stops
there and the report keeps the wider words. If an output would pass 1 MiB
with all the details the bound allows, the report is made once more with
80 KB of them, from the bytes already read and with no file read again,
before it is cut to the short form.

The two output files are individually atomic. Markdown embeds the SHA256 of
its corresponding JSON; check it before combining them. An interrupted second
write can leave a mixed pair, which must not be treated as coherent.

First-shift deltas are unavailable. To compare an earlier report, explicitly
retain a copy inside the current shift and supply, for example,
`--previous logs/prior-report.json`. The input must identify another shift in
the same repo with an older recorded grant start and compatible schema. Only
numeric metrics with coverage on both sides produce deltas. Its hash and
attribution are retained; no automatic cross-shift or home-directory search
runs, and the copied report is not independent authority.

The report fixtures prohibit subprocess and network operations while
finalizing. `tests/check_install.py` repeats preparation-to-report and the
synthetic report fixtures against an installed wheel with all packaged assets.
It runs no Codex/Claude binary, signing session, keychain or privacy probe.
