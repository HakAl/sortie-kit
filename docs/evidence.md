# What real shifts have shown

How far sortie-kit has been tried on real shifts, and what has never
happened on one. [Development](development.md#where-they-have-been-run) says
where the tests have been run.

Every real shift so far was run on macOS by the project's own developers,
with keys made for testing, and all but two on small practice repositories.
They have shown:

- Each of the three built-in review forms doing the reviews of a shift:
  `claude` picked, `codex` named and running `gpt-6.1-sol`, `agy` named.
  Every review came back in the contract's form.
- A `CHANGES` round on a plan, settled: the review had one finding, the
  builder answered it, and the next review accepted with that finding
  marked resolved.
- `claude` driving, with `codex` as the builder and with `claude` as the
  builder; in one of the latter the reviewer was picked from another family
  without a flag. Two tasks accepted in one shift, several times.
- One long shift on a copy of a real project: four tasks accepted in 67
  minutes of a 90 minute window, with nine reviews, on a profile of the
  developer's own that put `agy` in all three seats. The built-in `agy`
  profile is still review only.
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

None of the following has happened on a real shift. The host's part in
each is exercised by fixture tests, on macOS and in the Ubuntu jobs of the
hosted checks, and by nothing else:

- A review run by the host that fails, its second run, and a task blocked
  after two failures. Under an earlier version, where the builder ran a
  separate review tool, one review timed out and the task was left
  unaccepted.
- A review in wind-down, started when its limit still fits and held when
  it does not, and the deadline arriving with a builder turn in flight. A
  review already running when wind-down begins is in no test either.
- `Shutdown: NOT CONFIRMED`. One early shift had a driver that was slow to
  exit; the cause is not known and it has not happened since. The report
  now says so and gives a command to look for what is left, and that
  wording has not been seen on a real shift.
- A review that asks for changes on a commit. The changes rounds seen were
  on plans.
- A machine with a single agent CLI, where the reviewer is the builder's
  own CLI without any flag.
- A review whose end could not be saved in time.
- A lapse in the hold against idle sleep, and a hold that is refused or
  dropped.

None of the following has happened on a real shift either, and no test can
show it, since no test starts a real agent CLI, signs with a real key or
sleeps a machine, and the tests that have run on Linux ran on a hosted runner
with stand-ins in every seat:

- `codex` as the driver.
- A shift signed with a key the person uses for anything else. Every key so
  far was made for the test.
- A machine that is put to sleep in the middle of a shift.
- The hold against idle sleep on a machine that nothing else keeps awake.
- Anything at all on Linux. The tests of the Linux path for the hold use a
  stand-in for `systemd-inhibit`; they were run on macOS and, in the hosted
  checks, on Ubuntu.

