# What real shifts have shown

How far sortie-kit has been tried on real shifts, and what has never
happened on one. [Development](development.md#where-they-have-been-run) says
where the tests have been run.

Every real shift of this first list was run on macOS by the project's own
developers, with keys made for testing, and most on small practice
repositories. They have shown:

- Each of the three built-in review forms doing the reviews of a shift:
  `claude` picked, `codex` named and running `gpt-6.1-sol`, `agy` named.
  Every review came back in the contract's form.
- A `CHANGES` round on a plan, settled: the review had one finding, the
  builder answered it, and the next review accepted with that finding
  marked resolved.
- A `CHANGES` round on a commit, settled, once: the first review of a
  task's candidate asked for changes with one major finding, which named a
  requirement that the same answer marked as not met; the builder repaired
  it in one round and the next review accepted.
- `claude` driving, with `codex` as the builder and with `claude` as the
  builder; in one of the latter the reviewer was picked from another family
  without a flag. Two tasks accepted in one shift, several times.
- `codex` driving, in one shift: on a copy of a real project, with `claude`
  as the builder and the reviewer named by flag, four tasks accepted.
- One long shift on a copy of a real project: four tasks accepted in 67
  minutes of a 90 minute window, with nine reviews, on a profile of the
  developer's own that put `agy` in all three seats. The built-in `agy`
  profile has had the same driver and builder forms since. The logs that
  shift kept of its builder and its reviewer hold the final message of each
  run and nothing else.
- The built-in `agy` profile driving and building, in one shift: on a
  practice repository, with a key made for the test and the reviewer picked
  from another family (`claude`), two tasks accepted. The logs it kept of
  its builder hold the final message of each turn and nothing else.
- An agent CLI that replaced its own program after the grant was signed,
  once: `agy` updated itself seconds after the driver started. The host
  refused the builder's first turn with `builder agent executable changed
  after admission`, the driver closed the shift, and the report gave that
  reason with nothing built. The same command, started and signed again,
  ran the shift in the entry above.
- An agent CLI whose pinned file was written again with the same bytes,
  once: the pinned file of `codex` is a launcher script, and an update of
  the CLI during a shift wrote it again unchanged while the program it
  starts went to a new version. Other sessions of that CLI were running on
  the machine. The host refused the builder's next turn with the same words
  as above, in a repair round, and the shift ended with nothing accepted.
  The words of the refusal were changed after it to say what the host
  compares; see [the executable pin](agents.md#executable-pin).
- One shift with the developer's review skills switched off in the agent's
  settings for the repository, with `claude` in all three seats, on a copy
  of a real project: four tasks accepted, with eight reviews run by the
  host, a same-family review named by flag. The skills' files were still
  installed: switched off, not removed. The reviewer starts in a directory
  of its own beneath the repository, and nothing the shift kept shows which
  settings it loaded there.
- Wind-down: once it began the host refused a new builder
  turn in a plain sentence, and the shift closed with a report that said
  what was not done.
- The hold against idle sleep on one Mac: listed by `pmset -g assertions`
  while a real shift ran, gone after it, and recorded as held for the whole
  shift. Other programs on that machine held assertions of their own the
  whole time.
- A review run by the host that fails, its second run, and a task blocked
  after two failures, in two shifts. Both failures were caused on purpose.
  In one the reviewer was `codex` with a model name that does not exist:
  the plan review failed on both runs, each ending with status 1 in about 3
  seconds, the task was blocked, and the report quotes the CLI's error under
  each attempt. In the other the reviewer was `agy` with a review limit of
  one minute: the host ended both runs at 60 seconds, the task was blocked,
  the second task was not briefed for that reason, and shutdown was
  confirmed. A review that failed on its own, with nothing arranged to make
  it fail, has still not been seen.
- A reviewer answering lines of a brief on how the work is delivered from
  the host's own statement of the repository, in one shift. The brief had
  four such lines. Three were answered as met, each with the place in the
  statement named: the candidate is one commit on `main` as it was when the
  shift began, no remote was added, and nothing was left untracked or
  staged. The fourth, that nothing is pushed, stayed `not_checked`, with
  the reason that no remote and no remote-tracking ref cannot prove it. The
  host looks at remotes and remote-tracking refs and at no push, so such a
  line stays unchecked. On earlier shifts, where the statement gave the two
  commits and nothing else, 4 to 10 such lines were answered `not_checked`
  on several shifts.

Others than the project's developers have run real shifts too, on a build
that has `sortie doctor`, and told the project what they saw. Those shifts
were on macOS as well, as told. Each of them was signed with a key the agent
that ran it had made itself, so its signature is no proof that a person was
present, and the project did not run them. As told:

- `codex` as both the driver and the builder of one shift, with `claude` as
  the reviewer: two of two tasks accepted. The project's own developers have
  not run that combination.
- A task set aside after its builder had already committed. The driver set
  the task aside and said in its reason, which the report quotes, that the
  commit was on the branch with no build receipt and no review. The host's
  own lines in the report said of `HEAD` that it is not a commit the shift
  accepted. The host compares the commit at `HEAD` alone and keeps no account
  of other commits on the branch; see [shift reports](reports.md).

None of the following has happened on a real shift that the project ran or
was told of. The host's part in
each is exercised by fixture tests, on macOS and in the Ubuntu jobs of the
hosted checks, and by nothing else:

- A review run by the host that fails on its own, with nothing arranged to
  make it fail; the two shifts above were given a model name that does not
  exist and a short limit. Under an earlier version, where the builder ran a
  separate review tool, one review timed out and the task was left
  unaccepted.
- A review in wind-down, started when its limit still fits and held when
  it does not, and the deadline arriving with a builder turn in flight. A
  review already running when wind-down begins is in no test either.
- `Shutdown: NOT CONFIRMED`. One early shift had a driver that was slow to
  exit; the cause is not known and it has not happened since. The report
  now says so and gives a command to look for what is left, and that
  wording has not been seen on a real shift.
- A machine with a single agent CLI, where the reviewer is the builder's
  own CLI without any flag.
- A review whose end could not be saved in time.
- A repository with a remote added or changed during the shift, a
  remote-tracking ref that holds the candidate, or something untracked or
  staged left behind, and what the host's statement of the repository and
  the reviewer then say of it.
- A lapse in the hold against idle sleep, and a hold that is refused or
  dropped.

None of the following has happened on such a shift either, and no test can
show it, since no test starts a real agent CLI, signs with a real key or
sleeps a machine, and the tests that have run on Linux ran on a hosted runner
with stand-ins in every seat:

- A shift signed with a key the person uses for anything else. Every key so
  far was made for the test, or by the agent that ran the shift.
- A machine that is put to sleep in the middle of a shift.
- The hold against idle sleep on a machine that nothing else keeps awake.
- Anything at all on Linux. The tests of the Linux path for the hold use a
  stand-in for `systemd-inhibit`; they were run on macOS and, in the hosted
  checks, on Ubuntu.

