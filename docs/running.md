# Other ways to run it

The flags of `sortie start`, preparing or signing without starting, and what
the exit status means. [Your first shift](../README.md#your-first-shift) has
the guided form.

## Flags in place of questions

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
[what a shift can do to your machine](machine.md)).

## Keeping the machine awake

While a shift runs, the host asks the system not to put the machine to sleep
because it is idle, on macOS with a power assertion of its own. If the system
refuses, the shift runs all the same: the refusal is recorded, and the report
says the machine was not kept awake for the whole shift. The code has a path for
Linux as well, through `systemd-inhibit` when that is installed; it has not
been run on a Linux machine outside the tests, which put a stand-in in that
program's place, and Linux is not supported yet. Anywhere else
there is no hold. That is idle sleep only. Closing a laptop lid or choosing
Sleep by hand still sleeps the machine, and the signed window keeps running while it sleeps. A
line under the signing summary says what this run will do, and the report
says whether the hold lasted the whole shift. `--no-wake` turns it off; it
goes only with a start that runs the shift, not with `--prepare-only` or
`--sign-only`.

## The key and the trust file

`sortie start` uses the key and trust file from step 3 of
[your first shift](../README.md#your-first-shift) when they are
where that step puts them, and says which files it took. `--trust FILE` and
`--key FILE` name others. Neither is ever asked for: when one is missing the
start ends before any question and says so.

## Preparing or signing without starting

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
described in [grants](grants.md), [the receipt contract](receipts.md) and
[shift reports](reports.md).

## Exit status

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
