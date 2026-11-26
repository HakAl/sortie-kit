# What a shift can do to your machine

Each seat runs under your own configuration of its agent CLI. The toolkit
decides which program starts, with which arguments and model, and you sign
exactly that. It does not decide what that program is then allowed to do, it
installs no agent permissions, and it ships no permission files. The one
exception is in the arguments you sign: a built-in `codex` builder is started
in Codex's workspace-write sandbox, because a builder must write and commit,
and the built-in reviewers are started in their CLI's read-only or plan mode.

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
