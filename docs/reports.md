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
checks), each builder turn with its exit and log and, for a turn or a run of the reviewer whose CLI ended by itself
and not well, the last lines that CLI wrote and what a person can change, the notes the driver left, what was not done and why,
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
- `last_activity`: the host's own stamps of what it last did for the driver.
  `command` is when the last driver command was recorded, `commands` how many
  are on record, `work` when a builder turn or a review attempt the host ran
  last ended, and `running` whether one is recorded as started with no end.
  A time is `null` when there is none; `command` is also `null` when a record
  has no time, as in a shift kept by a host from before the time was
  recorded, because which command was the last is then not known.
  `work_known` is `false` when the kept state does not tell when everything
  the host ran ended: a turn or an attempt with no readable end, a turn or a
  review kept as lost, or a review with no attempt. A task kept from before
  turns or reviews were recorded has none, which is known. `work` is then
  still the latest end on record, and an end that is not known can only be
  later, so a shift whose `work` is within five minutes of its end gets no
  line. When the shift ended five
  minutes or more after the later of `command` and `work`, the Outcome has a
  line in bold after "Ran for": `Last driver command: recorded at TIME, N
  before the end.` It is followed by one of three statements: when the last
  turn or review ended and that nothing is on record after it; that no turn
  and no review ended in that time; or, with `work_known` false, that when
  the host last ran something is not known. A shift of five minutes or more
  with no command at all says `No driver command was recorded in the N this
  shift ran`. The line ends by saying that the host does not know why. A
  driver that gave up, one that waited for a person and one whose process
  hung all leave the same record: a host that went on to the end of the
  window with no command to serve. The line is the only place the report
  shows it; "How it ended" gives the host's own ending. Five minutes is the
  bound because a driver that works reads a result and sends its next
  command well within that, and a wait on a turn or a review is not counted.
  The times in the line are stamps of the wall clock, as the "Ran for" line's
  are. When that line says the machine slept or its clock was changed, this
  one says for how long and that how much of the quiet time the host was
  running is not known. No line is given when the last command's time is not
  known, when a turn or review is recorded as still running, when the host
  counted less than five minutes for the whole shift, or when the report was
  cut to the short form.
