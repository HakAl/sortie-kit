# sortie-kit

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
itself still reaches its model. A `claude` builder gets no such option and
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
covers the tiger working agreement it ships, which is the same owner's
project under the same licence.
The owner authorized the copied shift-driver code for this divergent public
instance. The private experiment named at the top continues separately; this
project does not modify or deploy to it.
