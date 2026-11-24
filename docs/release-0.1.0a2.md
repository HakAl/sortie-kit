# sortie-kit 0.1.0a2

The first public alpha. sortie-kit runs a bounded shift of coding agents in
your terminal: you sign which agent programs start, with which arguments and
for how long; a driver hands tasks to a headless builder; the host runs an
independent review of each plan and each commit; and every step leaves a
file receipt and a closing report you can read afterwards. It is an alpha:
the interfaces and file formats may change, and what has and has not been
shown is listed below so that you can decide what to trust it with.

## What you need

- macOS. No shift has been run on Linux. The tests have run there on a
  hosted runner, with a stand-in in every seat.
- Python 3.11 or newer, Git, and OpenSSH's `ssh-keygen`.
- At least one agent CLI you already use and have signed in to. `claude`,
  `codex` and `agy` are built in, and each can take any seat.

Install from a checkout with `python -m pip install .`. The README's first
shift takes it from there.

## What real shifts have shown

Every real shift so far was run on macOS by the project's own developers,
with keys made for testing, and most on small practice repositories.
`docs/evidence.md` says more about each. In short:

- Each of the three built-in review forms has done the reviews of a shift,
  and every review came back in the contract's form.
- `claude` has driven, with `codex` and with `claude` as the builder. Two
  tasks accepted in one shift, several times.
- `codex` has driven one shift, on a copy of a real project, with `claude`
  as the builder and the reviewer named by flag. Four tasks accepted.
- One long shift on a copy of a real project accepted four tasks. It ran on
  a profile of the developer's own that put `agy` in all three seats, not on
  the built-in profiles. The built-in `agy` profile has had the same driver
  and builder forms since.
- A review that asked for changes on a plan was answered and then accepted.
- Wind-down refused new work in a plain sentence and closed with a report
  that said what was not done.
- The hold against idle sleep was held for a whole shift on one Mac, on
  which other programs held assertions of their own the whole time.

Two shifts were run after that list was written, on this version:

- The built-in `codex` builder finished its tasks with no network option
  passed, which is how this version starts it. Your own Codex configuration
  can still turn the network on; sortie-kit does not read or limit it.
- With `--builder-network`, Codex's own log said the network was on, and
  the grant, the signing summary and the report all showed the option.

And two more after the built-in `agy` profile could take any seat:

- The built-in `agy` profile drove and built one shift on a practice
  repository, with a key made for the test and the reviewer picked from
  another family (`claude`). Two tasks accepted.
- The attempt before it ended with nothing built: `agy` updated itself
  seconds after the driver started, the host refused the builder's first
  turn because its program was no longer the one signed, and the report gave
  that reason. See the limit on self-updating CLIs below.

## What no real shift has shown

The host's part in each of these is exercised by fixture tests, on macOS and
in the Ubuntu jobs of the hosted checks, and by nothing else:

- A review that fails, its second run, and a task blocked after two
  failures.
- A review started in wind-down, and the deadline arriving with a builder
  turn in flight. A review already running when wind-down begins is in no
  test either.
- `Shutdown: NOT CONFIRMED` with its present wording.
- A review that asks for changes on a commit. The ones seen were on plans.
- A machine with a single agent CLI, where the reviewer is the builder's own.
- A lapse in the hold against idle sleep, or a hold that is refused or
  dropped.

And these no test can show, since no test starts a real agent CLI, uses a
developer's own key or sleeps a machine, and the tests that have run on
Linux ran on a hosted runner with stand-ins in every seat:

- `codex` as both the driver and the builder of one shift.
- A shift signed with a key you use for anything else. Make a key for this.
- A machine put to sleep in the middle of a shift, and the hold against
  idle sleep on a machine that nothing else keeps awake.
- Anything at all on Linux.

## Known limits

- A seat runs under your own configuration of its agent CLI. sortie-kit
  decides which program starts and with which arguments, and you sign
  exactly that. It does not limit what the program may then do. Read "What
  a shift can do to your machine", `docs/machine.md`, before the first
  shift.
- The built-in `codex` builder can write anywhere in the repository,
  `.git` included, so it can change that repository's Git hooks and
  configuration, which later run as you. It can read whatever your user can
  read.
- A `claude` builder works only if your own Claude settings already allow,
  without asking, what the work needs.
