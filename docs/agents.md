# Agent profiles

`sortie start` signs these values into a format 4 grant (see
[grants](grants.md)). From them the host starts the driver interactively, the
builder headless, and the reviewer headless in its review form (see
[the lifecycle](lifecycle.md)).

A profile tells the toolkit how to start one agent CLI. A seat is no longer
limited to `codex` or `claude`: any CLI with a profile can take a seat.

A seat runs under your own configuration of that CLI. The toolkit supplies no
permission file, fills none, and signs none. What a seat may do is whatever
your setup of that CLI allows, with the exceptions that you see and sign
as part of the arguments: the built-in `codex` builder asks Codex for its
workspace-write sandbox, and each built-in reviewer asks its CLI for its
read-only or plan mode (see [what the built-in seats can reach](#what-the-built-in-seats-can-reach)).

## Profile file

```json
{
  "version": 1,
  "name": "claude",
  "executable": "claude",
  "interactive_argv": ["--model", "{model}", "{prompt}"],
  "headless_argv": ["--print", "--model", "{model}", "{prompt}"],
  "models": {"claude-sonnet-5-5": "claude"},
  "default_model": null,
  "review_argv": ["--print", "--permission-mode", "plan", "--model", "{model}", "{prompt}"],
  "review_model": "claude-sonnet-5-5"
}
```

- `name` is a short lower-case token and must equal the file name.
- `executable` is a command name looked up on `PATH` when you sign, or an
  absolute path.
- The argv templates list arguments only. The program itself is always the
  pinned executable and is never taken from template text. A profile with no
  `headless_argv` cannot be a builder; one with no `interactive_argv` cannot be
  a driver.
- `headless_network_argv` is optional: a second, whole headless template
  that a builder is started with when the start is given
  `--builder-network`. It needs a `headless_argv` beside it and must differ
  from it. The two must also differ as they are started: when they are one
  command once the builder's model or the repository's path stands in its
  place, the flag is refused for that start. A builder whose
  profile has none refuses that flag before anything is prepared or signed.
  Of the built-in profiles only `codex` has one.
- `review_argv` and `review_model` are optional and come only together; one
  without the other is refused. `review_argv` is the form the reviewer is
  started in: headless, and read-only where the CLI has such a mode.
  `review_model` is the model a reviewer runs with when it was picked or named
  without a model; it must be listed in `models` or be covered by
  `unlisted_family`. A profile without them cannot be the reviewer.
- Placeholders are whole arguments: `{prompt}` and `{model}` exactly once,
  `{cwd}` at most once. For the reviewer `{cwd}` is the directory the host
  made for that run of the review, not the repository. `--model={model}` is refused, so a filled value can
  never split into or merge with another argument. No shell is involved.
- Any other argument is passed exactly as written and is signed with the rest
  of the template. A profile of your own may therefore name fixed options,
  including a settings file path; the toolkit signs that text, not the file's
  content.
- `sortie doctor --agent NAME` looks at a profile of your own before any
  shift: whether it loads, which file would be pinned, and whether its
  flags are listed in the program's top help
  ([before the first shift](machine.md#before-the-first-shift-sortie-doctor)).
- `models` maps each model ID to its family. `default_model` may be left out or
  set to `null`; when given it must be listed.
- `unlisted_family` is optional: the family of any model the profile does not
  list. Use it for a CLI whose models all come from one maker, so a new model
  ID needs no profile change. Leave it out when the CLI runs models of several
  families.

Built-in profiles ship for `claude`, `codex` and `agy`. Add your own as
`~/.config/sortie-kit/agents/NAME.json`. That file must be yours, a regular file
reached through no symlink, outside the target repo, and not group or world
writable. A custom file cannot reuse a built-in name.

## Model and family

No model is assumed for the driver or the builder. Such a seat's model comes
from your choice or the profile's `default_model`; with neither, a start in a
terminal asks and any other start refuses. The reviewer's model is the one
you named or the profile's `review_model`, and it is never asked for. A model missing from the profile's `models` takes the profile's
`unlisted_family`; without one its family must be stated (`--family
ROLE=FAMILY` with the role `driver`, `builder` or `reviewer`, or the question
a guided start asks for the driver and the builder). A stated family decides
only where the profile says nothing. It may not contradict the profile for a
listed model, nor a profile's `unlisted_family` for an unlisted one: no
statement makes a model on the built-in `claude` profile anything but
`claude`, or one on the built-in `codex` profile anything but `gpt`. A CLI
that you run with models of another family needs a profile of your own
without `unlisted_family`. A maker's name is the
same family as its model line: `openai` is read as `gpt`, `anthropic` as
`claude`, `google` as `gemini`, wherever a family is stated or listed, so the
two spellings can never pass as different families in the review policy. Families are canonical lower-case tokens.

## The reviewer seat

The reviewer is a third seat, signed like the other two, and it is picked
unless you name it. Nothing asks who reviews.

- Picked: among the profiles that have a review form and whose executable is
  found, in the order `claude`, `codex`, `agy`, then your own profiles by
  name, the first whose family differs from the builder's. Its model is the
  profile's `review_model`.
- When none of another family is found: the builder's own CLI (or, when that
  profile has no review form, the first installed profile that has one), and
  the review is a same-family review. No flag is needed. The signing summary, the
  terminal and the report say so with the reason.
- Named: `--agent reviewer=NAME[:MODEL]`, with `--family reviewer=FAMILY` when
  the profile does not give the model's family. A named reviewer of the
  builder's own family, while one of another family is installed, is refused
  without `--allow-same-family-review`. That flag is refused when the two
  families differ.
- No profile that can review with a found executable: the start refuses before
  anything is prepared or signed.

The signing summary shows the reviewer with how it was chosen (`picked: the
first installed agent of another family than the builder`, `picked: no agent
of another family is installed`, or `given with --agent reviewer=`), its full
command, the limit of one run, and what it can reach.

## Executable pin

At signing the executable is resolved to its real path and hashed. The file must
be outside the target repo, a regular file that you can execute, and not group
or world writable. It is never run to identify it; there is no `--version` probe. The pin
is `{path, sha256, bytes}`. Admission hashes it again, and each start compares
device, inode, size, modification time and change time with what admission saw.

Limits: a launcher script pins only itself, not the program it goes on to
start. The comparison before a start is a check of the file at that path, made
first and again last among the start's checks. It is not an atomic start of an
unchangeable file: keep agent programs where seats and other processes cannot
replace them.

An agent CLI that updates itself is such a replacement. The grant signs the
bytes of the file at each seat's path, so once a CLI has written that file
again after the shift was admitted, the host refuses the next start of that
seat. For a builder it refuses each new turn with `builder agent executable
replaced or rewritten after admission: the file at the pinned path is
another file or has other size or times; its bytes were not read again`: the
shift builds nothing further, the driver is told why, and the report gives
the reason once the shift is closed. A file that can no longer be looked at
is refused with `builder agent executable cannot be looked at after
admission`. A reviewer that changed fails its reviews, which blocks the
task, and a program that changed before the shift was admitted stops the
start with a message and no report; neither has been seen on a real shift.
The remedy is to start a newly signed shift, which pins the program as it
then is.

The words say what was compared because the bytes are not. Before a start
the host does not read the program again, which for some agent CLIs is
several hundred megabytes: it looks at the file, and a file written again
with the very same bytes is another file or has other times, and is refused
like any other. That is meant. This has ended two real shifts. In the first
`agy` replaced its program seconds after the driver started, before anything
was built. In the second the pinned file of `codex` was a launcher script
that an update wrote again with the same bytes, while the program it starts
went to a new version: the rewrite was all the host could see of that
change, and a host that had hashed the script again and gone on would have
run a program nobody signed. Builds from before this one gave the reason as
`builder agent executable changed after admission` in both cases.

When a seat's pinned file is a script, one that begins with `#!`, the lines
under the signing summary that are marked as not signed say so: which seats
have it, that the pin covers that one file and not its interpreter or any
program the script starts, that an update of the CLI which writes the file
again ends the work of a builder or a reviewer in the shift, and that an
update which leaves the file as it is goes unseen by the host. The host
looks at a seat's file before each start of that seat, and the driver is
started once, when the shift begins: a rewrite of the driver's file after
that is not looked at, and the line says so. The line is read from the file as it is when the
summary is shown, the file is not run, and nothing of it is in the grant.
Two limits: `#!` shows that the file is a script, not what runs it or what
it starts, so neither is named; and a seat without such a line is not pinned
any further than its one file either, since a native program can load other
files.

An agent CLI can be updated by another session of the same CLI on the
machine, or by a package manager, and not only by a seat of the shift. So:
do not update agent CLIs while a shift runs, and where a CLI has a setting
that turns its own updates off, turn them off for the length of a shift.
sortie-kit sets nothing of the kind for the seats.

## What the built-in seats can reach

A builder has to write: receipts and logs in the shift, the source it changes,
commits, checkouts under the shift's `scratch/`, and temporary files in
[the directory the host makes for the shift](lifecycle.md#the-temporary-directory-of-builder-turns-and-reviews),
which is outside the repository. It does not start or call
the reviewer; the host does.

- **`codex` as builder** is started as

  ```text
  codex exec --sandbox workspace-write --add-dir .git
    --model MODEL --cd REPO PROMPT
  ```

  and, when the start is given `--builder-network`, with
  `-c sandbox_workspace_write.network_access=true` after `--add-dir .git`.

  In the real run that led to this, `codex exec` without these ran in a
  read-only sandbox and the builder could write nothing. That default comes
  from Codex and your Codex configuration, not from the toolkit. Each addition
  was checked with codex 0.171.0 in a throwaway repository on one machine:
  - `--sandbox workspace-write` lets it write inside the repository (which
    holds the shift) and its temporary directory, which was then the shift's
    `scratch/`. Without the next one it
    still cannot commit, and without the last it cannot reach the network.
  - `--add-dir .git` makes the repository's own `.git` writable, which the
    sandbox otherwise keeps read-only even inside the workspace. Commits and
    `git worktree add` need it. It also means the builder can change Git hooks
    and configuration in that repository, which later run as you. The path is
    relative to the repository, where the builder is started; a repository
    whose `.git` is a file (a linked worktree or a submodule) keeps its Git
    data elsewhere and its builder cannot commit under this profile.
  - `-c sandbox_workspace_write.network_access=true` allows outbound network
    in the commands the builder runs. It is passed only when the start is
    given `--builder-network`; the profile keeps it as a second headless
    form, `headless_network_argv`. Without it the builder cannot install a
    package from a remote index, fetch a page, or clone or pull from a
    remote, unless your own
    Codex configuration turns the network on; Codex itself still reaches its
    model. Without it the builder's commands also cannot bind a socket, a
    Unix socket in the temporary directory included: in a real shift on
    macOS with codex-cli 0.172.0 a test that does so failed there with
    `PermissionError: [Errno 1] Operation not permitted` and passed in a
    shift started with `--builder-network`. A project whose tests bind
    sockets needs the flag with a Codex builder. Not checked on Linux.
    The signing summary and the report say which it is, as what the
    option asks of Codex and not as something the toolkit enforces. The flag
    is asked in no question, and it is refused before anything is prepared
    for a builder whose profile has no such form.

  These are what the toolkit asks for. They are not the whole of what
  the builder gets: Codex adds whatever your own Codex configuration already
  grants, for example further writable directories, and the toolkit neither
  reads nor limits that. In the check above, with that machine's
  configuration, a write to the home directory was still refused. Codex
  reported the writable places as the repository, `/tmp`, the
  turn's temporary directory (then the shift's `scratch/`) and that `.git`. The
  directory the host now makes for a shift's temporary files is the turn's
  `TMPDIR` and, unless `/tmp` could not be used, under `/tmp`, so it is on
  that list, usually twice over; the toolkit's own tests
  run no real agent CLI, and what was seen is three real shifts on macOS,
  two with a Codex builder (codex-cli 0.172.0) and one with a Claude
  builder, in which the builder's checks ran in that directory and the host
  removed it at the end. Reading is not confined: the builder can read whatever your user
  can, and with the network on that is a way for file contents to leave the
  machine. Approvals stay as Codex sets them for `exec` (nobody is asked;
  what the sandbox refuses fails). No built-in profile uses an option that
  turns a sandbox or approvals off.

  To run a narrower or wider builder, copy the profile under a new name to
  `~/.config/sortie-kit/agents/` and change the arguments; they are shown in
  the scope you sign.
- **`codex` as driver** and **`claude` as driver or builder** are started with a
  model and a prompt and nothing that selects permissions. The interactive
  driver asks you in the terminal when it needs approval.
- **`agy` as driver** runs `agy --prompt-interactive PROMPT --model MODEL`,
  and **`agy` as builder** runs `agy --print PROMPT --model MODEL`. Both were
  written from `agy --help`, which says of `--prompt-interactive` "Run an
  initial prompt interactively and continue the session" and of `--print`
  "Run a single prompt non-interactively and print the response". Neither
  form passes `--mode`, `--sandbox` or any permission option, so the built-in
  `agy` builder can do whatever your own agy configuration allows without asking.
  The summary you sign says so, of exactly that form. sortie-kit has not tried what `agy --print`
  does when its configuration would have it ask. `--builder-network` is
  refused for an `agy` builder, since the profile has no form with the
  network on. With `agy` a builder's or a reviewer's log holds its final
  message only: that is what the real shifts with `agy` in those seats
  kept.
- **`claude` as builder** runs `claude --print`. Nobody can answer a
  permission prompt there, so anything your Claude settings do not already
  allow without asking is denied, including file edits and shell commands
  under default settings. The built-in profile adds no permission option; a
  `claude` builder works only if your own settings allow what the work needs
  (or through a profile of your own with the options you choose). Real
  shifts have run a `claude` builder, on machines with their owners' own
  Claude settings; what is denied under default settings is from
  `claude --help` (2.1.330) and was not tried.
- **The reviewer**, whichever profile it comes from, is started by the host
  in a directory of its own under the shift's `reviews/`, never in the
  repository. That directory holds `REVIEW.md` with the instructions, the
  brief, an export of the files (`git archive`, so there is no `.git` in it)
  and, in a repair round, the earlier findings and the builder's answers. For
  the review of a commit the export is of that commit and the task's whole
  patch is beside it, with the plan when the task had one. For the review of
  a plan the export is of the commit the repository is at (empty when there
  is none yet), with the plan and no patch. The prompt on the
  command line is a few lines that point at those files. The built-in forms:

  ```text
  claude --print --permission-mode plan --model MODEL PROMPT
  codex exec --sandbox read-only --model MODEL --cd REVIEW PROMPT
  agy --print PROMPT --mode plan --sandbox --model MODEL
  ```

  Each asks its CLI for the mode that CLI offers for reading. These forms were
  written from each CLI's own help output. Real shifts ran each of the three
  forms as the reviewer, the `codex` form with the model `gpt-6.1-sol` that a
  picked `codex` reviewer runs with, and every review came back in the
  contract's form. sortie-kit has not checked what each mode stops, and the
  summary you sign says so.
  The reviewer runs as you with your sign-in for that CLI, can read whatever
  your user can read and can use the network as its CLI allows. A profile of
  your own without a read-only form can write. What the host does check: the
  repository's commit and status and the names, sizes and times of the files
  in the shift's `receipts/` are compared before and after each run, and a
  difference fails that run of the review.
- All three built-in profiles can take any seat. The built-in `agy` profile as the
  driver and as the builder has been shown on one real shift: two tasks
  accepted on a practice repository, with a key made for the test. A profile
  of the developer's own with the same forms ran one longer shift before
  that.
- `agy` serves models of more than one maker. The built-in profile lists four
  `gemini-4` models in the family `gemini`, the six `claude` models `agy
  models` showed when the profile was written in the family `claude`, and
  one `gpt` model, and names no family for a model it does not list. The
  family is the model's, not the CLI's: an `agy` builder on one of those
  `claude` models is of the family `claude`, so `claude` is not picked to
  review it as if it were of another family, and `--family` cannot say
  otherwise. For a model the profile does not list, the family is the one
  you state, and you sign it; sortie-kit does not guess a family from a
  model's name.

### What the summary says of a profile of your own

The lines under "what the reviewer can reach" and "what the builder may do"
describe the signed command line and nothing else.

- **A reviewer.** The note on the read-only form is given when the seat starts
  a CLI sortie-kit knows and its arguments hold that CLI's exact run of
  options (`--sandbox read-only` for `codex`, `--permission-mode plan` for
  `claude`, `--mode plan --sandbox` for `agy`). Which CLI it is, is taken
  from the profile's name for a built-in profile, and otherwise from the file
  name of the pinned program: a profile `my-agy` whose program is `.../agy`
  gets the `agy` note. The profile's name is asked first because the pinned
  file is the resolved one, which a CLI may name by its version (`claude`
  installs as `.../claude/versions/N`). A name is no proof of what a program
  is, and the note says only what the option asks of that CLI. For a profile
  of your own it is followed by `a profile of your own: sortie-kit has not
  inspected what its other arguments allow`, since another argument beside
  the run may undo it. Any other reviewer gets `sortie-kit has not inspected what these arguments allow;
  this reviewer may be able to write`.
- **A builder.** The built-in `codex`, `claude` and `agy` profiles get the
  lines above, including what they do not pass. A profile of your own whose pinned
  program is named `codex` and that passes at least one of the three `codex`
  options gets a line for each one it passes, and then `a profile of your
  own: sortie-kit has not inspected what its other arguments allow`. Nothing is said about what it leaves out,
  since its other arguments were not read. Every other builder profile of
  your own, one on `claude` or `agy` too, gets `a profile of your own: sortie-kit has
  not inspected what these arguments allow`.

When a builder turn ends without anything new under `receipts/`, the turn
record says so (`receipts_changed: false`) and `sortie wait` adds a `note`
that names the turn log to read. The host sees a directory listing, not the
reason. The toolkit does not otherwise check
what a seat is allowed to do.
