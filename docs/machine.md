# What a shift can do to your machine

Each seat runs under your own configuration of its agent CLI. The toolkit
decides which program starts, with which arguments and model, and you sign
exactly that. It does not decide what that program is then allowed to do, it
installs no agent permissions, and it ships no permission files. The one
exception is in the arguments you sign: a built-in `codex` builder is started
in Codex's workspace-write sandbox, because a builder must write and commit,
and the built-in reviewers are started in their CLI's read-only or plan mode.

## Before the first shift: sortie doctor

`sortie doctor` says what a `sortie start` on this machine would find. It
asks no model anything, writes nothing, signs nothing and makes no shift.
It finds things out with the functions a start uses, and it is no gate: a
start checks everything again.

```sh
sortie doctor
sortie doctor --repo PATH --agent NAME
```

`--repo PATH` names the repository a shift would be for. A start requires
five things to lie outside that repository: a profile of your own, each
agent program, the key, the trust file and the signing tool. With `--repo`
doctor holds them to that, as the start would. Without it nothing is held
against any repository, the output says so, and a program that lies inside
a repository is run like any other. `--agent NAME` also looks at your own
profile of that name.

What it prints, in order:

- **What runs.** The version of sortie-kit, the Python, the directory the
  package runs from, and which file is the first `sortie` on your `PATH`:
  this installation's, or another file. A shell alias or function named
  `sortie` is invisible to any program, and with one in place
  `sortie doctor` never reaches sortie-kit at all. So when the output does
  not begin with `sortie-kit` and a version number, ask your shell with
  `type sortie`.
- **`git` and `ssh-keygen`.** Found or not found. They are not run.
- **One block for each built-in profile** (`claude`, `codex`, `agy`), and
  for each profile of your own named with `--agent NAME`:
  - whether the program is found. A built-in CLI you have not installed is
    said and is nothing to do by itself; the missing program of a profile
    you named is;
  - the file a shift would pin, with the start of its hash and its size,
    and whether it is a script. When `sortie start` would refuse the
    program (writable by others, or inside the repository named with
    `--repo`), the line says so in the start's words and **the program is
    not run**;
  - the first line the program prints for `--version`;
  - for each form the profile has (driver, builder, builder with the
    network on, reviewer), each flag the profile passes, with `listed` or
    `not found in the help`. For the built-in `codex` profile, whose
    headless forms begin with the subcommand `exec`, the help asked for is
    that of `codex exec`, and the word `exec` is looked for in the top help.
- **Who would review.** For each profile that could be the builder and each
  model family it can build as, the reviewer the start would pick, by the
  function the start picks with.
- **The key and the trust file**, where a start looks without `--key` and
  `--trust`: none, found, or what a start would refuse about them. The key
  is not opened for signing and no passphrase is asked.
- **To do before a shift**, one line for each thing that sets the exit code.

What its words mean, and do not:

- `listed` means the flag's name stands in the CLI's own help text as a
  whole word. It does not show that the flag works, or that it still means
  what it meant. The values passed with a flag (`workspace-write`, `plan`)
  are not looked for. No line says more than that.
- `not found in the help` means the name is not in that text. A start may
  then fail at that seat; [agent profiles](agents.md) says how to write a
  profile of your own with the flags your version takes.
- `help not known` means the CLI gave no help that could be read: it ended
  with an error, whose first line is shown, printed nothing, or did not
  end. The help of a subcommand that the top help does not list is not
  asked for, and the flags after it are not looked for. An error
  text is never read as a help text.
