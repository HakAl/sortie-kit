# What you will see

What happens after `sortie start`, from the first question to the report, and
how a shift ends. [Your first shift](../README.md#your-first-shift) has the
steps before it.

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
  exit"; the one seen from `agy` was "Yes, I trust this folder"), and a driver that exits ends the shift. The driver begins once you
  accept. The builder
  runs without a window, one turn at a time, when the driver asks for it. The
  reviewer runs without a window too: the host starts it when the builder
  hands in a plan or a build, and the driver waits for its answer. You can
  watch, and what you type goes to the driver, but the driver is told not to
  count on you: see [what still needs you](#what-still-needs-you).
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

Everything a shift keeps (briefs, receipts, logs, the report) is
under `.sortie/` in the repository, which Git is told to ignore through the
repository's own `.git/info/exclude`. Your `.gitignore` is not touched. The
builder commits its work in the repository itself. The toolkit never pushes.
A seat's log holds what that seat's CLI printed, which for some CLIs, `agy`
among them, is only its final message. sortie-kit does not record what a seat ran.

For its checks the builder may add Git working trees under the shift's
`scratch/` folder. They are evidence and the host removes none, so `git
worktree list` in your repository goes on showing them; the report names
them. When you no longer need a shift's evidence, remove `.sortie/` and run
`git worktree prune` in the repository to clear those entries.

The one thing a shift puts outside the repository is the temporary directory
of the builder's turns and of reviews: the host makes it in the system's
short temporary location, so that tests which bind Unix sockets or must run
outside a Git checkout can pass, and tries to remove it when the shift ends.
The signing summary says where it would be made as things are when you sign,
and the report says where it was and what became of it.

## What still needs you

The signature and the window are the handover. Within the direction and what
you signed, the driver is told to decide for itself, never to wait for an
answer in the terminal, and to record each decision it made on its own and
each question it could not settle. The report counts these in its Outcome and
lists them under "Notes from the driver", as the driver's own statements,
of which the host checked none. One thing is not the driver's to decide:
when it holds that accepted work breaks a line your direction states, it is
told to note that and end the shift early with you as next owner, since no
command sends an accepted plan back. sortie-kit itself asks you nothing after the signature. What
has needed a person in the shifts run so far:

- **Before the shift runs:** the questions of `sortie start` when the command
  line left something out, and your key's passphrase at signing.
- **The driver CLI's own first question,** such as whether you trust the
  folder. Its preselected answer can be the one that declines and exits.
- **A driver CLI that your own configuration makes ask before it runs a
  command.** It asks in the terminal, and with nobody there it waits. The
  builder and the reviewer are given no terminal input, and each turn and
  each review has a time limit: a CLI that lacks a permission there normally
  fails or stops short, and one that hangs all the same is stopped at its
  limit.
- **A machine that stays awake.** The host holds off idle sleep where it can
  ([keeping the machine awake](lifecycle.md#keeping-the-machine-awake)); a closed lid, Sleep chosen by hand, an empty
  battery or `--no-wake` still let the machine sleep, and the signed window
  goes on running meanwhile.

Beyond that list nothing is promised. Not waiting is an instruction to the
driver, not something the host enforces: the driver is an interactive
program, and the toolkit cannot prompt one that stops and waits all the same.
Such a shift stands idle until its deadline, and the report says what was
done until then.

## When an agent runs it for you

This section is written to the agent as much as to you. An agent that was
asked to run sortie-kit and reads that a shift is signed in the developer's
own terminal and not from inside an agent has a way on in both of these
cases, and no reason to refuse the work.

**A trial.** The developer told the agent to try a shift: to see the tool
work, to test it, or to work on sortie-kit itself. The agent signs with a key
of its own, made for the trial, and never with the developer's key or trust
file:

```sh
mkdir -m 700 TRIAL
HOME=TRIAL sortie setup
sortie start --repo COPY --direction DIRECTION --for 30m \
  --agent driver=NAME:MODEL --agent builder=NAME:MODEL \
  --trust TRIAL/.config/sortie-kit/trust.json \
  --key TRIAL/.config/sortie-kit/signing-key
```

- `TRIAL` is a new, empty directory outside the repository, written as a
  full path with no symbolic link in it. On macOS `/tmp` and the directory
  `mktemp` gives are behind one and are refused; a directory under the home
  directory works. `sortie setup` makes the key and the trust file under the home
  directory it is given, so with `HOME` set for that one command they land
  in `TRIAL/.config/sortie-kit/` and the developer's own are neither read
  nor written.
- `sortie start` runs under the real home directory, because the agent CLIs
  in the seats need their sign-in there, and is given the trial's two files
  by name with `--trust` and `--key`.
- `COPY` is a practice repository or a clone of the real one that stands
  alone, never the only copy of work that matters. A linked working tree
  (`git worktree add`) is not such a copy: it shares the real repository's
  Git directory, where the shift writes and the builder commits.
- `DIRECTION` is the direction file, by its full path or a path from the
  directory the command is run in. `--repo` does not change where it is
  looked for.
- `NAME` is an agent profile, such as `claude` or `codex`, and `MODEL` the
  exact model its CLI is started with. A model the profile does not list
  may also need `--family ROLE=FAMILY`
  ([agent profiles](agents.md)). Whatever of the window, the seats, their
  models and a family that has to be stated is left out, `sortie start` asks
  for in the terminal, and it shows the whole shift before it signs.
- Both commands need a terminal and refuse without one, so the agent runs
  them in a terminal it can type into. It chooses a passphrase for the trial
  key, types it at `ssh-keygen`'s prompts and again when `sortie start`
  signs.
- The agent says in what it reports that the shift was signed by the agent
  with a key made for the trial. Such a shift shows what the tool does. It
  does not show that a person approved the scope, so the presence boundary
  was not tested.

**Real work under your key.** The agent prepares everything and hands you
one step. It writes the direction file, checks what
[a shift needs](../README.md#what-you-need), and writes out the whole
command with nothing left for `sortie start` to ask but the passphrase:
`--repo`, `--direction`, `--for`, `--agent` with the profile and the
model for the driver and for the builder, and `--family` where a model's
family has to be stated. `sortie start` shows you the whole
shift before it signs. You run that command in a terminal of your own that
no agent can watch or type into, outside any agent session, and type your
passphrase there. A pane the agent can read or send keys to is not such a
terminal, and the tool cannot tell: see the
[presence boundary](grants.md#presence-boundary-and-evidence-limits). The
agent does not type that passphrase, does not ask you for it and does not
look at the terminal you sign in. That one step, taken there, is the
handover the signature stands for: after it the shift needs nothing more
from you than [what still needs you](#what-still-needs-you) lists.

None of this contradicts [shift grants](grants.md), which is about your real
key and real work: what a signature made with that key says, and what has to
be kept from agents for it to say so.

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
