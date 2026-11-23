# Grants

A grant binds exact signed JSON bytes to a canonical repository, shift ID,
random nonce, direction digest, the pinned agent start for each seat, the builder and review
models with their families, the limit of one run of the reviewer, allowed local actions,
start/expiry, a two-minute maximum admission window, and a ten-second maximum
report-only finalization bound. It does not authorize publishing. A format 4
grant names exactly three seats: a driver, a builder and a reviewer. There is
no pane. Existing signed grants are never rewritten.

## Developer setup, in your own terminal

Install sortie-kit and OpenSSH outside the target checkout, in locations your
seats cannot modify. Create an ordinary encrypted SSH key and protected trust
configuration outside every agent-writable workspace. For example:

```sh
mkdir -p "$HOME/.config/sortie-kit"
chmod 700 "$HOME/.config/sortie-kit"
ssh-keygen -t ed25519 -C sortie-kit -f "$HOME/.config/sortie-kit/signing-key"
```

Enter a nonempty passphrase at ssh-keygen's terminal prompts. Do not enter it in
chat, an agent pane, an argument, an environment variable, a file, or a script.
Then create the public trust configuration in that same developer terminal:

```sh
sortie trust init
```

This reads `signing-key.pub` beside the key, finds `ssh-keygen` on your `PATH`,
and writes `~/.config/sortie-kit/trust.json`, owner-only: a small JSON file
with `version`, `identity` (`developer` unless you pass `--identity NAME`),
`public_key` and the resolved `ssh_keygen` path. `--key PATH` and `--out PATH`
name other locations. It opens no private key, signs nothing, and never
replaces a file: if the trust file exists it refuses and leaves it alone. The
file appears under its name only once it is complete, and the directories of
the key and of the trust file must be real directories, not links.
`sortie start` uses these two files without being told when they are there,
and prints which files it took. `--trust FILE` and `--key FILE` name others,
each on its own.

The identity is one literal value, not a pattern or signer registry supplied by
the grant. Key and trust files must be owner-only regular files outside the
target repository; symlink paths are refused. The configured executable is
explicit, outside that repo, and pinned during signing/admission. Certificates,
agent-only keys, unencrypted private keys and hardware key formats are refused
by this default mode. Hardware presence remains a future optional mode: this
version makes no unsupported touch or user-verification claim.

## Sign, verify, withdraw

```sh
sortie start --repo PATH --direction FILE --for 1h \
  --agent driver=claude:DRIVER_MODEL --agent builder=codex:BUILDER_MODEL \
  --sign-only
```