- `actual_window.temp`: the closeout's `temp`, the directory the host made
  for the temporary files of builder turns and reviews (see
  [the lifecycle](lifecycle.md#the-temporary-directory-of-builder-turns-and-reviews)),
  or no such entry for an earlier shift. `report.md` has one line for it under
  Outcome: the path, its length in bytes and that the host removed it with so
  many entries in it, with a sentence more when it could not be seen
  afterwards that what went was the host's directory; or, in bold, that it was left, where the host made it,
  why, how many of its entries went, and to look before removing anything; or, in bold, that the path was replaced, which tells
  nobody to remove what is there now; or, in bold, that none was made, why, and that turns
  and reviews kept their temporary files in the shift's `scratch/`, where
  tests that bind Unix sockets or must run outside a Git checkout can fail
  for that alone. A report written while the shift ran says the directory is
  in use. An entry that cannot be read is said as not known.
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
  `stage`, `round`, `receipt`, `id`, `severity`, `location`, `claim`,
  `earlier` and `task_accepted`), `more` (how many of the count are not
  listed: the report keeps 16 findings of one review), `unread` (accepted
  reviews whose receipt could not be read) and `unaccepted` (how many of the
  count are on a task that was not accepted locally). A review that ended in
  ACCEPT is not an accepted task: the review of a plan accepts a plan, and
  the shift may end before the task is built. `task_accepted` is true only
  for a task the report counts as accepted locally. There are two kinds. The accepting review's own
  findings have `earlier` false. An earlier finding that the accepting
  review was asked about and answered as not resolved, which an ACCEPT may
  do for a minor one, has `earlier` true; its `location` and `claim` are
  those of the earlier review receipt of the same task and stage that made
  it, and when that receipt cannot be read or the report did not keep that
  finding of it, `location` is `null` and `claim` is the reviewer's reason. An ACCEPT moves the task on whatever was left
  open and nobody is asked to act on it, so `report.md` gives the number
  under Outcome and lists each under "Findings left open by reviews that
  ended in ACCEPT"; the Outcome line says how many are on a task that was
  not accepted, and each of those is marked "(task not accepted)".
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
- `repeated_findings`: the findings that two or more reviews running
  answered as not resolved. A review in a repair round is asked about the
  earlier findings the review before it handed on and answers each as
  resolved or not. A run is one finding of one task and stage (the plan or
  the candidate, counted apart) answered as not resolved by reviews that
  follow one another, and it is counted from two such answers on. The review
  that raised a finding does not answer it, so a run of two is three reviews
  that held it. The entry has `count`, `items` (those listed, at most 256,
  each with `task`, `stage`, `id`, `severity`, `times`: how many reviews
  running answered it so, `reviews`: the numbers of those reviews, each
  `null` where the driver's record of the task gives none, `resolved` and
  `task_accepted`), `more` (how many of the count are not listed) and
  `unread` (review receipts that could not be read). `resolved` says how the
  run ended: `true` when a later review answered the finding as resolved,
  `false` when none did (the stage had no further review, or the finding
  was no longer asked about), and `null` when the run ended at a review
  receipt that could not be read, so that what that review answered, and
  whether the run went on in it, is not known. A run that ended as resolved
  on a task that went on to be accepted is still a fact of the shift and is
  counted, with `resolved` true and `task_accepted` true. `report.md` gives
  one line under Outcome, "Findings answered as not resolved by two or more
  reviews running": the number, and for at most eight of them the task and
  whether it was accepted, the stage, the finding with its severity, how
  many reviews running and which, and how the run ended; `none` when there
  is none; and with `unread` above zero never `none` but for how many
  reviews it is not known. The reviews are named by number only when the
  number of every one of them is known. Of those beyond the eight the line
  says how many `report.json` lists and how many only the review receipts
  have. It is computed from the review receipts, as
  `open_findings` is, and not from the driver's record: an answer counts
  only from a review whose last attempt completed and that ended in `ACCEPT`
  or `CHANGES`, and only for an id the host asked that review about; a
  review that failed is passed over and neither adds to a run nor ends it.
  An `ACCEPT` may answer an earlier minor finding as not resolved, and that
  answer counts like any other. What the line does not count or say: who was
  right, the builder or the reviewer; a finding that a review raised again
  under another id, which is another finding to the host; a finding no
  longer handed on because a review left more than 64 to answer, whose run
  ends there; and a run through a review receipt that cannot be read. The
  host says the same to the driver while the shift runs, after each review
  that ends in `CHANGES`, in the task's reason and `last_review.detail` (see
  [the lifecycle](lifecycle.md)); that count is made from the driver's
  record, and the two agree on every such review where the receipts can be
  read. After an `ACCEPT` the host says nothing of it, since no repair
  follows, and this entry still counts the answer. `count` is
  `null`, and the line says "not known", in the same cases as for
  `open_findings`, and the whole entry is `null` in the same cases. A report
  kept from before this entry has no such key and no such line.
- `requirements_unchecked`: what a brief asked for that the review which
  accepted the task answered `not_checked` (see
  [the review contract](review.md)). It is read from the kept review
  receipts: a report made again for a kept shift, by this build or a later
  one, counts the same answers. The host takes an `ACCEPT` beside such
  an answer, so the report counts them: `count`, `items` (those listed, at
  most 256, each with `task`, `review`, `receipt`, `asked` and `where`),
  `more` (how many of the count are not listed), `unasked` (accepted reviews
  of a candidate that were asked for no list, because a build from before
  the list ran them) and `unread` (accepted reviews whose receipt could not
  be read or relied on: damaged, not the kept `ACCEPT` the acceptance rests
  on, silent on which was asked, or without the list it says it has; and
  those whose list holds an answer that is none of `met`, `not_checked` and
  `disclosed`, which a kept receipt can hold and an answer the host takes
  cannot: what such a list does say is counted and listed). `report.md` gives the
  number under Outcome as "Requirements accepted unchecked" and never says
  none while `unasked` or `unread` is above zero. Under each task that a
  review accepted it says first how many requirements that review answered
  and how many of them as met, as not checked and as said to be not
  delivered (and how many otherwise, if any), since an `ACCEPT` beside
  requirements nobody checked is honest only when the developer sees it;
  then it lists every requirement of that review with its
  status, as the reviewer worded it, cut to 200 characters; for a review
  kept from before the list it says that no requirement list was asked, and
  for one whose receipt could not be read, is not the kept `ACCEPT` or
  holds no list, that it is not known and why; a list with an answer the
  report does not know is shown as a list, with that answer among those
  counted as otherwise. Only the
  review that the driver's record of the acceptance names is gone through
  (`accepted_receipt` of the task, beside `review_matched`), and it counts
  as `unread` when that receipt is not among the task's receipts or is not,
  as kept, the host's consumed `ACCEPT` of the candidate. A plan
  review answers no list, and the list of a review that asked for changes
  is in its receipt. `count` is `null` and the whole entry is `null` in the
  same cases as for `open_findings`. Each kept attempt of a review receipt
  carries `requirements_asked` (`null` where the attempt has no object or
  one of a schema this build does not know) and `requirements` (`null`
  unless a list was asked). A requirement answered `met` is the reviewer's statement and was
  not checked by anything. A kept finding of a commit review in which each
  finding names its requirement carries `requirement`, a position in that
  list or `null`; one of an earlier review has no such key. A kept attempt
  carries `evidence` where its receipt has it as one of `shown`, `no_time`,
  `late` and `failed`, and no such key otherwise. When the attempt that gave
  a review its outcome was not shown the builder's receipts, `report.md`
  says under that review that the host did not show them and why, and for a
  review of a candidate that answers of `not_checked` in it may rest on
  that; the list under a task of what its accepting review answered begins
  with the same statement; and the line of a failed attempt says it of that
  attempt. Nothing is said of evidence for an attempt whose receipt does
  not say.
