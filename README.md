# sortie-kit

**Run coding agents in a time-boxed shift that you sign, in a Git repository
of yours, with each plan and each build reviewed by an agent that sortie-kit
starts itself.**

sortie-kit runs a time-boxed working session, a shift, with coding agents in a
Git repository of yours. You write down what you want done; a driver agent in
your terminal breaks it into tasks and hands them to a builder agent, which
plans, builds and commits. Each plan and each build is reviewed by a third
agent, the reviewer, which sortie-kit starts itself and whose answer it checks.
When the time is up you get a report of what was done and what was not.

Sortie-kit generalizes a private experiment in which a team of agents has worked on a real project in bounded, signed shifts since September 2026.

Nothing starts until you have read what the shift may do and signed it with
your own key, in your own terminal.

This is an alpha, for macOS only. Linux is not supported yet. Commands, file
formats and the wording of what you sign may change from one version to the
next. [Status](#status) says how far it has been tried.

This is what a report looks like: an excerpt of the `report.md` of a real
shift on a practice repository, two tasks in nine minutes. Each line is a
line of that report; `[...]` stands for lines left out.
[Shift reports](docs/reports.md) says what the rest of a report holds and
what it does not claim.

```text
## Outcome
- Tasks: 2 accepted locally of 2 briefed (trim-empty-args at 50b50ff, version-flag at 6a12567); the driver says the direction held no more.
- Shutdown: confirmed.
- Status: no problem was found with the evidence that was read.
[...]
- Ran for 9m 18s, from 2026-11-24T04:13:06.500076+00:00 to 2026-11-24T04:22:24.664376+00:00.
[...]
- Findings left open by reviews that ended in ACCEPT: 4. They are listed under that heading below.
[...]
### version-flag (feature): accepted locally
- Result: accepted locally. The host's review of commit 6a125673c15f0b4df2f812f362b9bb06f19cfa7e ended in ACCEPT, and the review receipt the host wrote for it is kept, intact and in the consumed ledger. Nothing here shows it was pushed or merged.
[...]
  - Review 2, of the candidate, round 0: ACCEPT on commit 6a125673c15f0b4df2f812f362b9bb06f19cfa7e; 2 findings. Receipt: receipts/review-9b2ce2104a7d42b0-5b603b8d.json.
    - F1 (minor) at cli/\_\_main\_\_.py:21: tomllib needs Python 3.11+ but pyproject.toml declares no requires-python, so on older interpreters --version fails with ImportError (a traceback).
    - F2 (minor) at cli/\_\_main\_\_.py:48: Switching to argparse changes greeting behavior for dash-prefixed arguments: before, any argv was greeted verbatim; now \`--foo\` or \`-x\` exits 2 and \`--help\` prints usage.
```

## What you need

- macOS, Python 3.11 or later, `git`, and OpenSSH's `ssh-keygen`.
- At least one agent CLI, installed and signed in. Profiles for Claude Code
  (`claude`), Codex (`codex`) and Antigravity (`agy`) are built in;
  [agent profiles](docs/agents.md) says how to add another. Each of the
  three can take any seat.
- Better, two CLIs from different makers. The review is worth more when the
  reviewer's model is not of the builder's family. `sortie start` picks the
  reviewer for you ([who reviews](docs/who-reviews.md)); with one CLI the
  shift still runs and says `same-family review` wherever it names the review.
- Nothing else. No skill, no review script and no other tool has to be
  installed.
- Agent CLIs that stay as they are while a shift runs. An agent CLI that
  updates itself during a shift ends the work of a builder or a reviewer
  that uses it: the host refuses that seat's next start, and the shift
  builds or reviews nothing further with it.
  Another session of the same CLI on the machine can set the update off.
  This has cost two real shifts. Do not update agent CLIs while a shift
  runs, and turn their own updates off for its length where the CLI has such
  a setting ([the executable pin](docs/agents.md#executable-pin)).
- A Git repository to work in. Use one you can afford to have changed; see
  [what a shift can do to your machine](#what-a-shift-can-do-to-your-machine).

## Your first shift

1. Install sortie-kit, from a checkout of this repository, into a Python
   environment of its own. In the checkout:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install .
   ```

   `python3` has to be Python 3.11 or later; `python3 --version` says which
   it is. The `python3` that comes with Apple's developer tools can be older
   (3.9 where this was written), and the install then refuses: name a newer
   one in the first command, such as `python3.13`. The command is then
   `.venv/bin/sortie`. The steps below write `sortie`: run
   `. .venv/bin/activate` in the terminal you use, or write the path out.
   Another environment of your choice does as well.

2. Make a signing key with a passphrase, and the trust file that names it.
   This is done once, in your own terminal:

   ```sh
   sortie setup
   ```

   It starts `ssh-keygen`, which asks for a passphrase for the new key. Type
   the passphrase only at `ssh-keygen`'s own prompt, never into an agent. The
   command replaces nothing that is already there;
   [shift grants](docs/grants.md#developer-setup-in-your-own-terminal) says
   what it does and how to do the same by hand.

3. Write a direction file: plain text that says what you want done in this
   shift. It can live anywhere; the shift takes a copy. For example, in a file
   named `direction.md`:

   ```
   Add a --version flag to the command line tool. It prints the version from
   pyproject.toml and exits 0. Add a test for it.
   ```

4. Start the shift from your own terminal, not from inside an agent:

   ```sh
   sortie start --repo PATH --direction direction.md
   ```

   `PATH` is the top directory of the Git repository. An agent you asked to
   run a shift for you, or to try one, reads
   [when an agent runs it for you](docs/walkthrough.md#when-an-agent-runs-it-for-you).

## What you will see

- **Questions.** The command asks for what the command line did not say: the
  driver's agent and model, the builder's agent and model, and how long the
  shift may run. Who reviews is not asked; it is picked and shown in the
  summary you sign.
- **The signing summary.** It shows what you are about to sign in plain words,
  then the exact bytes, and `ssh-keygen` asks for your key's passphrase.
- **The driver in your terminal.** The first time an agent CLI starts in a
  repository, expect a question of its own first, such as whether you trust
  the folder. Read it before pressing Enter: its preselected answer can be
  the one that declines and exits, and a driver that exits ends the shift.
  `sortie start` prints one line saying so just before the driver's CLI
  takes the terminal, on every start, since it cannot know what a CLI has
  been told to trust.
  Once the driver has begun it is told not to wait for you: it decides within
  the direction, and the report lists what it decided on its own or could not
  settle. [What still needs you](docs/walkthrough.md#what-still-needs-you)
  says where a person is still needed.
- **The report.** When the shift ends (the driver exits, the time runs out, or
  you run `sortie stop --shift SHIFT` in another terminal, with the shift
  directory the signing summary showed), the command prints how it ended,
  the tasks, the shutdown, and where the report is.

The builder commits its work in the repository itself. The toolkit never
pushes. [What you will see](docs/walkthrough.md) is the full walk-through.

## What a shift can do to your machine

Each seat runs as you, under your own configuration of its agent CLI. The
toolkit decides which program starts, with which arguments and model, and you
sign exactly that. It does not decide what that program is then allowed to
do. The default `codex` builder can write anywhere in the target repository,
including `.git`, so it can change that repository's Git hooks and
configuration, which later run as you, and it can read anything your user
can read. The toolkit asks Codex for its workspace-write sandbox, which runs
the builder's commands without the network unless you start with
`--builder-network` or your own Codex configuration turns it on; Codex
itself still reaches its model. A project whose tests bind sockets, Unix
sockets included, needs `--builder-network` with a Codex builder (seen on
macOS with codex-cli 0.172.0, not checked on Linux). A `claude` builder gets no such option and
can do only what your Claude settings allow without asking. The built-in
`agy` builder gets none either and can do whatever your own agy
configuration allows without asking. Read
[what a shift can do to your machine](docs/machine.md) before you sign.

## Where to read more

- [What you will see](docs/walkthrough.md): the walk-through, and how a shift ends.
- [Who reviews](docs/who-reviews.md): how the reviewer is picked or named.
- [A complete example](docs/example.md): from an empty directory to a running shift.
- [What a shift can do to your machine](docs/machine.md): what each seat can reach.
- [Other ways to run it](docs/running.md): the flags, preparing only, the exit status.
- [What real shifts have shown](docs/evidence.md), and what none has.
- [Development](docs/development.md): the tests, the checks, the tree.
- [Grant setup and password isolation](docs/grants.md): the key, the trust file, signing.
- [Agent profiles](docs/agents.md): the built-in seats, and adding your own.
- [Driver lifecycle and builder checks](docs/lifecycle.md): what the seats and the host do.
- [The review contract](docs/review.md): what a reviewer is told and what is taken from it.
- [The receipt contract](docs/receipts.md): templates, validation, evidence and acceptance.
- [Shift reports](docs/reports.md): closeout, unknown evidence, regeneration.

## Status

An alpha. Every real shift so far was run on macOS by the project's own
developers, with keys made for testing, and most on small practice
repositories. No real shift has been run on Linux or with a key the person
uses for anything else; see
[what real shifts have shown](docs/evidence.md), and what none has.

## License

Sortie-kit is licensed under the [MIT license](LICENSE). That one file also
covers the tiger working agreement it ships: the working rules the builder
is given, copied unchanged into each shift as `tiger.md`
([driver lifecycle](docs/lifecycle.md)). It is the same owner's project under
the same licence.
The owner authorized the copied shift-driver code for this divergent public
instance. The private experiment named at the top continues separately; this
project does not modify or deploy to it.