`--agent ROLE=NAME[:MODEL]` picks an [agent profile](agents.md) and the exact
model for a seat. The roles are `driver`, `builder` and `reviewer`. The
reviewer need not be named: without `--agent reviewer=` it is picked from the
installed profiles that can review, the first of another family than the
builder, or the builder's own CLI when there is no other (the first installed
profile that can review when the builder's own cannot; see
[the reviewer seat](agents.md#the-reviewer-seat)). `--review-limit DURATION`
is the longest one run of the reviewer may take, from 30 seconds to an hour,
ten minutes when not given. `--family ROLE=FAMILY` states a family when
the profile neither lists the model nor names a family for models it does not
list. The built-in `codex` profile lists no model and places any model in the
family `gpt`; `claude` places an unlisted model in `claude`; `agy` names none.
The family is shown in the summary before you sign. The toolkit assumes no
model for the driver or the builder. `--review MODEL` and `--family review=`
were replaced by the reviewer seat and are refused with a line that names the
new spelling.

In a terminal, a start that lacks any of these choices or `--for` asks for
them, in the order driver agent and model, builder agent and model,
duration, and for a family where one is needed. Nothing about the reviewer is
asked. The questions come
before anything is prepared, read the terminal only, and take no secret; three
unusable answers or the end of input end the start. Without a terminal the
command exits 2 and lists what is missing. `--trust` and `--key` are never
asked for: without the flag, the file `sortie trust init` writes is used when
it exists, and otherwise the start ends listing it as missing.

A signing start settles who reviews in two steps. Before any question is
asked it checks that a reviewer can be had: a named reviewer's profile must
exist, have a review form and a known family, and when none was named at
least one installed profile must be able to review, else it refuses in one
sentence. After the questions and before anything is prepared or signed it
picks the reviewer against the builder's family, applies the same-family
rule and pins the reviewer's program. Only the presence
of the program is checked. That the CLI is signed in is not checked here; when
it is not, its reviews fail inside the shift and the tasks they belong to are
blocked. No path under any skills directory is looked up and no review script
is needed.
`--prepare-only` asks nothing. The earlier `--driver`, `--builder`, `--runtime`,
`--builder-model`, `--builder-family`, `--review-model` and `--review-family`
flags are refused with a pointer to these.

A format 4 grant signs, for each seat: the agent name and profile digest, the
argument template, model, family, mode (`interactive` for the driver,
`headless` for the builder, `review` for the reviewer), and the executable pin
`{path, sha256, bytes}`. It also signs `review_limit_seconds`.
It signs no permission or settings file: each seat runs under your own
configuration of its CLI, and the grant makes no statement about what that
configuration allows.

The argument template is signed as text, and for some built-in seats that
text selects permissions: the built-in reviewers are started in their CLI's
read-only or plan mode (see
[what the built-in seats can reach](agents.md#what-the-built-in-seats-can-reach)),
and the built-in `codex` builder's arguments are
`exec --sandbox workspace-write --add-dir .git --model MODEL --cd REPO PROMPT`,
shown in the scope before you type your passphrase. Started with
`--builder-network` they also carry
`-c sandbox_workspace_write.network_access=true` after `--add-dir .git`,
which is what a grant signed by an earlier version always had; such a grant
still verifies and is reported as it was signed.

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
[what the built-in seats can reach](agents.md#what-the-built-in-seats-can-reach)
for what stops working without each.

It also carries a `review_policy`: the builder model and family, which must
equal the builder seat's; the review model and family, which must equal the
reviewer seat's; `same_family_review`, which is true exactly when the two
families are equal; and `review_choice`, how the reviewer came to be that one
(`picked-other-family`, `picked-same-family` or `given`). Families are
canonical lower-case tokens; `claude` and `claude-opus` are different, so name
them consistently. A same-family review is signed without a flag when it was
picked because no agent of another family is installed. A reviewer of the
builder's family that you named while another family was installed needs
`--allow-same-family-review`. Receipts are later checked against these signed
values.

Allowed actions in format 4 are `local-work`, `spawn-driver`, `spawn-builder`
and `spawn-reviewer`. Format 1 (no models), format 2 (pane seats) and format 3
(two seats, with the review made by the builder through a review script)
grants keep valid signatures: `sortie verify` still checks them under the
rules they were signed with and says they cannot be admitted. Sign a fresh
shift.

Signing shows a plain-words summary of the grant (repository, window and working time, each seat's agent, model and full command, how the reviewer was chosen and the limit of one run, what the builder may do, what the reviewer can reach), then the exact bytes being signed, and invokes `ssh-keygen -Y sign` in the developer's
foreground controlling terminal. Python never reads the password. Signing
requires unredirected terminal streams, explicitly disables/checks terminal
echo, uses the encrypted private key directly, and removes inherited agent,
askpass, provider and preload environment paths. No agent is started by
this command. Signing consumes the requested finite duration; expiry, signal
interruption, or source/trust/key change stops the signer. Terminal settings and
signal handlers are restored on exit. There is no password flag or fallback to
piped input. If the environment cannot control terminal echo, signing refuses.

```sh
sortie verify --shift PATH/.sortie/shifts/SHIFT --trust TRUST_JSON
sortie stop --shift PATH/.sortie/shifts/SHIFT
```

Verification uses the protected identity and a dedicated SSH signature namespace.
It verifies snapshots of the exact bytes, rejects replacement during verification,
and checks repository, direction and scope. It does not admit the shift or
prove password entry.

`sortie verify` reports two facts apart, so a shift can still be checked after
its window has passed:

- `Signature: verified.` The bytes of `grant.json` and its scope are as signed,
  and the shift was signed by the key in the trust file.
- `Window:` whether the signed window is open now: `current until TIME`,
  `ended TIME`, `not begun`, or open and the shift was withdrawn.

| Exit | Meaning |
| --- | --- |
| 0 | The signature is good and the window is current. |
| 4 | The signature is good and the window is not current: it ended, has not begun, or the shift was withdrawn. This answers "was this shift signed by this key" with yes. Nothing is admitted under the grant now. |
| 3 | Refused: a bad or missing signature, changed bytes, a changed repository or direction, a wrong trust file, or a window that is not sound in itself. Only the reason is printed, on standard error. |

Admission keeps its own rule and never uses the looser check: a grant whose
window has ended, or whose shift was withdrawn, is never admitted.

`--shift` may be a relative path and may hold `.` and `..`, as in `sortie
verify --shift ../other/repo/.sortie/shifts/ID`. Three things are refused,
each with its reason: a path with a symbolic link in any part of it as
written, also one whose `..` leads back out of the link (give the shift by
its real path); a path through a directory that is not there
(`absent/../repo/...`); and a path whose last part is not the shift
directory's own name (`.../shifts/ID/logs/..`, or `.` from inside the
shift; a trailing `/` or `/.` after the name is taken).

Stop creates a local `STOP` marker and contacts no agent;
existing or malformed markers also withhold authority. Stopping the toolkit
does not prove agent cessation.

`--prepare-only` remains unsigned/offline. `--sign-only` produces a signed grant
without admission. Without either flag the same command signs, admits at once
and runs the shift (see [the lifecycle](lifecycle.md)); nothing else admits a
grant, so a grant made with `--sign-only` is for inspection. A grant can be
verified while current, but a **first admission must occur within two minutes**
of its signed start (or before its earlier expiry). Sign a fresh shift when this
window passes; there is no resume switch that extends authority.

## Admission and seat starts

Admission is internal. One public command reaches it: `sortie start` without
`--prepare-only` or `--sign-only`, which signs and then admits at once. It holds one
foreground owner lock per repo and consumes each nonce once in
`.sortie/admissions/`. Consumed markers remain after exit and crashes. A second
admission, even after clean shutdown, refuses. Before the nonce is consumed,
admission hashes each seat's executable again. It then
compares every executable with the observation it just made, and runs the same
checks every later guard runs, apart from the not yet written nonce marker:
remaining duration, toolkit sources, trust, scope and time, the stop marker,
the grant files, the owner lock, and the duration once more after those reads. A difference found
by any of these refuses and leaves the nonce unused. A failure after the nonce
is written (publication itself, or the checks that follow it) keeps the nonce
consumed; sign a fresh shift.

The live process pins the grant/signature, trust, source manifest, owner inode,
and consumed marker. Its monotonic budget cannot grow if wall time rolls
backward. Changing or removing the observed authority produces a sticky refusal.

The argv for a seat comes only from `Admission.seat_start(role, prompt)`. For
the reviewer the caller also names the working directory, which must be a real
directory the host made under the shift's `reviews/`; no other seat is started
outside the repository. It
requires the matching signed `spawn-ROLE` action, rechecks the admission guard
(binding, stop marker, time), compares the executable's device, inode, size,
modification and change time with what admission saw, fills the signed template, checks the guard once more, and
compares the executable a last time. The caller must start the returned argv
at once; see the executable pin limits in [agent profiles](agents.md).
Nothing but the host-authored prompt comes from the caller. Allowed local work
remains bounded by the developer's direction and the seats' own harnesses; the
toolkit does not install or infer harness permissions.

## Presence boundary and evidence limits

A signature proves key use, not that a human typed a password. Presence depends
on using this trusted signing path and a terminal that agents cannot observe or
drive. An agent's PTY is also a terminal: `isatty` cannot identify a human. The
developer must run signing outside any agent session and deny seats access to that
terminal, signing command, private key, trust writes, and installed toolkit/tool
writes. This is a required harness boundary, not a property of Unix owner mode
bits. An unrestricted malicious same-user process can bypass it; this toolkit
cannot establish isolation from that process. Keep grant/signature files, the owner lock, and the consumed-nonce
ledger outside seat write allow rules; deleting all replay evidence cannot be
detected by a fresh process using only that evidence. Receipt directories are
separate from these authority files. A leaked password requires key
rotation, trust replacement and withdrawal of grants under the old identity.

Tests generate random synthetic secrets in memory and type them only into fresh
PTY fixtures. They check the actual `sortie start --sign-only` path, forbid Python
password readers and agent starts, inspect signer argv/environment/terminal file
descriptors, and scan every generated fixture file and terminal transcript for
the synthetic secret. Other tests exercise real SSH signature failures, scope,
expiry, nonce replay, ownership, replacement, pin changes, and withdrawal before a seat start.
These demonstrate the implementation's channels under the stated boundary; they
do not certify a developer's external harness configuration. Some sandboxes
block terminal echo control; execute these synthetic PTY tests only with that
capability explicitly available, never by suppressing the isolation assertions.

References: [OpenSSH ssh-keygen](https://man.openbsd.org/ssh-keygen) for SSHSIG
sign/verify and allowed-signers namespaces; [OpenSSH private-key envelope](https://github.com/openssh/openssh-portable/blob/master/PROTOCOL.key)
for encrypted-key header inspection. No cryptographic primitive is reimplemented.