- `said`, on a builder turn and on a kept attempt of a review: the last lines
  the seat's CLI wrote before it ended by itself and not well, as a list, and
  no such key otherwise. A turn's lines are the host's, kept in
  `driver-state.json` when the turn ended. An attempt's lines are not in the
  review receipt: the report makes them, by the host's own rule, from the two
  logs the receipt names, and only from bytes that are the ones the receipt
  gives, read from the log or, where the log is gone or changed, from the
  host's kept copy. Where neither holds those bytes nothing is quoted. Nor is
  anything quoted of an attempt whose log the host found written to by
  something else while the review ran, or of one that printed more than the
  log holds: such a log is not what the reviewer printed last.
  `report.md` shows the lines under the turn or the attempt as inert text,
  marked as the CLI's own words, with the sentences that name what a person
  can change; those sentences stand there without lines too, where the CLI
  ended by itself and not well and nothing is quoted. Under an attempt that the host ended at its limit it shows no
  lines and says how long one run may take, and names `--review-limit` and
  the reviewer's model. Beside `Driver process:` it says, for a driver whose
  CLI ended by itself and not well, that the host kept nothing of what the
  driver wrote, and names what can be changed. What is quoted is bounded and
  passed through masks for what has the shape of a credential; the masks are
  a filter by shape and no proof that no secret is shown, see "What a CLI
  wrote last" in [the lifecycle](lifecycle.md). Read those lines before you
  pass a report on.
- `requirements_disclosed`: what a brief asked for that the review which
  accepted the task answered `disclosed`: not delivered, with the brief, as
  that review read it, allowing that to be said in place of the work. The
  host takes an `ACCEPT` beside such an answer: `count`, `items` (at most
  256, each with `task`, `review`, `receipt`, `asked` and `where`, the place
  where it is said) and `more`, from the same one review per task as
  `requirements_unchecked`. `report.md` gives the number under Outcome as
  "Requirements accepted as said to be not delivered", lists each under its
  task as `disclosed`, and while `requirements_unchecked`
  has `unasked` or `unread` above zero it does not say none and says beside
  a number that it is not known for those accepted reviews. `count` is `null` and the whole
  entry is `null` in the same cases as for `open_findings`.
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
- `head_unaccepted`: `null`, or `{head, accepted, last}` when HEAD moved
  during the shift and is, at the end, not the candidate of a task accepted
  locally. `accepted` is how many tasks were accepted locally. `last` is the
  task and commit of the accepted task whose record moved last
  (`last_progress` in `driver-state.json`, in whole seconds; one task is
  open at a time, so that is the one accepted last), or `null` when none
  was accepted or two moved in the same second. `report.md` then has one
  more Outcome line, "HEAD when the shift ended is not a commit this shift
  accepted", with that commit, or saying that none was accepted, or that
  which was the last is not known: a builder's commit that no review accepted is on top, as when a
  shift ends in a repair round. It is `null` when HEAD did not move, when a
  look failed, when the report did not take the closeout, and when not
  every task of the driver's record could be read. Only the commit at HEAD is compared; whether it holds an accepted
  commit in its history is not looked at.