- Not shown at all: that a CLI is signed in, that a model answers, that you
  know your key's passphrase, and what your own hooks and permission rules
  will do to a seat ([your hooks](#your-hooks)). No CLI is asked to do any
  work.

How it runs the CLIs: each call is the pinned file with `--version` or
`--help`, in the directory `/` and not in your repository, with no terminal,
with your environment less the Git and SSH agent variables a seat does not
get either, and for at most 10 seconds. The CLI is started by a small
keeper of sortie-kit's own, the Python that runs `sortie`, in a session
that is not yours; the keeper leads the process group and stays until the
call is over, so that the group that is signalled is never another's. A
call that does not end is stopped with what it started in that process
group, named, and passed over, and what a call that did end left running in
the group is stopped too; a process it put into a session of its own is
out of reach and is left, and cannot hold the call. At most 256 KiB of a
help are read: a flag that is not in that part of a longer help is `not
known`, never `not found`. For the three built-in profiles that is seven calls,
well under a second in all where this was written and about 80 seconds at
the worst. A CLI may look for an update or
write a log of its own when it is asked for its help; sortie-kit cannot
prevent that. It was seen with one CLI: under a home directory that was
empty, codex 0.172.0 made a lock file in a `.codex` directory there when
it was asked for its help.

A profile of your own is looked at only when you name it, because looking
means running its program with `--version` and `--help`, and sortie-kit
cannot know what a program of yours does with them. It is never run with
the leading words of its forms: a flag that is not in its top help is then
`not known from the top help`, which is said and is not held against it.

Exit codes: 0 when nothing was found to do, 1 when the closing list has a
line, 2 for wrong usage, 130 when interrupted. Something that could not be
found out, such as a help that did not end, is a line of the list like
anything else. On a machine with an agent CLI and no key yet, the list has
exactly one line, to run `sortie setup`.

## The builder

What the default `codex` builder is started to do, in plain words:

- write anywhere in the target repository, including `.git`, so it can commit
  and can also change that repository's Git hooks and configuration, which
  later run as you;
- read anything your user can read, inside the repository or not.

It is not given the network: Codex's workspace-write sandbox runs the
builder's commands without it unless your own Codex configuration turns it
on. A builder without the network cannot install a package from a remote index,
fetch a page, or clone or pull from a remote; Codex itself still reaches its
model. The same setting decides one thing that is not the network as most
people mean it: in a real shift on macOS with codex-cli 0.172.0, a test that
binds a Unix socket in the temporary directory failed in the builder's
sandbox with `PermissionError: [Errno 1] Operation not permitted`, and the
same test passed in a shift started with `--builder-network`. So a project
whose tests bind sockets, Unix sockets included, needs `--builder-network`
with a Codex builder. That refusal is the Codex CLI's sandbox, not the
toolkit's, and it has not been checked on Linux. When the
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
[what the built-in seats can reach](agents.md#what-the-built-in-seats-can-reach)
for what stops working without each.

A `claude` builder gets no such option and can do only what your Claude
settings allow without asking.

The built-in `agy` builder gets no permission, mode or sandbox option either.
It can do whatever your own agy configuration allows without asking, and
sortie-kit neither reads nor limits that configuration.

## An agent CLI that updates itself

The grant signs the bytes of the file at each agent's path. Once an agent
CLI has written that file again after the shift was admitted, the host refuses the next
start of that seat, and the shift can do no more work with it: also when the
file has the same bytes as before, since the host looks at the file before a
start and does not read it again. The report gives the reason; see
[the executable pin](agents.md#executable-pin). Start a newly signed shift.

This has ended two real shifts. Another session of the same CLI on the
machine can set the update off, so do not update agent CLIs while a shift
runs, and turn their own updates off for its length where the CLI has such a
setting. When a seat's pinned file is a script that starts the real program,
the pin covers the script alone: a line under the signing summary says so,
and an update that leaves the script as it is goes unseen. The driver is
started once, so a rewrite of its file after that start is not looked at.

## The driver

The driver is an interactive agent in your terminal under your own settings,
like any session you start yourself.

## The reviewer

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

## Your hooks

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
`sortie doctor` does not look at hooks and cannot show this: a hook acts on
the driver's own session, in the repository, on the command with the shift's
path in it, and nothing short of a shift is that.

A hook that keeps an agent inside the project meets one more thing. The
builder's temporary files go to
[a directory the host makes for the shift](lifecycle.md#the-temporary-directory-of-builder-turns-and-reviews)
under `/tmp`, outside the repository. A test run reaches it through `TMPDIR`
and names no path on its command line, so a hook that reads command lines has
nothing to refuse, and the toolkit's own tests run a builder's check that
way. A command that names the directory can be refused by such a hook, and
builder.md tells the builder not to name it. If you want your agent to be
able to, allow paths that begin with `/tmp/sortie-` and, on macOS,
`/private/tmp/sortie-` in your own hook's configuration; the signing summary
shows the base and the report the full path. The toolkit reads and changes no
hook. In the first shift a developer signed in person the temporary files
were still inside the repository, the project's tests could not pass there,
and the builder, kept inside the project by such a hook, rightly did not go
round it.

The host tries to remove that directory at the end of the shift. When it
could not, the report says where the host made it and why it was left, under
Outcome, and to look at what is there before removing anything.
