# sortie-kit

sortie-kit runs a time-boxed working session, a shift, with coding agents in a
Git repository of yours. You write down what you want done; a driver agent in
your terminal breaks it into tasks and hands them to a builder agent, which
plans, builds and commits. Each plan and each build is reviewed by a third
agent, the reviewer, which sortie-kit starts itself and whose answer it checks.
When the time is up you get a report of what was done and what was not.

Nothing starts until you have read what the shift may do and signed it with
your own key, in your own terminal.

This is an alpha, for macOS only. Linux is not supported yet. Commands, file
formats and the wording of what you sign may change from one version to the
next. [What real shifts have shown](#what-real-shifts-have-shown) says how
far it has been tried and what has never happened on a real shift.

## What you need

- macOS, Python 3.11 or later, `git`, and OpenSSH's `ssh-keygen`. Linux is not
  supported yet: the code has a path there for the hold against idle sleep,
  and nothing has been run on a Linux machine.
- At least one agent CLI, installed and signed in. Profiles for Claude Code
  (`claude`), Codex (`codex`) and Antigravity (`agy`) are built in;
  [agent profiles](docs/agents.md) says how to add another. `claude` and
  `codex` can take any seat. The built-in `agy` profile can only review; a
  profile of your own can put `agy` in the other seats, as one long shift
  did with all three.
- Better, two CLIs from different makers. The review is worth more when the
  reviewer's model is not of the builder's family. `sortie start` picks the
  reviewer for you from what is installed (see
  [who reviews](#who-reviews)); with one CLI the shift still runs and every
  place that names the review says `same-family review`.
- Nothing else. No skill, no review script and no other tool has to be
  installed. The start does not check that a CLI is signed in: nothing shows
  that without a model call. A reviewer that is not signed in fails its
  reviews, and a task whose review failed twice is blocked, not accepted.
- A Git repository to work in. Use one you can afford to have changed; see
  [what a shift can do to your machine](#what-a-shift-can-do-to-your-machine).

## Your first shift

1. Install sortie-kit, from a checkout of this repository, into a Python
   environment of your choice:

   ```sh
   python -m pip install .
   ```

2. Create a signing key with a passphrase. This is done once. Type the
   passphrase only at `ssh-keygen`'s own prompt, never into an agent.

   ```sh
   mkdir -p "$HOME/.config/sortie-kit"
   chmod 700 "$HOME/.config/sortie-kit"
   ssh-keygen -t ed25519 -C sortie-kit -f "$HOME/.config/sortie-kit/signing-key"
   ```

3. Write the trust file that names that key. Also once.

   ```sh
   sortie trust init
   ```

4. Write a direction file: plain text that says what you want done in this
   shift. It can live anywhere; the shift takes a copy. For example, in a file
   named `direction.md`:

   ```
   Add a --version flag to the command line tool. It prints the version from
   pyproject.toml and exits 0. Add a test for it.
   ```

5. Start the shift from your own terminal, not from inside an agent:

   ```sh
   sortie start --repo PATH --direction direction.md
   ```

   `PATH` is the top directory of the Git repository.

## What you will see

- **Questions.** The command asks for what the command line did not say, in
  this order: the driver's agent and model, the builder's agent and model, and
  how long the shift may run (for example `45m` or `2h`). Who reviews is not
  asked; it is picked and shown in the summary you sign. It
  offers the installed agent profiles and the models they list; a number picks
  one, or you type a model ID. The built-in `codex` profile lists no models,
  so for a Codex seat you type the model ID you would give the `codex` CLI.
  Nothing is guessed, and an unanswered question ends the start.
- **The signing summary.** It shows what you are about to sign in plain words:
  the repository, the time window, each seat's agent, model and full command,
  what the builder may do, what the reviewer can reach, and the shift's
  directory with the `sortie stop` command that ends it early. For the
  reviewer it says who was taken and why, for example:

  ```
  Reviewer:   claude, model claude-sonnet-5-5 (family claude), headless, started by the host for each review
              picked: the first installed agent of another family than the builder
  ```

  Then it shows the exact bytes being signed, and `ssh-keygen` asks for your
  key's passphrase.
- **The driver in your terminal.** The terminal is handed to the driver agent.
  The first time an agent CLI starts in a repository, expect a question of
  its own first, such as whether you trust the folder. That prompt is the
  CLI's, not the toolkit's. Read it before pressing Enter: its preselected
  answer can be the one that declines and exits (Claude Code's is "No,
  exit"), and a driver that exits ends the shift. The driver begins once you
  accept. The builder
  runs without a window, one turn at a time, when the driver asks for it. The
  reviewer runs without a window too: the host starts it when the builder
  hands in a plan or a build, and the driver waits for its answer. You can
  watch and you can talk to the driver.
- **The report.** When the shift ends (the driver exits, the time runs out, or
  you run `sortie stop --shift SHIFT` in another terminal, with the shift
  directory the signing summary showed), the terminal comes
  back and the command prints its last lines: how the shift ended, the tasks,
  the shutdown, and where the report is. For example:

  ```
  Shift ended: Task version accepted locally and closed.
  Tasks: 1 accepted locally of 1 briefed (version at aed7b35); the driver says the direction held no more.
  Shutdown: confirmed.
  Driver process: stopped by the host
  Report: /path/to/repo/.sortie/shifts/SHIFT/report.md
  ```

  `Tasks` counts the tasks the driver briefed to the builder. The host cannot
  read your direction, so the end of the line is the driver's own statement
  about the rest of it: that it held no more, how many things it names as not
  started (the report lists them under "Not done"), or, when the driver said
  nothing, that the host does not know.
  `Tasks` and `Shutdown` are two separate facts. `Shutdown: NOT CONFIRMED`
  means the host signaled a process it started and did not see it end; the
  line names it and gives a `ps` command that lists whatever is left of it (no
  line printed means nothing is). The tasks line stands either way. The report
  is `report.md` in the shift's directory under `.sortie/shifts/` in the
  repository. It is written for a person, and its Outcome begins with the same
  two lines: then what was asked, what was accepted and at which commit, what
  the review said, which findings accepted work was left with, what was not
  done and why, and who is next.

Everything a shift writes for itself (briefs, receipts, logs, the report) is
under `.sortie/` in the repository, which Git is told to ignore through the
repository's own `.git/info/exclude`. Your `.gitignore` is not touched. The
builder commits its work in the repository itself. The toolkit never pushes.
A seat's log holds what that seat's CLI printed, which for some CLIs is only
its final message. sortie-kit does not record what a seat ran.

For its checks the builder may add Git working trees under the shift's
`scratch/` folder. They are evidence and the host removes none, so `git
worktree list` in your repository goes on showing them; the report names
them. When you no longer need a shift's evidence, remove `.sortie/` and run
`git worktree prune` in the repository to clear those entries.

To be asked nothing, give every choice as a flag:

```sh
sortie start --repo PATH --direction FILE --for 4h \
  --agent driver=claude:DRIVER_MODEL --agent builder=codex:BUILDER_MODEL
```

Three more flags are there when you want them, and none is ever asked for:
`--agent reviewer=NAME[:MODEL]` names the reviewer yourself,
`--review-limit DURATION` sets how long one run of the reviewer may take (30
seconds to an hour, ten minutes when not given), and `--builder-network`
starts a `codex` builder with the network on, which it does not have
otherwise unless your own Codex configuration turns it on (see
[what a shift can do to your machine](#what-a-shift-can-do-to-your-machine)).

While a shift runs, the host asks the system not to put the machine to sleep
because it is idle, on macOS with a power assertion of its own. If the system
refuses, the shift runs all the same: the refusal is recorded, and the report
says the machine was not kept awake for the whole shift. The code has a path for
Linux as well, through `systemd-inhibit` when that is installed; it has not
been run on a Linux machine, and Linux is not supported yet. Anywhere else
there is no hold. That is idle sleep only. Closing a laptop lid or choosing
Sleep by hand still sleeps the machine, and the signed window keeps running while it sleeps. A
line under the signing summary says what this run will do, and the report
says whether the hold lasted the whole shift. `--no-wake` turns it off; it
goes only with a start that runs the shift, not with `--prepare-only` or
`--sign-only`.

`sortie start` uses the key and trust file from steps 2 and 3 when they are
where those steps put them, and says which files it took. `--trust FILE` and
`--key FILE` name others. Neither is ever asked for: when one is missing the
start ends before any question and says so.

## Who reviews

The reviewer is a third seat. You sign it like the other two: its program, its
arguments, its model and how long one run may take. You are not asked who it
is.

- With no `--agent reviewer=`, the start looks at the agent profiles that can
  review and whose program is installed, in the order `claude`, `codex`,
  `agy`, then your own profiles by name. It takes the first whose model family
  differs from the builder's. The driver's CLI is a fine reviewer. The summary
  says `picked: the first installed agent of another family than the builder`.
- When none of another family is installed, it takes the builder's own CLI.
  That is the one-CLI case and needs no flag. The summary, the terminal and
  the report say `same-family review` and the reason, that no agent of
  another family is installed. A same-family review shares the builder's
  blind spots; read its accepts with that in mind.
- `--agent reviewer=NAME[:MODEL]` names it yourself, and the summary says
  `given with --agent reviewer=`. Naming one of the builder's own family while
  an agent of another family is installed is refused unless you also pass
  `--allow-same-family-review`.
- When no installed profile can review, the start refuses before anything is
  prepared or signed and says so in one sentence.

A picked reviewer runs with the model its profile names for reviews
(`claude-sonnet-5-5`, `gpt-6.1-sol` and `gemini-4-hydrogen-high` in the
built-in profiles). If your CLI does not accept that model, name one with
`--agent reviewer=NAME:MODEL`.

The host starts the reviewer each time the builder hands in a plan or a build,
with a limit on its time, and reads its answer. The answer has to end with one
object in a fixed form ([the review contract](docs/review.md)); the host checks
it and writes the review's receipt itself. The builder does not run, check or
report its own review.

- `ACCEPT` moves the task on.
- `CHANGES` hands the findings to the builder, which answers each one in its
  next receipt; the next review is given those answers. Three rounds at most.
- A run that gives nothing usable (no object, a wrong one, an error exit, the
  limit passed, or a repository whose commit or status changed while it ran)
  is a failed review.
  It is not an accept and it uses no repair round. The host runs it once
  more; a second failure blocks that task for you and the shift goes on with
  the next one.

## A complete example

Everything from an empty directory to a running shift, to copy from top to
bottom into your own terminal. It makes a disposable repository and a virtual
environment under `~/dev/sortie-try`, and starts a 30 minute shift with a
Claude driver and a Codex builder; the reviewer is picked, and with those two
installed it is `claude`, of another family than the Codex builder. Thirty minutes and not
fewer: the last third of a window shorter than an hour is wind-down, in which
no new builder turn starts, so this leaves twenty minutes of work, which is
room for the task and one repair round if the plan or the review sends it
back. A 20 minute shift has run out during such a round with the task not
accepted. Set `SORTIE_KIT` to your
checkout of this repository first. The model IDs are the ones this pairing
was run with; use the IDs your own `claude` and `codex` accept. If you
already have the signing key and the trust file, leave out the `ssh-keygen`
and `sortie trust init` lines: neither overwrites what is there, and `trust
init` says so and stops.
The two `git config` lines give the disposable repository an example-only
identity for its commits; they touch no other repository and not your own
Git configuration.

```sh
SORTIE_KIT="$HOME/dev/sortie-kit"

mkdir -p "$HOME/dev/sortie-try"
cd "$HOME/dev/sortie-try"
python3 -m venv venv
venv/bin/python -m pip install "$SORTIE_KIT"

git init repo
git -C repo config user.name "Sortie Try"
git -C repo config user.email "sortie-try@example.invalid"
printf 'print("hello")\n' > repo/hello.py
git -C repo add hello.py
git -C repo commit -m "Start"

mkdir -p "$HOME/.config/sortie-kit"
chmod 700 "$HOME/.config/sortie-kit"
ssh-keygen -t ed25519 -C sortie-kit -f "$HOME/.config/sortie-kit/signing-key"
venv/bin/sortie trust init

printf '%s\n' 'Add a --version option to hello.py that prints hello 0.1.0 and exits 0.' \
  'Add a standard-library unittest for it. Make one local commit.' > direction.md

venv/bin/sortie start --repo repo --direction direction.md --for 30m \
  --trust "$HOME/.config/sortie-kit/trust.json" --key "$HOME/.config/sortie-kit/signing-key" \
  --agent driver=claude:claude-sonnet-5-5 --agent builder=codex:gpt-6.1-sol
```

Keep the repository under a normal project directory such as `~/dev`, not
under a temporary one; the note on hooks below says why. The guided form in
[your first shift](#your-first-shift) asks for the same choices one at a time.

## What a shift can do to your machine

Each seat runs under your own configuration of its agent CLI. The toolkit
decides which program starts, with which arguments and model, and you sign
exactly that. It does not decide what that program is then allowed to do, it
installs no agent permissions, and it ships no permission files. The one
exception is in the arguments you sign: a built-in `codex` builder is started
in Codex's workspace-write sandbox, because a builder must write and commit,
and the built-in reviewers are started in their CLI's read-only or plan mode.

What the default `codex` builder is started to do, in plain words:

- write anywhere in the target repository, including `.git`, so it can commit
  and can also change that repository's Git hooks and configuration, which
  later run as you;
- read anything your user can read, inside the repository or not.

It is not given the network: Codex's workspace-write sandbox runs the
builder's commands without it unless your own Codex configuration turns it
on. A builder without the network cannot install a package from a remote index,
fetch a page, or clone or pull from a remote; Codex itself still reaches its
model. When the
work needs the network, start with `--builder-network`: the builder's
arguments then carry `-c sandbox_workspace_write.network_access=true`, which
you see and sign, and the summary and the report say the builder can use the
network. With the network on, reading whatever your user can read is a way
for file contents to leave the machine. The flag is refused for a builder
whose profile has no such form, which is every built-in one but `codex`.

That is what the toolkit asks Codex for. Anything your own Codex configuration
grants on top, such as further writable directories or the network, still
applies; the toolkit does not read or limit it. Writing in the repository
and writing in `.git` each come from one element of the argument list you
see and sign (`--sandbox workspace-write`, `--add-dir .git`). Reading is not
limited by any of them. To strike one, copy the profile under your own name
without it and name that agent for the builder; see
[what the built-in seats can reach](docs/agents.md#what-the-built-in-seats-can-reach)
for what stops working without each.

A `claude` builder gets no such option and can do only what your Claude
settings allow without asking.

The driver is an interactive agent in your terminal under your own settings,
like any session you start yourself.

What the reviewer can reach, in plain words:

- It runs as you, with your sign-in for its CLI, without a window.
- It is started in a directory the host makes for that one run under the
  shift's `reviews/`. That directory holds a plain copy of the files (an
  export, not a Git checkout), the review instructions, the brief and, in a
  repair round, the earlier findings with the builder's answers. For the
  review of a commit the copy is of that commit, with the task's whole patch
  and its plan when it had one; for the review of a plan it is of the commit
  the repository is at, with the plan.
- The built-in profiles start it in the CLI's own mode for reading: `claude
  --print --permission-mode plan`, `codex exec --sandbox read-only`, `agy
  --print PROMPT --mode plan --sandbox`. You see that command in the summary
  and sign it. sortie-kit has not checked what each mode stops.
- sortie-kit does not confine it. It can read whatever your user can read and
  use the network as its CLI allows. A profile of your own without such a mode
  can write.
- If the repository's commit or status, or the shift's `receipts/`, differ
  after a run from what they were before it, that run is a failed review.

Your own configuration includes your hooks. Each seat's agent CLI loads the
hooks, permission rules and project settings it would load in any session you
start in that repository, and the toolkit neither reads nor switches them off.
A hook that refuses a command the driver has to run stalls the shift. The
driver's work is mostly one command, `sortie wait` (it runs it as
`python -m sortie_kit wait --shift ...`), over and over. In one real run a
Claude Code project hook refused that command because the repository was
under a temporary directory. What that looks like: the driver says in your
terminal that its command was blocked or denied, no builder turn starts, and
the shift runs out with no task accepted. If you see it, end the shift with
`sortie stop`, move the repository or change the hook, and start again.

## How a shift ends

A shift starts nothing new in its last stretch, the wind-down: twenty minutes
for a shift of an hour or more, the last third of a shorter one. In a 45 minute
shift new work can start for at most 30 minutes, in a 10 minute one for at
most 6 minutes 40 seconds; signing and startup come out of that time.
Wind-down does not end the shift. A builder turn that is already
running may finish, and the driver can still take what was handed in, close an
accepted task and write its last statement. A review is started in wind-down
only when its whole limit still fits in the working time left; when it does
not, the report says the work was handed in and not reviewed. With the default
limit of ten minutes no review starts in the wind-down of a shift of thirty
minutes or less; a shorter `--review-limit` changes that. The summary shown
before signing states these times.

Shortly before the signed deadline (ten seconds before it, less in a shift of
under a minute) the host begins to stop whatever still runs, and then writes
the report. It signals each process and waits a second or two for it to be
gone. These stops are best effort: no exact order or moment is promised, and a
process that was not seen to stop is reported as such: the `Shutdown` line in
the terminal and at the top of the report says `NOT CONFIRMED`, names the
process and gives the `ps` command to look for it, and `sortie start` exits
with status 3. A process that was still on its way out when the host stopped
waiting is looked at once more before the report is written; seen gone by
then, the shutdown is confirmed.

When it ends, the host also takes a short, bounded look at the repository, and
the report lists the uncommitted paths that look saw, so half a change from a
stopped turn does not go unnoticed. Paths the look at the start had
already seen are counted, not listed. When the look at the start failed or
was cut short, the report says that it cannot tell whether the listed paths
are new. That list is advice, not evidence. It is what Git reported when run
as you, it leaves out ignored paths, and it can be cut short or fail; the
report says so when that happens. `git status` in the repository is the
answer.

## Other ways to run it

To prepare a shift without signing or starting anything:

```sh
sortie start --repo PATH --direction FILE --for 4h --prepare-only
```

`PATH` must be a Git worktree root. `FILE` is a nonempty UTF-8 direction file.
The duration is finite; seconds, minutes, and hours are supported. Preparation
asks nothing, so it needs `--for`. Preparation copies the direction, renders
templates, and writes an explicitly unsigned `grant.draft.json`; an unsigned
draft never authorizes anything. When no rule
ignores `.sortie` yet, it adds `/.sortie/` to the repository's own
`.git/info/exclude`, a file Git never tracks. It does not create or edit
`.gitignore`. If a rule in `.gitignore` re-includes `.sortie` (for example
`!/.sortie/`), preparation refuses and says so, because that rule outranks the
exclude file. It also refuses tracked `.sortie` artifacts and symlinks in its
writable paths. Preparation names no agent and starts no background process.

`--sign-only` prepares and signs and starts no agent. Signing on its own,
`sortie verify`, `sortie stop`, receipt checks and `sortie report` are
described in the documents below.

Exit 0 means the command did what it was asked. Exit 2 means the command line
was incomplete or invalid: before creating anything, a start lists what is
missing when the trust file or key is not found, or when a choice is missing
and there is no terminal to ask in. Exit 3 means a refusal or a failure, for
example when signing is not possible in this terminal or no installed agent
can review. A partial preparation is retained with
`preparation-error.txt` when that diagnostic can be written. From `sortie
verify`, exit 4 means the signature is good and the signed window is not
current (it ended, has not begun, or the shift was withdrawn).

For a shift that ran, the exit status is about the host, not about the tasks:

- 0: the shift ended in an ordinary way (the driver finished or exited, or the
  working time ran out) and the shutdown was confirmed. It does not mean a
  task was accepted; the `Tasks` line and the report say that.
- 3: the shift was cut short (withdrawn, a changed source, a failure), or it
  ended in an ordinary way and the shutdown was not confirmed or not clean, or
  the final report was not written. With tasks accepted and `Shutdown: NOT
  CONFIRMED`, the 3 is about the shutdown; the accepted tasks stand.
- 130: the host was interrupted from the keyboard.

## More detail

- [Grant setup and password isolation](docs/grants.md): the key, the trust
  file, what a grant pins, signing, verifying and withdrawing.
- [Agent profiles](docs/agents.md): the built-in seats, what they can reach,
  and adding your own.
- [Driver lifecycle and builder checks](docs/lifecycle.md): what the seats
  do, the file channels, the host-run review, repair limits, wind-down and
  the repository look.
- [The review contract](docs/review.md): what a reviewer is told and what the
  host takes from its answer.
- [The receipt contract](docs/receipts.md): templates, validation, and the
  distinction between attributed evidence and task acceptance.
- [Shift reports](docs/reports.md): closeout, unknown evidence, local
  regeneration and comparison limits.

## Development and evidence

```sh
python3 tests/run_suites.py
```

This one command runs every test in `tests/`, found by discovery, from the
checkout with nothing installed. It gives Git an empty configuration and
removes the SSH agent from the run, so the signing tests reach only the
throwaway keys they make. A name narrows it to a module, a class or a
method: `python3 tests/run_suites.py test_report`. `tests/readme_run.py` runs
this README's commands from a fresh install, and `tests/check_install.py`
checks an installed wheel. `tests/check_sdist.py` and `tests/check_wheel.py`
account for every member of a built source distribution and of a built wheel.
Tests keep scratch beneath `.scratch/`, use fixture agents and disposable
terminals, and create and
reap their own bounded fixture subprocesses. They never run a real agent CLI.
The internal supervisor retains foreground child ownership, bounded
restart backoff, finite lifetimes, and source-change shutdown. At a terminal
condition it starts no more work; local cleanup signals the toolkit child's own
process group and reaps the direct child, within a one-second bound. Descendant
cleanup also runs when the direct child exits and before any restart. Existing
developer agent CLIs are outside that group. Toolkit subprocesses must retain
the inherited group; deliberately detached descendants are not contained by it.
The signed admission checks exact grant bytes and the
pinned seats before the driver starts and again before every builder turn and
every run of the reviewer; no
command-line option or environment switch enables unsigned live execution.

See [source provenance](docs/source-provenance.json) for what was adapted from
earlier code, and [SECURITY.md](SECURITY.md) for how to report a security
fault privately.
The agent CLIs themselves are covered by fixtures only: no test starts
`claude`, `codex` or `agy`. The tests have been run on macOS with Python
3.11, 3.13 and 3.14.

### What real shifts have shown

Every real shift so far was run on macOS by the project's own developers,
with keys made for testing, and all but one on small practice repositories.
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
- Wind-down: once it began the host refused a new builder
  turn in a plain sentence, and the shift closed with a report that said
  what was not done.
- The hold against idle sleep on one Mac: listed by `pmset -g assertions`
  while a real shift ran, gone after it, and recorded as held for the whole
  shift. Other programs on that machine held assertions of their own the
  whole time.

None of the following has happened on a real shift. The host's part in
each is exercised by fixture tests, on macOS, and by nothing else:

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
show it, since no test starts a real agent CLI, signs with a real key,
sleeps a machine or runs on Linux:

- `codex` as the driver.
- A shift signed with a key the person uses for anything else. Every key so
  far was made for the test.
- A machine on which no separate review tool was ever installed. The
  reviewer needs none, but the machines used all had one from earlier
  versions.
- A machine that is put to sleep in the middle of a shift.
- The hold against idle sleep on a machine that nothing else keeps awake.
- Anything at all on Linux. The tests of the Linux path for the hold use a
  stand-in for `systemd-inhibit` and were run on macOS.

The earlier pane transport, its journal and the runtime permission examples
were removed; `sortie verify` still reads grants signed for them, and
`sortie report` still reads their shifts.

## License and relationship to the experiment

Sortie-kit is licensed under the [MIT license](LICENSE). That one file also
covers the tiger working agreement it ships, which is the same owner's
project under the same licence.
The owner authorized the copied shift-driver code for this divergent public
instance. The private experiment continues separately; this project does not
modify or deploy to it.
