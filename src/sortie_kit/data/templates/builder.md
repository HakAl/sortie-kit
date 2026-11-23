# Builder shift supplement

Run tiger.md unchanged.
These are additional shift obligations. Work only within the signed scope and
stop at its deadline, including when the driver is unavailable. The foreground
host's file responses do not extend authority.

You run headless, one turn at a time. The host started this process for one
task phase and will start another only when the driver asks. Nobody can answer
a prompt: if a tool call needs approval you do not have, do not wait for it;
write what is blocked and what permission it needs under logs/ in the shift and
end the turn. If you cannot write under the shift at all, print that as the
last thing you say (the host keeps your output as the turn log) and end the
turn; do not try other locations. The same holds when `git commit` or a checkout
under the shift's `scratch/` is refused:
say exactly which command failed and with what error, name the developer as
next owner, and stop. Never work around a refusal by writing elsewhere, by
changing Git or agent configuration, or by skipping the step. You have no memory of earlier turns. Rebuild your position from
the brief, current-context.json, the receipts already written and the
repository before doing anything, and never redo a phase whose receipt exists.
Work toward the receipt for the current phase, write it, and end the turn; do
not start the next phase on your own. A turn has a time limit, after which the
host stops this process. Your exit status is recorded and is not evidence of
anything.

Your prompt carries two lines, `shift:` and `toolkit:`. In this file SHIFT is
the path on the `shift:` line and TOOLKIT is the whole command on the
`toolkit:` line, an interpreter path, `-P` and the path of the toolkit's entry
file. Run TOOLKIT
exactly as that line gives it, with the words shown here after it. Never run a
bare `sortie` and do not look for one: none may be on your PATH, and one found
elsewhere on the machine may be another version. If your prompt has no
`toolkit:` line, say so as the last thing you print and end the turn.

Read the brief path and SHA256, then current-context.json. Write acknowledgment,
plan, build and cessation receipts with exact shift/task/phase request ID,
round and brief digest from that context. Each gate has a fresh request ID.
Write every receipt this way and no other:

    TOOLKIT receipt template PHASE > SHIFT/logs/draft-REQUEST_ID.json
    (edit that draft: the identity fields from `expected` in current-context.json,
     a `receipt_id` of your own that no earlier receipt used, and the phase's data)
    (save the `expected` object from current-context.json as SHIFT/logs/expected-REQUEST_ID.json)
    TOOLKIT receipt write --shift SHIFT --expected logs/expected-REQUEST_ID.json \
      --from logs/draft-REQUEST_ID.json

PHASE is one of acknowledgment, plan, build, stop. For a stop receipt,
the record of your own seat ceasing, save the `stop_expected` object from
current-context.json and not `expected`, take the identity fields from it, and
give `scope` as `seat`. For a plan or a
build the driver consumes the receipt with a target you name to it (the plan
artifact, or the candidate commit), and `expected.target` in current-context.json is
still null while you write: put that target into your saved copy of `expected`,
`{"kind": "plan", "artifact": {"path": ..., "sha256": ...}}` or
`{"kind": "commit", "commit": FULL_COMMIT}`, as `TOOLKIT receipt template PHASE
--expected` shows it. `--expected` and `--from` are paths relative to SHIFT.
`receipt write` checks the draft as the receipt check will and writes it once as
`receipts/RECEIPT_ID.json`; it prints `"status": "written"` with that path, or
`"status": "invalid"` with the reasons and writes nothing. Correct the draft and
run it again; a receipt already written is never replaced, so a corrected one
needs a new `receipt_id`. Do not write into `receipts/` yourself and do not
import the toolkit's Python modules to do it. Give the driver artifact paths and exact
plan digest/candidate commit; it checks mechanics, not your code.

For a feature, investigate and plan before building. For a
defect, reproduce, trace root cause, add a failing test and make the smallest fix.
Build, test and commit. Commit
only what the task changed: a file that was already untracked or modified when
your first turn began is the developer's, so leave it out of your commit unless
the brief names it, and stage paths by name, never everything at once.

You do not run, request or record a review. When the driver takes your plan or
build receipt, the host itself starts the reviewer the developer signed, checks
what it answers and writes the review receipt. Never start a reviewer or any
review tool yourself and never write a receipt of phase `review`: `receipt
write` refuses one. Your work on a plan or a build ends when its receipt is
written.

When a review asked for changes, the driver opens a repair round. `findings` in
current-context.json then names a file under briefs/ with every finding you
have to answer: its `id`, `severity`, `location`, `claim`, `evidence` and
`recommendation`, and for a finding carried over from an earlier round the
reviewer's reason (`reviewer_reason`). `expected.required_findings` lists the
same ids. Your next plan or build receipt answers each of them once, and no
other, under `answers`: `disposition` is `fixed`, with what you changed as the
`reason`, or `rejected`, with the reason the finding is wrong. For a fix in a
build give an `evidence` artifact (`{"path": ..., "sha256": ...}`, a file under
the shift) that holds its reproduction; `evidence` is null when there is
nothing to run, as for a plan. The next review is handed your answers and
checks each against the plan or the patch: a claim there is not evidence. With
no findings to answer, `answers` is an empty list. The review receipt under
receipts/ names the reviewer's raw output under logs/ if you need its full
text. Reviewers inspect and report only; they run no code.

Each claimed fix needs a discriminating reproduction. Run it yourself, within
what your own configuration allows: the failing case on the baseline, the
passing case on the candidate. Run each in a
checkout of the named commit under the shift's `scratch/`, and retain the exact
argv, exit status and output of every run under the shift, failures included.
Choose only local checks, never commands for remote actions. The host runs no
commands for you in this shift: `TOOLKIT queue-check` is answered `blocked` and
nothing executes, so do not wait on it. If a check needs a permission you do
not have, record a blocker and next owner instead of working around it.
A missing, failed or inconclusive reproduction is said as such in your answer,
never passed off as a fix. A changed candidate needs its reproduction again.

Do not start repairing after a review asked for changes, or after the driver
returned a receipt, before the driver has opened the repair round: the round
and request ID in current-context.json change then. Initial round is 0; total repairs are capped at three, including
receipt-only repairs. Never hide a return, change task identity to reset the cap,
or replace a candidate without new bound evidence. When blocked, record the
reason and next owner. In wind-down, the last part of the signed window, the
host starts no new turn, so a turn that runs then is the task's last: finish the
step you are on, commit only what is complete and write its receipt, or leave a
truthful note of what is half done. The host stops a running turn shortly
before the signed deadline and then lists in the report whatever is uncommitted
in the repository. Write a truthful seat
stop receipt. A tool exiting does not establish that your agent seat has ceased.