- The built-in `agy` builder is passed no permission, mode or sandbox
  option. It can do whatever your own agy configuration allows without
  asking, and sortie-kit neither reads nor limits that. With `agy` a
  builder's or a reviewer's log holds its final message only.
- The grant signs the bytes of each agent's program. Once an agent CLI has
  updated itself after the grant is signed, the host refuses the next start
  of that seat, the shift can do no more work with it, and the report gives
  the reason. This happened once, with `agy` as the builder. The remedy is
  to start a newly signed shift.
- A family stated with `--family` may not contradict the profile: no
  statement makes a model on the built-in `claude` profile anything but
  `claude`, or one on `codex` anything but `gpt`. For a model that a profile
  neither lists nor places, the family is the one you state and sign.
- What each reviewer's read-only or plan mode actually stops has not been
  checked by sortie-kit, and the summary you sign says so.
- A reviewer is told that a finding which shows a breach of something the
  brief says must hold, or of something it rules out, is at least major,
  whatever its size, so that the task goes back for repair. That is in the
  reviewer's instructions; the host does not check the severity a reviewer
  gives, and no real shift has shown this rule at work.
- The stops at the end of a shift are best effort. No exact moment and no
  order between them is promised, and a stop that could not be confirmed is
  reported as such.
- No response time is promised for reports of faults, and earlier alphas are
  not patched. See SECURITY.md for how to report a security fault privately.

## Two findings accepted as they are

An independent review raised these two. Neither was changed in the code.
The first is described in `docs/lifecycle.md`; the second is described only
here.

- **The end of working time is not a hard edge for a builder turn that ends
  just before it.** The host can spend what is left of the working time
  waiting for that turn's processes to be gone, and can then still write
  the turn's last output into its log after the working time is over and
  before it stops the driver. The timing of the stops is best effort: a stop
  can come later than planned when something before it is slow, and no
  order between the stops is promised. A process group that was not seen
  gone within its wait is reported as a stop that could not be confirmed.
- **A repository whose Git directory path contains a line break is not
  looked at.** Both of the host's looks at the repository, at the start and
  at the end of a shift, then record an error, and the report says that
  what is uncommitted could not be read and to run `git status`. The shift
  itself runs.

## What the hosted checks cover, and what they do not

The repository carries two workflows. `ci.yml` runs on every pull request
and on every push to the default branch: every test in `tests/`, a build of
the source distribution and of the wheel, an install of each into a clean
environment with `tests/check_install.py`, every member of the source
distribution compared with `tests/sdist-files.txt` by `tests/check_sdist.py`,
every member of the wheel compared with the package directory by
`tests/check_wheel.py`, and the documented commands through
`tests/readme_run.py`, on macOS and on Ubuntu with Python 3.11 to 3.14.
`release.yml` builds and checks the two files from a tag and attaches them
to a pre-release. `ci.yml` has run on hosted runners: on commit `225e91a`
it passed in all eight jobs, macOS 15 and Ubuntu 24.04, each with Python
3.11, 3.12, 3.13 and 3.14, every step of each. `release.yml` has not run,
since no tag has been pushed. The results under Verification are from the
developers' machine.

What they do not cover:

- No real agent CLI. No job starts `claude`, `codex` or `agy`, since that
  needs a signed-in account and no job is given a secret. Every seat in the
  tests is a stand-in program.
- Nothing about real sleep. A hosted runner is a virtual machine, and what
  its power management does shows nothing about a laptop. What real shifts
  have shown of the hold against idle sleep, and what they have not, is
  listed above.
- Linux. The Ubuntu jobs report and do not gate: a failure there does not
  fail the run, and passing there does not make Linux supported. This
  version is for macOS only.

## Verification

Run on the exported tree, on macOS:

- Unit tests: `python3 tests/run_suites.py` in a fresh clone of this tree,
  Python 3.14: 622 tests, all passing. The same sources before the export:
  622 tests on each of Python 3.11, 3.13 and 3.14. Python 3.12 was not run
  on that machine; the hosted run on commit `225e91a` passed with it.
- Fresh-clone checks: a clone of this tree was installed with
  `python -m pip install .` into a new environment, `sortie --version`
  answered 0.1.0a2, and `tests/readme_run.py`, which follows the documented
  commands with stand-in agents and a key made for the test, passed.
- Live validation: none was performed on this exact tree. The two shifts
  named above as run on this version used a build of the same sources.
- The public file set matches its explicit allowlist. Private development
  history, raw evidence, transcripts and captures are excluded.