- `stop`: `{by: "developer"}` when the shift folder holds the `STOP` marker
  with the very bytes `sortie stop` writes, else `null`. That marker is whole
  evidence of a withdrawal and no problem with the evidence: it is not
  listed under "Problems with the evidence" and does not make the status
  partial. "How it ended" keeps the host's reason and adds who withdrew the
  shift. The host has one reason for a withdrawal, a changed source and a
  file it could not read (`admitted scope unavailable, changed, or
  withdrawn`); beside that reason the line says that the developer withdrew
  the shift and that the reason does not say which of the three the host
  saw. Beside any other reason it says only that `sortie stop` was also
  run, since the command may have been run after the host had ended. A
  `STOP` file with any other content withdraws the shift as well; whose it
  is is not known, and it stays listed as a problem.
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
- `driver_notes`: what the driver recorded with its `note` command, in the
  order it was recorded. Each item has `kind` as the driver gave it (`decision`:
  something it says it decided on its own; `question`: something it says it
  could not settle before it went on), `text`, `task_id` (the briefed task it
  named, or `null`) and `at`, the host's time when it took the note. The
  Outcome has the line `Notes the driver left:` with the count of each kind,
  or `none.`, and when there are notes `report.md` lists them under "Notes
  from the driver", before "Not done". Each is the driver's own statement:
  the host kept the text as written and checked none of it, also not whether
  a decision was within the direction, and a note has no answer in the
  shift. The section says so. A shift kept by a host from
  before this field has no notes and reads `none.` Notes kept in another
  shape are not shown and are listed under "Problems with the evidence";
  `driver_notes` is then `null` and the line reads `not known`, as it does
  when the notes were left out at the report's size limit or the report was
  cut to the short form.
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
  second. A task that is not accepted and stands at a stage that waits for
  a receipt of the builder (`ack`, `plan`, `build`) may have `untaken`: the
  receipts in `receipts/` that no task record names and that are, as the
  files are now, a builder's receipt of this shift, this task, its open
  request, its round and that phase, at the path a receipt of its id has
  (`receipts/ID.json`) and with no entry in the consumed ledger (at most 4,
  each with `path`, `sha256`, `stage`, `round` and `data`). The report looks
  for them last, when everything of the shift was read: at most 64 unnamed
  files, none over 1 MiB and 4 MiB of them together, where a file larger
  than what is left of the 4 MiB is passed over and the bytes of a read
  that failed count too, counted against none of the bounds on the
  shift's own files. Only a receipt that is named is listed among the inputs, and one
  whose `data` does not fit the bound on details is named with `data` null.
  The builder wrote such a receipt and the driver never consumed it, so the
  host checked nothing of it and no review of it ran. `report.md` then says
  the task stopped at "a build was handed in and not taken up" with the
  path, where it would have said "waiting for the build", lists the receipt
  under "Handed in and not taken up" as the builder's own report, and the
  blocker row carries the paths as `untaken`. A file there that cannot be
  read as such a receipt is passed over and is no problem of the evidence:
  nothing of the shift rests on it. A repair reason is cleared when a later receipt advances the task,
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
Changed/missing files and stale reviews leave uncertainty and blockers.

A file a receipt names is read at its path first. When it is gone from there,
or holds other bytes, the host's own copy from when it recorded the receipt
(`receipts/.kept/SHA256`, see [receipts](receipts.md)) is read, and if that
holds the bytes the receipt gives, the receipt is intact: its entry in
`report.json` lists the path under `kept`, and `report.md` says under a
receipt the builder wrote that the path was written over or removed and where
the copy is (under a review receipt of the host's it does not; `report.json`
has the list). The copy is looked for only when the path is gone or holds
other bytes: a path that could not be read at all, for a limit of the report
or because it is no regular file, is listed as a problem and is neither
called changed nor read from a copy. A regular file larger than any file a
receipt may name (16 MiB) is not read either, but its size says it is other
bytes, so the copy is looked for. A copy that is there and does not hold
its sum is listed as a problem under its own name. With
no such copy, as in a shift from before the host kept them, the path is listed
under `changed` and as a problem, each check whose output it was is marked on
its own line, and the Result of a task whose acceptance this leaves unbacked
names the receipt that fails. `kept` and `changed` are lists in every receipt
entry, empty when there is nothing to list. A task with a recorded acceptance
has `review_matched`: whether the review receipt the acceptance names is,
kept and consumed, the ACCEPT of the candidate. The Result calls a review
receipt intact only when this is true, and says so when it is not. A path read
from the host's copy is read twice, at its path and at the copy, and both count
against the limits on what the report reads.

A reported seat stop is attributed to its retained receipt; toolkit finalization
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
its fields, then the driver's notes, and last each driver command. What does not fit is left out and
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
