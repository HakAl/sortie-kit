# Driver lifecycle and builder verification

`sortie start` without a mode flag runs a whole shift in the developer's
terminal: it prepares the shift, shows the scope and signs it there, admits the
grant once, starts the driver agent in the foreground of that terminal, and
serves the driver's and the builder's file channels and runs every review
itself until the shift ends. No pane tool is involved.

```sh
sortie start --repo PATH --direction FILE --for 4h \
  --agent driver=claude:MODEL --agent builder=codex:MODEL
```

The reviewer is the third signed seat. It is picked unless `--agent
reviewer=NAME[:MODEL]` names it; see [agent profiles](agents.md#the-reviewer-seat).

`launcher.admitted_start` is the seam between the live Admission and
`host.run_shift`: it runs the driver launch, and if that fails it publishes a
report whose closeout source is `admitted start failed` without retrying or
entering the loop. The Admission is constructed once; the nonce is not reused,
so a shift that ended cannot be started again. Sign a fresh one.

### Keeping the machine awake

From admission until its closeout and report are written, the host of a live
`sortie start` holds one hold against idle system sleep (`wake.py`). A running
shift is the whole reason; there is no other condition. `--no-wake` turns it
off, and is refused with `--prepare-only` or `--sign-only`, which run no
shift.

- macOS: a power assertion of type `PreventUserIdleSystemSleep` in the host's
  own process (IOKit, through `ctypes`; no child process). It is created with
  a timeout of 120 seconds and renewed from the host's loop every 30 seconds:
  the new assertion is created first and the old one released after. A host
  that dies or hangs renews nothing, and the system drops the assertion at
  its timeout. How old the assertion is, is counted on the system's
  `CLOCK_MONOTONIC`, which on macOS goes on while the machine sleeps (Python's
  `time.monotonic()` does not) and cannot be set; the wall clock gives only
  the times written into the entry. Each call into the system (create,
  release, and the question below) runs in a thread of its own and is waited
  on for 2 seconds at most. One that has not returned by then is a refusal
  (`did not return within 2s`) and is left to itself; until it returns no
  other call is made, each try in that time being a refusal at once (`an
  earlier call into the system has not returned`), and an assertion it makes
  after all is released on the spot. The host renews only on a pass of its
  loop that goes on: the pass that sees the shift is to stop renews nothing,
  and nothing is renewed while the seats are stopped and the report is
  written, which an assertion renewed within the last 30 seconds outlasts by
  far. A renewal is two such calls, so the host tells it how much working time
  is left, and with less than 4 seconds it is not begun: no renewal reaches
  past the moment the seats are stopped. So a stop waits on the hold only for
  the question below, half a second at most, and the release comes after the
  report is written. A report that a command writes at once, because it
  closed or blocked the shift, comes while both seats still run: it does not
  ask that question and has no `wake` entry. Only the host's own report at
  the end has it.
- Linux is not supported yet, and nothing below has been run on a Linux
  machine outside the tests. What the code does there: when `systemd-inhibit` is installed, the host runs
  `systemd-inhibit --what=idle --who=sortie-kit --why="sortie-kit: a shift
  is running" --mode=block /bin/sh -c 'printf 1; exec cat'`
  with the write end of a pipe as its input, and stops it when the shift
  ends. A host that dies closes the pipe, and the inhibitor ends with it. The
  program starting is not yet a hold: `systemd-inhibit` runs its command only
  once it has the lock, so the host counts the hold from the moment it reads
  that `1`, and records a refusal when it has not come within 10 seconds
  (no login manager to ask, or the request was denied or hangs). A `1` that
  the host first reads later than those 10 seconds is a hold from that moment
  and a refusal as well (`was seen to hold the lock only after 10s`), whether
  it came late or lay unread in the pipe: the start of the shift is not
  counted as held. The seconds are counted on `CLOCK_BOOTTIME`. The lock is
  one on the login manager's idle handling (`--what=idle`), not one against a
  suspend that is asked for. An inhibitor
  that ends by itself is a refusal with its exit status. Both are seen at the
  next pass of the host's loop and again when the closeout's entry is made.
  Without `systemd-inhibit` there is no hold, and the summary and the report
  say so. This path has tests with the program stood in for, which have also passed
  on a hosted Ubuntu runner; with the program itself it was not run on a
  Linux machine.
- Anywhere else there is no hold, said the same way.

It stops idle sleep only. A closed laptop lid, Sleep chosen by hand and a
battery that runs out still sleep the machine. The signed window, wind-down
and the stop follow the wall clock, so a sleep does not extend them: a shift
that slept through its deadline ends at once when the machine wakes. The time
limits of one builder turn and of one review are counted on the monotonic
clock, which stands still while the machine sleeps, so a turn or a review is
not cut short by a sleep; both are still ended when the working time is over.

A failure to get the hold never stops or blocks a shift. Every refusal is
kept with its time and return code. The closeout's `wake` entry holds
`asked`, `means`, `held_from`, `held`, `seen` (when the entry was made),
`refusals` and `gaps` (eight of each at most, with `refusals_count` and
`gaps_count` for all), and `whole`: the hold was taken at the first try, was
held when the closeout was written, nothing was refused, and no renewal came
later than the timeout after the one before. `held` is `true`, `false` or
`null`. On macOS the host does not take its own books for it: when the
entry is made it asks the system for the assertion's properties
(`IOPMAssertionCopyProperties`). The system keeps the properties of an
assertion that is no longer in force, so `held` is true only when they say
level on, not suspended and not timed out; one that is there and not in force
makes `held` false with a refusal (`the system has it, but not in force`).
When the system gives nothing, it has let the assertion go before its timeout
or it could not be reached, and the call does not say which: `held` is null,
not known, with a refusal (`the system gave nothing for it`). A question that
fails or does not return within half a second makes `held` null too
(`whether it is held could not be asked`). In each case the host keeps the
assertion's id and still releases it after the report, and makes a new one at
the next renewal. The host keeps that half second beside the two seconds it keeps
for writing the closeout and the report. `whole` is
true only when `held` is true. Such a late renewal is a gap:
the host was held up for that long, or the machine slept anyway; one lapse is
one gap however many renewals fail after it. On macOS a
release that the system answers with "no such assertion" before the
assertion's timeout is kept as a refusal too (`release: the system no longer
held it, before its timeout`): the machine was not held for some time before
that, and how long is not known. The hold is
not part of the signed grant: it gives no seat anything, and it is a fact
about the host process that runs the shift. The signer sees it in a line
under the summary, marked as not signed.

### The temporary directory of builder turns and reviews

A builder turn and a run of the reviewer get `TMPDIR`, `TMP` and `TEMP` set to
one directory that the host of a live `sortie start` makes for the shift
(`turn_temp.py`). It is outside the repository and short, because a project's
own tests may need both: a Unix socket's path is limited to 103 bytes on
macOS and 107 on Linux, so tests that bind sockets under the temporary
directory fail when it is long, and some tests must run outside any Git
checkout. The shift's `scratch/`, where these files went before, is inside the
repository and adds 53 bytes to the repository's own path. In the first shift
a developer signed in person, 76 of a project's tests failed for that alone,
on the untouched base as on the candidate.

- Where: the host tries `/tmp` by its real path (`/private/tmp` on macOS) and
  then the directories its environment names in `TMPDIR`, `TEMP` and `TMP`.
  It only reads those names and probes nothing: looking for a base writes no
  file anywhere, also not when the signer's line is made. A base is taken
  only when it is a
  directory your user can write, is not the repository or under it,
  neither it nor a directory above it holds a `.git` entry, no other
  user can rename or remove what you make in it (it is root's and sticky, as
  `/tmp` is), and no other user can turn the path to it elsewhere: every
  directory above it is root's and is writable by its owner alone or sticky.
  A directory of your own is not taken as a base, however its mode reads:
  where a directory can carry an access list, as on macOS, the list can let
  another user rename what is in it, the host does not read such lists, and
  only root can put one on what is root's. So the per-user temporary
  directory of macOS is passed over, and a name in your environment helps
  only when it points at a place like `/var/tmp`. In the first base
  that passes, the host makes `sortie-` and eight random characters,
  mode 700, through the base's open handle, and takes it only when the path
  it hands out names the directory it holds. Under `/tmp` the longest path is
  28 bytes on macOS (`/private/tmp/sortie-XXXXXXXX`) and 20 on Linux. Only
  when `/tmp` is refused is it another base plus 16 bytes, and such a path
  can be too long for a project's sockets; the
  closeout records the length and the report says it.
- When: the host's loop makes it as it starts, after admission, and the one
  directory serves every builder turn and every review of the shift. They run
  one at a time and share the repository and the shift anyway.
- No base passes: nothing is made, turns and reviews keep their temporary
  files in `scratch/` as before, and the report says so in bold with what
  that can break. The shift runs.
- At the end, after both seats' processes were stopped and before the look
  at the repository and the report, the host removes the directory with
  everything in it. It does that through the handles it has held since it
  made the directory, of the directory and of its base: it follows no link,
  goes into no directory on another file system and no deeper than 64
  directories, and removes the name only when it still is that directory. It
  is given one second at most and never the time kept for the report: the
  removal runs beside the host's own work, which waits for it no longer than
  that, so one call into a file system that does not answer cannot hold up
  the closeout and the report. A removal not seen to finish is told to stop
  and is recorded as left, with what it counted and took not known: the work
  asks before every change whether it was told to stop, so a call that comes
  back late is followed by no further change, though a change already under
  way when the host stopped waiting can still land. What cannot go in that
  time or cannot be removed is left, and a
  directory is left untouched when a builder turn, a review or the driver
  was not seen to stop: each runs as your user and can still write there. A
  host that dies removes nothing.
  The path is in `logs/host.log` from the moment it is made.
- A directory that was left is named in the report by the path the host made
  it at. On most of the ways it comes to be left the host did not look at
  what that path names at the end, so the report tells nobody to remove it
  and says to look first.
- A path that no longer names the directory the host made is recorded as
  that (`replaced`): when the name is gone or is something else by the time
  of the removal, or when the host finds after removing the name that its own
  directory still exists, so that an empty directory which had taken the name
  went in its place. The report then says so and tells nobody to remove what
  is at the path. No system call removes a directory by its identity, so the
  last case is found after the fact and not prevented. It is found by
  reading only: Linux counts no link to a removed directory, and macOS gives
  the path a held directory is at. On a system that offers neither, the
  record says `unverified: true` beside `removed`. The making and the
  removal are held against other users of the machine; a process of your own
  user, which the builder is, can do to the directory whatever you can.
- A directory that could not be opened after it was made, or was not what
  was asked for, is taken out again only when its name is seen to be what was
  opened and only when it is empty; otherwise it stays in the base and the reason names it.
- The closeout's `temp` entry is `{path, bytes, state}` (for `not made`,
  `path` is `null` and there is no `bytes`) with `state` one of
  `in use` (a report written while the shift ran), `removed`, `left` and `not
  made`; `removed` and `left` add `entries`, the number of entries that lay
  directly in the directory (`null` when they were not counted), and
  `removed_entries` (`null` when a removal was given up on and what it took
  is not known); `removed` adds `unverified: true` when the system gave no
  way to see afterwards that what went was the host's directory; `left` and `not made` add `why`, and `left` adds
  `replaced: true` when the path was seen not to name the directory. A removal that fails is
  said in the report and changes neither the exit status nor what the report
  says of the shutdown.
- Nothing of the shift points into it. Check checkouts and whatever a receipt
  names stay under `scratch/`, and builder.md tells the builder to keep
  evidence under the shift.
- It is not part of the signed grant, which holds no environment at all. The
  path does not exist when you sign, and its random name is what keeps
  another user of the machine from preparing it. The signer sees where it
  would be made as things are at signing in a line under the summary, marked
  as not signed; the place is looked at again when the shift starts.

The toolkit's own tests run no real agent CLI. Seen in three real shifts on
macOS, one with a `claude` builder and two with a `codex` builder (codex-cli
0.172.0): the directory was made with a path of 28 bytes, the builder's
checks ran in it, and the host removed it at the end with no `unverified`.
Under another configuration of yours this may differ. With a `codex` builder
and the network off, a test that binds a Unix socket there is still refused
by Codex's own sandbox; see
[the built-in seats](agents.md#what-the-built-in-seats-can-reach) and
`--builder-network`. A hook of
your own that keeps an agent inside the project is in
[your hooks](machine.md#your-hooks).

## The processes

The driver is interactive. The host starts the signed driver argv
(`Admission.seat_start`) as its own process group with the terminal as its
input and output, opened by the terminal's own device name (the name of the
command's own standard streams) and never as `/dev/tty`: macOS refuses that
alias in kqueue, which agent CLIs use to watch their terminal. The host hands
the driver the terminal foreground, and prints nothing while it runs; host notes go to `logs/host.log`.
Ctrl-C in the terminal reaches the driver, not the host. When the shift ends
for any reason the host signals the driver group and waits a bounded time for
it, tries to take the terminal back and restore its settings, and prints where
the report is. A full-screen program turns terminal modes on and turns them
off as it leaves; one that is stopped by a signal may leave them on, and the
shell then prints bytes for every movement of the mouse and every key. So
after the driver, on every way out, the host also writes to that terminal
what turns them off: mouse reporting (1000, 1002, 1003, 1006), focus
reporting (1004), bracketed paste (2004) and a hidden cursor. Two more things
a driver changes can be the caller's own as well, an entry of the kitty
keyboard protocol and the alternate screen (1049) with the cursor saved for
it, so the host writes nothing for them on a guess. It asks the terminal
before the driver starts and again after it whether the alternate screen is
on and which keyboard flags are in force, waits half a second at most for
each answer, and puts back only what differs: it leaves an alternate screen
that was not on before, and pops the keyboard stack, asking again after each
pop and eight times at most, until the flags are the ones from before. A
terminal that gave no answer before the driver, or said what kind of
terminal it is and nothing of what it holds, is not asked after it and
gets only the plain modes turned off; one that stops answering, or whose
answer does not come back to what it was, is reported as not restored. A key
typed during the half second of a question is read with the answer and
dropped, and so is what was typed to the driver and not read by it when it
ended. The host writes all of this to
the terminal's own device and never to standard output, so none of it reaches
a pipe or a file; a terminal that takes no output at that moment is reported
as not restored. These are attempts: a group that was not seen gone, or a
terminal that could not be restored, is said in the `Shutdown` and `Driver
process` lines and in the closeout, and the command exits with status 3. A stop signal to the
host (hangup, interrupt, terminate) is noted from before the driver starts
until the terminal is back. The host asks for it one last time when the
driver's arguments, directory and environment are ready, immediately before
it creates the process: a signal noted by then means no driver is started.
Asking and creating are two steps, so a signal that arrives after that last
check can still be followed by the start; the driver is then closed at once,
before the start-failed report is attempted. The same holds for each builder
turn. That report is best effort within the time left, and a start that is
refused prints the error and exits without the usual report line. A driver that exits ends
the shift with reason `driver exited`. A driver suspended with Ctrl-Z is treated
the same way, because the host cannot resume an interactive program for the
developer. An interactive driver that stops working without exiting cannot be
prompted by the toolkit. Its instructions tell it that the developer may not
be at that terminal and that it must never wait for an answer there; a driver
that waits all the same leaves the shift idle until the signed deadline.

The builder is headless and runs in turns. Each turn is one owned process
group started from the signed builder argv in the repository, with no terminal
input, its output captured to `logs/builder-turn-TASK-N.log` (first 4 MiB kept,
the rest counted as dropped), and a time limit. That log holds what the
builder's CLI printed, which for some CLIs, `agy` among them, is only its
final message; the same goes for a reviewer's logs. sortie-kit does not record what a seat ran. Its environment is the
developer's with `GIT_*`, SSH agent and askpass variables removed,
`GIT_TERMINAL_PROMPT=0`, and `TMPDIR`, `TMP` and `TEMP` set to
[the directory the host made for the shift](#the-temporary-directory-of-builder-turns-and-reviews),
short and outside the repository, or to the shift's `scratch/` when none could be made;
HOME is unchanged because the agent CLI needs its own sign-in. `PATH` is the
developer's, unchanged, and need not hold `sortie`: the prompt of every turn
carries a `shift:` line and a `toolkit:` line, the same command the driver is
given (the host's interpreter and the package's `entry.py`, both by full
path, so that another `sortie_kit` in the target repository or on
`PYTHONPATH` is not the one that runs), and
builder.md tells the builder to run exactly that for templates and `receipt
write`. The toolkit's directory is not put on the
builder's `PATH`, because that would also decide which `python3` and `pip`
the builder's own checks of the target repository find. A turn has no
memory of earlier turns. When a turn's process exits, runs out of time, or the
shift ends, the host stops the whole group and records on the task
`{turn, started, limit_seconds, log, ended, outcome, exit, reaped,
output_dropped_bytes, elapsed_seconds, receipts_changed, request_id}`.
`request_id` is the phase the turn ran in (a row written by an earlier host
has none); `continue` reads it to tell whether the last turn of the open
phase handed something in. `limit_seconds` is
the time the turn was allowed. `elapsed_seconds` runs from just before the
host started the process until the host saw it over or began stopping it,
in whole seconds; it is what the host observed at its polling interval, not
the process's own accounting. `receipts_changed` compares two listings of
`receipts/` (names, inode, size and modification time; names starting with a
dot left out), one taken before the start and one when the end is recorded:
true when they differ, false when they are equal, null when either could not
be taken within its bound (1024 entries, a quarter second) or the host did
not run the turn to its end; the ending listing is skipped when less than
half a second would remain for writing the turn's end. A difference can be a removal or another
process's write and is never a valid receipt by itself; equal listings make
a written receipt unlikely, not impossible. An ended row of a shift from
before this field has `seconds` in place of `limit_seconds`.
`outcome` is `exit`, `signal`, `timeout`,
`stopped: REASON` (the shift ended), `not started: REASON`, or `lost` (a turn
recorded by a host process that no longer exists; whether its process still
runs is unknown). None of these is acknowledgment, progress or acceptance.

The host creates each turn log itself and writes through that one open file;
whatever a builder later puts at the log's path is never opened. It reads a
bounded amount of builder output per pass, and signals the group before the
last drain, so output cannot hold up a deadline or a stop.

The host lets go of a turn only when it saw the group stop. While a stop is
not confirmed the turn stays recorded as running and `brief` and `continue`
answer `blocked`; at the end of the shift such a turn is recorded with
`; stop not confirmed` after its outcome and `reaped` false. A `lost` turn
blocks the lifecycle with next owner `developer`: the host signals nothing it
does not own and starts no further builder, because the earlier process may
still run. Look for it by hand, then sign a fresh shift.

The reviewer is headless too and is started by the host alone; see
"The review the host runs" below.

Every seat runs under the developer's own configuration of its agent CLI.
The grant pins which program, model and arguments start; it makes no statement
about what that program may then do, and neither does the report. The built-in
`codex` builder's arguments ask Codex for its workspace-write sandbox with the
repository's `.git` writable, and for the network only when the start was
given `--builder-network`; see
[agent profiles](agents.md#what-the-built-in-seats-can-reach). A headless
builder that needs a permission it does not have stops short; the toolkit
records the turn, says when `receipts/` looked unchanged after it, and does not work around it. Programs that deliberately leave
their process group are not contained.

## Waiting

`sortie wait --shift SHIFT [--for N]` is how the driver blocks instead of
idling. It reads the shift's files and nothing else: no signature check, no
admission, no write. It returns one JSON line when the outstanding
`driver-command.json` has a matching response and neither a builder turn nor a
review is running (`ready`), when the shift is `closed`, `stopped` or `expired`, when
`driver-state.json` cannot establish that (`damaged`), or with `waiting` after
N seconds. A response matches only when it carries the digest of the exact
command bytes and, where the command has an id, that id. `damaged` means the
state file is unreadable, malformed, lacks a field readiness rests on, is
bound to another grant, or is missing although a response exists: the host
writes its initial state before it answers anything, so only a shift whose
host has not begun has neither. It comes with `mode` `unknown` and a `detail`;
it is never `ready`, because whether a builder turn runs cannot be told.
The host replaces its files while seats run, and the driver replaces its
command file. A read that such a replacement cut across is made again within
the same reading, up to twenty times a hundredth of a second apart, and is not
reported as damage; only a file that changed under every one of them, or that
is unreadable or malformed, is. The host reads the command file and a
builder's check request in the same way and leaves one that changed under
every read of a pass to its next pass. A receipt, and a file a receipt names,
is written once and is still refused when it changes while it is read. One
`damaged` reading is one reading: the driver is told to read again, up to
three more times, before it concludes anything, and `sortie wait` writes
nothing, so a `damaged` reading that the next one clears leaves no trace in
the shift.
`ready` is a statement about these files as read, not about processes.
`expired` outranks `stopped`, which outranks `damaged`. N is at most 540
seconds. The first reading of the files always happens and has between 50 ms
and five seconds, also when N is smaller; the grant's `start` and `until` are
known only from it and are read only to bound this command's own sleep and to
work out the times it reports. A later reading starts only while at least
50 ms of both N and the time until the host stops all work are left, and is
given no more than that. The time limit is checked between file
operations, so one that is slow can let the command return somewhat later
than N.
The line carries `answered`, `response` (`id`, `status`, `detail`, `state`),
`stage`, `turn` (the running turn or null), `last_turn`, `review` (the running
review as `{task_id, stage, round, attempt, started, limit_seconds}`, or
null), `last_review` (how the last review ended: `task_id`, `stage`, `round`,
`outcome`, `receipt`, `findings`, `detail` and, for a review that failed with
words of the reviewer's CLI on hand, `said`; kept also for a task the review
blocked), `mode`,
`seconds_left`, `wind_down` and `seconds_before_wind_down`. `seconds_left` is
the time until the host stops the builder turn and the driver, which is
shortly before the signed deadline (see "Wind-down" below), not the time to
the deadline itself; `expired` is reported from that moment.
`seconds_before_wind_down` is the time in which new work can still start, and
`wind_down` is true once it is zero. `seconds_left` is rounded down and
`seconds_before_wind_down` up, so the latter is zero only in wind-down.
`expired` is said from 50 ms before the stop, when no further reading could be
made. These times are computed from the grant's window by the rule the host
uses. They do not see a host whose own budgets end earlier than the signed
window or that a caller started with another wind-down, so the host's own
answer to a command is what counts. A wait that was called before wind-down
returns once wind-down has begun, whatever its status, so a driver blocked on
a running turn learns of it then; it notices at its next reading, which can be
up to its polling interval and one reading late. When no turn runs and the task's last turn has
`receipts_changed` false, it also carries a `note` saying so in plain words,
with the turn log to read. It carries a `note` too, whatever `receipts_changed`
says, when the builder's CLI ended that turn by itself with a status other
than 0 or by a signal: see "What a CLI wrote last" below. Exit status is 0 for any such line and 3 with
`{"status": "refused"}` when the directory is not a signed shift.

The host services driver decisions. In a live shift it runs no command on the
builder's request (see "Builder check exchange").
The driver LLM selects work and writes coordination commands. The driver component
never imports or calls the execution helper, reviews code, or edits target source.
The builder retains tiger's build loop and owns its reproductions. It runs no
review: the host starts the signed reviewer and writes the review receipt.
`sortie instructions driver|builder` prints the packaged instructions; preparation
copies `driver.md`, `builder.md` and verbatim `tiger.md` into the shift. The latter
is the tiger working agreement, unchanged.

## Driver file protocol

Publish complete version-1 JSON atomically to `driver-command.json` under the
shift, then wait for `driver-response.json` with the same `id` and SHA256 `digest`
of the request bytes. One slot is outstanding at a time. Responses contain
`version`, `id`, `digest`, `status`, `detail`, `state` and `evidence`. Status is a
coordination result (`advanced`, `recorded`, `blocked`, `invalid`, `replayed`, or
`closed`), never a substitute for a satisfying receipt. Missing command files
are idle. Partial/malformed files receive invalid responses without an action.
Unobserved overwrites are not claimed processed. Exact ID/content replay repeats
no effects; changed same-ID content refuses. An invalid response can have null ID.

Every command has `version: 1`, a unique bounded `id`, and `op`. The fields below
are required except where marked optional. Artifacts use the receipt contract's
shift-relative `{path, sha256}` form. All task commands other than `brief` also
name `task_id` and the current phase `request_id` from `current-context.json`.

| op | Additional fields | Result |
| --- | --- | --- |
| `brief` | `task_id`, `kind` (`feature` or `defect`), `brief`, `next_owner`; optional `turn_seconds` | Select one new task, publish context, start builder turn 1 with its brief path/digest. |
| `consume` | `receipt`; `target` only when proposing a plan/build target | Mechanically consume one bound phase receipt of the builder's. A plan or build receipt that is taken moves the task to its review, and the host starts that review in the same step. At a review stage the host takes its own review receipt: the command is `blocked` while a review is open and `invalid` when none is. |
| `repair` | `reason`, `next_owner` | After a return, increment the task's shared repair count or block at three. |
| `progress` | `evidence` | Record a new artifact; never acceptance. |
| `continue` | optional `turn_seconds`, optional `again` (only `true`) | At a review stage: start the review that was held back, or run on one that was begun and has no run in progress. Otherwise: start the task's next builder turn with a fixed host message. Blocked while a turn runs, in wind-down, when no builder phase is open, and after 32 turns. Also blocked, with no turn started, when the last turn of the same phase changed `receipts/` and nothing was consumed since: the builder would have nothing to do. `"again": true` starts the turn all the same, for a turn that wrote something other than the phase's receipt. In wind-down `brief` and `repair` are blocked too. |
| `close` | none | Close only a locally accepted candidate. |
| `set-aside` | `reason`, `next_owner` | Leave the open task so that the next can be briefed, at any stage but `accepted` and in any repair round, with no need to use the rounds up first. The task's stage becomes `blocked` with the reason "set aside by the driver at STAGE, repair round N:" and the driver's words, and with the owner given. Answered `recorded`. See "Leaving a task" below. |
| `stop-evidence` | `receipt` | Record builder-seat cessation separately. |
| `finish` | `reason`, `next_owner`, optional `not_started`; no task/request required | Close the local handoff. `not_started` is the driver's list of what the direction held and it never briefed: at most 32 items, each `{"name": text up to 200, "reason": text up to 500}` and nothing else; `[]` says the direction held no more. A list of another shape is answered `invalid` and nothing changes. The host keeps it as the driver's statement and checks only its shape. |
| `note` | `kind` (`decision` or `question`), `text` up to 1000 characters, optional `task_id` of a task briefed in this shift; no request | Keep the driver's own statement for the report and change nothing else: what it calls a decision it made on its own, or a question it could not settle before it went on. Answered `recorded`. Served whenever commands are, also while a builder turn or a review runs and in wind-down. At most 32 are kept; a further one is answered `blocked`. The host checks the shape and nothing of what the text says, also not whether a decision was within the direction. A note has no answer: the command gives back only that it was kept. |

`turn_seconds` is a whole number from 1 to 7200 (default 1800) and is clipped
to the time left until the host stops all work, so a turn started before
wind-down may run on through it; any other value is `invalid` and changes
nothing. `consume`, `repair`, `close` and `set-aside` are `blocked` while a
turn or a review runs. A
turn that could not be started is `blocked`, recorded on the task, and leaves
the lifecycle open. `repair` starts no turn; follow it with `continue`.

A command refused for what it says is answered `invalid` and changes nothing
but the record of that command: a `task_id` or `request_id` that is not the
active phase, a `consume` whose `target` has the wrong shape, whose `receipt`
is not a safe relative path or names no file, or that tries to rebind a review
target, a second `brief` while a task is open or for a task ID already used,
an unknown `kind`, blank or oversized text, a `repair` when no receipt was
returned, a `close` before acceptance, a `progress` artifact that is missing
or does not match its digest. The task keeps its stage and repair round, the
lifecycle stays open, and no builder turn is needed: the driver corrects the
command and sends it under a new ID. Such a command used to block the whole
lifecycle with `effect incomplete`. That ending is now kept for a failure
after the command began to take effect, where what was done is uncertain:
storage that failed, or authority that ended, once `consume` had published its
expected context or the ledger may hold the receipt. A `consume` also needs
the target its phase takes (`plan` for a plan receipt, `commit` for a build
receipt, none otherwise) and a `receipt` path of the form
`receipts/<receipt_id>.json`, directly under `receipts/`, that opens as a
regular file of the shift through no link; anything else, such as a log or a
coordination file named by mistake, is `invalid` before anything is
written. A receipt that is read and does not satisfy its phase is not the
driver's mistake: it is answered `blocked`, the stage becomes `repair_needed`,
and a repair round is the way on. A failure inside receipt validation is
treated as a returned receipt only when the ledger can be read afterwards and
has gained no entry. When the ledger gained one, or cannot be read, the
failure came after or around the ledger write and is an incomplete effect:
the lifecycle is `blocked` for the developer and the task keeps its stage.
The same holds when the check succeeds but recorded other bytes than the host
had read, because the receipt file was replaced in between: the phase slot is
used up by a version the host did not see. A
validation that the running host could not finish because the working time
ran out, with nothing in the ledger, is `invalid` and changes nothing. One
that ran out of the time the host gives the check while working time is left,
again with nothing in the ledger, is `blocked` and changes nothing: the
receipt is not at fault, no repair round is used, and the driver sends the
same `consume` again under a new ID. Any
other failure inside validation, whatever its cause, still counts as a
returned receipt. When a write fails once a command has begun (the note of its
intent, or its result), the answer is `blocked` and says the outcome could not
be recorded. Which of the writes reached the disk is then not known: a file
is replaced before its directory is synced, so the command may have taken
effect in part or in whole, and its intent may or may not be on file. The
driver sends nothing again and the developer has to look. `driver.md` shows one exact
example of every command.

### Leaving a task

One task is open at a time, and a second `brief` is refused while it is. A
task stops being the open one in four ways. `close` takes an accepted task.
A `repair` sent after three repair rounds were used blocks the task. A review
that failed twice, or whose receipt the host could not write, blocks it by the
host's own doing. And `set-aside` leaves it on the driver's word.

Until `set-aside` there was no way out before the repair rounds were used up,
and none at all for a task at `ack`, `plan` or `build` whose builder could not
hand in a receipt, since `repair` needs a receipt or a review that came back.
A shift whose first task met a wall outside the task ended there with every
other task unstarted.

`set-aside` names the open task and its current `request_id` and gives a
reason and a next owner. The task is recorded with the stage `blocked`, the
stage and repair round it was left at and the driver's reason; it is not
accepted, and the report has it under "Not done" with that reason and that
owner. Its identity stays taken, so that `task_id` cannot be briefed again
in the shift. The next `brief` is free, and it may be the same work under a
new identity: that is how a driver puts right a brief it sent with a line of
the direction missing, since no command changes a brief and none sends an
accepted plan back. Nothing of the task that was left is carried over. Its
acknowledgment, its plan and the review that accepted the plan were of the
old brief, so the builder plans again and the host reviews that plan; the
report has the task that was left under "Not done" and counts one task more
than the direction held; and the host links the two tasks nowhere, so only
the driver's reason and the new brief say that one took the other's place.
A defect has no plan, so there only the acknowledgment is made again.
Nothing the builder committed for the task is
undone: what is in the repository stays there, and the review of the next
task is of the change from where the repository is at its `brief`.

The command stops no seat. While a builder turn of the task runs, or a review
of it is running or was begun and is not finished, it is answered `blocked`
and changes nothing, as `consume`, `repair` and `close` are; the driver waits
for the end, or sends `continue` to run a begun review on. An accepted task
is answered `invalid`: it is closed, not set aside. In wind-down the command
is served, since it starts nothing, and its answer says that no brief is
taken any more. A task left at a review stage, where the host had started or
finished no review of what was handed in, has "with the work handed in and
not reviewed" in its reason, which is where the report then has it.

`driver.md` tells a driver when to use it. A task that a repair can put
right is repaired. A task is set aside when it cannot be put right in the
shift, and the next is briefed only when it can be done from the repository
as it then is. A task whose brief the driver itself got wrong, with a line
of the direction left out or misstated, is set aside before wind-down and
briefed again under a new identity with a whole corrected brief; `driver.md`
says that this is for a wrong brief only, and no way to get repair rounds
back or another review of work that fell short. A candidate the host accepted and the driver holds to break a
line of the direction is neither closed nor set aside: the driver finishes
with the developer as next owner.

Limits. The host does not judge the reason. It cannot tell work briefed
again for a wrong brief from work briefed again to start over: a new
identity begins at repair round 0 with no earlier finding to answer, and
only the words of `driver.md` stand against that use; the report shows both
tasks, each with its reviews. The host puts nothing back: a
commit the builder made for the task is in the history the next task builds
on and is outside that task's review, and what the builder left uncommitted
is still in the working tree when the next builder starts. That is so for
every task that ends not accepted, and the report's part on the repository
shows what was left at the end. A builder's stop receipt cannot
be recorded with `stop-evidence` for a task that was set aside, as for any
task that is no longer the open one. A turn that runs has to end first, which
can take as long as the `turn_seconds` it was started with.

### The time a command has

A command is applied under the shift's lock, and the host's loop takes no
other step meanwhile. From the moment the host asks for the lock a command has
at most 15 seconds, and never more than the working time left. Until this was
changed it had five, for everything from the wait for the lock to the last
write, and a shift on a loaded machine ended on it: a `continue` started its
builder turn, the last write found the five seconds over, and the next command
blocked the shift on the intent that was left on file. Measured on one Mac
with the fixture shift of the tests, a command took under half a second on a
machine at rest, but for one `close` at 1.6 seconds, and up to 3.4 seconds beside 28 computing and 4 writing
processes; a `continue` took up to 2.9 seconds beside 14 and 24. Fifteen is
three times what the real shift exceeded. It is not more because, while a
command is applied, the host reads no output of a running builder or review
and sees no exit and no stop.

What follows from running out of that time depends on where it happens:

- **Before the command began.** The wait for the lock, the reading of the
  state or the writing of an earlier turn's or review's end used it up. The
  answer is `blocked` and says that no time was left to begin. Nothing of the command was done and no intent stays on file. The end
  of an earlier turn or review may have been recorded in the same pass, so the
  driver reads `current-context.json` again and sends the command under a new
  ID.
- **Before a builder turn, a review or a new task was on file.** The turn is
  recorded as not started and the answer is `blocked`, as for any turn that
  could not be started. A review attempt that was not begun is not an attempt:
  the stage stays, and `continue` starts the review. When it was the second
  attempt, which the host runs by itself after a first that failed, the review
  is kept as failed once with no receipt, `last_review` says so, the work stays
  handed in, and `continue` starts another review. A `brief` whose task
  could not be recorded leaves no task, and the same brief can be sent again.
  The lifecycle stays open in each case.
- **After the command took effect.** The record of the outcome has a time of
  its own, counted from when the command was applied and not from when it was
  taken: the finalization time left, five seconds at most. Through the running
  host it is the working time left when that is less, and one second when less
  than a second of it or none is left, so the stop is held up by a second at
  most. The end of a builder
  turn or of a review is written on such a time of its own as well.
- **Anywhere else inside a command**, once it began to take effect and where
  the command does not handle it itself, the time running out is still an
  incomplete effect: the lifecycle is `blocked` for the developer. What was
  done is not known there.

A command whose time is over starts no process. The time is asked once more
at the start of a builder or a reviewer, after the start's own checks: a turn
is then recorded as not started and the review attempt as one that could not
start, as above. The check of a builder's receipt is given no more than the
command still has, and is not begun when the look at the ledger before it left
none: the answer is then `blocked` with nothing changed, as for a check that
ran out of its own time.

A save writes `current-context.json` first and `driver-state.json` last. The
state file is what counts: a save that fails before it leaves the state as it
was, with the intent still in it. The state file is replaced before its
directory is synced. When that sync fails, the file that can be read may show
the outcome with no intent in it while the driver was told that it could not
be recorded. The stop does not rest on the file then: the host that gave that
answer remembers it and blocks the lifecycle at the next command, as it does
for an intent on file, also when that command carries the id the file shows
as taken. When the command was `finish` and the file shows the lifecycle
closed, the host ends on that pass and does not keep the close as the
driver's: the developer is the next owner and the reason names the command
whose outcome could not be recorded. The exit status is what a lifecycle
stopped on an intent gives. A host started later has only the file.

When the record still cannot be written, on its own time or because storage
failed, nothing is tried again and nothing is replayed: the answer is
`blocked` and says the outcome could not be recorded, the intent stays on
file, the next command blocks the lifecycle with `unresolved effect intent`,
and the developer is the next owner. That is the stop it always was.

Limits. Every time named here is asked between one step and the next and
interrupts none: a write or a sync that the system holds up is waited for,
past the end of the command's time, of the record's second and of the working
or finalization time, and the host stops no seat and writes no report until
it returns. The host has one thread for this. `sortie report` waits at most five seconds for the same lock, so one
run beside a command that takes longer ends with `duration elapsed` and can be
run again. A review that ended within its limit while a slow command was
applied, and that the host looked at only after the limit, is recorded as
late, which is a failed attempt. A machine on which a command takes more than
15 seconds still loses the command, in one of the ways above. None of the
figures was measured on Linux.

A `target` is `{kind: plan, artifact: ...}` for plan consumption or
`{kind: commit, commit: FULL_OID}` for build. Later reviews cannot rebind those
targets. The reviewer and any same-family permission come only from the signed
grant; no driver command can change them.

The gates are acknowledgment, plan and plan review for a feature, build, candidate
review, acceptance, close. Defects use the builder's reproduce/root-cause/TDD
route before build/review. Only `validate_receipt(record=True)` returning
`satisfied`, `satisfies_phase: true`, and `recorded: true` advances. Diagnostic
checks never advance. Build evidence alone is not final acceptance. The driver
writes expected context from active state and the signed `review_policy`, not from
the received receipt. Plan/build target is unset in the initial context until
the driver names the proposal. Builder reads the current context at each gate.

Each gate has a fresh phase request ID; plan review and candidate review cannot
collide in the receipt ledger. Turn numbers are separate from both. Task round starts
at 0; all repairs share rounds 1–3, including internal builder returns and receipt
repairs. A review that failed (see below) is not a return and uses no round. A check
request/follow-up assessment on an unchanged candidate is progress within the
same round. Required plan and candidate finding IDs are separate. Rebriefing a
stored task ID refuses even after closure; prompts also prohibit renaming the
same logical task to evade the cap. The toolkit cannot infer semantic task identity
or hidden, unreported builder work.

`driver-state.json` records these distinctions, a grant/direction binding, task
history and command results, each command with the host's time when it was
recorded (`at`; a state kept by a host from before that has none and still
loads). It permits at most 128 tasks and 512 commands; state
and input documents are limited to 1 MiB. Each metadata operation/lock wait is
bounded. Intent is persisted before a builder start or receipt consumption. An unresolved
intent after interruption is a blocker with next owner, never a guessed replay
or duplicate-as-acceptance. This deliberately fails closed across multi-file
write failures; it does not claim transactional crash recovery.

## The review the host runs

`plan_review` and `review` are host work. When `consume` takes a plan or a
build receipt, the host begins the review of that plan or candidate itself:

- It makes a directory for this run under the shift's `reviews/`: an export of
  the files at the commit under review (`git archive`; for a plan review, the
  commit the repository is at then), `REVIEW.md` with the instructions and a marker
  value made for this run, the brief, the plan, `change.patch` with the task's
  whole change from the commit the repository was at when the task was briefed
  to the candidate, and in a repair round `answers.json` with the earlier
  findings in full and the builder's answer to each.
- In that directory it puts `evidence/`, what it has taken from the builder
  for the task so far: under `evidence/receipts/` each acknowledgment, plan
  and build receipt it consumed that satisfied its phase, shown while the file still has the bytes
  the host took; under `evidence/files/` the files those receipts name,
  from the host's own copies of the bytes it checked at that time, so a log
  the builder has since rewritten is shown as it was; and `evidence/HOST.md`,
  the host's statement: which receipts it took, of which phase and round,
  when and with which SHA-256, the commit the task began at and, for a plan, the commit the
  repository is at now or, for a candidate, the candidate, what it checked of them (shape, the request they were
  written for, the bytes of each named file) and that it did not judge what
  they say. For the review of a candidate `HOST.md` also states what the
  host itself saw of the repository when it prepared the review, from a
  read-only look of its own and from no receipt:
  - the branch `HEAD` is on, and whether `HEAD` is at the candidate;
  - the commits from the commit the task began at to the candidate: whether
    the first is an ancestor of the second, how many there are (counted to
    1000) and the ids of the 20 newest, with those that are merges marked;
  - the remotes of the repository's own configuration, at most 20 (every
    section `remote "NAME"` that sets anything, also one with a fetch rule
    and no URL, and one with an empty name under the name Git gives it,
    `.url`), each with
    its name and, of its URLs, the scheme and the host only (for `http`,
    `https`, `ssh` and `git` URLs and the form `host:path`; any other URL,
    a path on the machine among them, is "not shown"), and against the look
    at the shift's start whether it was added since, was there, or has
    another URL, and which were removed;
  - whether any ref exists under `refs/remotes/` and which of them, at most
    20, the candidate is reachable from;
  - the files that are untracked and not ignored, at most 50 names of at
    most 1000 counted, with those the start look already listed marked, and
    how many paths are staged and not committed.

  A name is given between backticks and only when it is made of letters,
  digits, `.`, `_` and `-` (a path: each part) and has not the beginning of
  a known kind of credential; others are counted, since a name is the
  builder's to choose, stands in the host's own text and goes to the
  reviewer's provider. Of one remote at most four URLs of each key are
  given and the rest counted, and where more than 20 remotes are named,
  those not listed are still compared with the start and counted as added
  or changed. The start look keeps a name cut at a length: of a longer name
  that begins as one kept there the host says that it cannot tell. The
  look takes at most three seconds and at most a quarter of the time
  evidence has, before the receipts are read, and asks before each Git
  command what is left as it does before each piece of evidence. It is
  held to the Git directory the start look found, runs no hook, monitor,
  pager or filter of the repository and follows no replaced object. For
  the remotes it reads neither the user's nor the machine's configuration
  nor a file the repository's configuration includes; its other commands
  read configuration as Git does, so the user's own exclude file can hide
  an untracked name. A command that the time ended ends the look: no
  further one is started. A fact it could not read has a line
  that says so and why, and that is no finding. The section ends with what
  it does not show: nothing from before the shift; not that nothing was
  pushed (a push to a URL typed by hand touches nothing here, and the host
  asks no remote); remotes and untracked files as they are at the look and
  not in between; nothing that ignore rules hide; not edits that are not
  staged; not how files were staged; and that it is a look at a `.git`
  the builder can write, not a record. The instructions tell the reviewer
  to answer a line on how the work is delivered from that section, to
  answer "do not push" and "staged by name" `not_checked` with what the
  host saw under `where`, unless an added remote or a remote-tracking ref
  that holds the candidate speaks for a push, and that `met` is not the
  answer to those two. A plan review gets none of this. The names of
  remotes, of remote-tracking refs and of untracked files, and the hosts of
  remote URLs, go to the reviewer's CLI with the rest: they are not in the
  exported files, so this is more than a reviewer of another family was
  sent before.
  At most 64 files are shown, none over 1 MiB, 4 MiB together; a
  file whose name is not made of plain parts, or that the host holds no
  copy of, is not shown. Whatever is not shown has a line in `HOST.md` with
  the reason, and none of it fails the review. The instructions tell the
  reviewer, and `HOST.md` says too, that the brief is the whole of what is
  asked of the task and that `direction.md` and `builder.md`, the
  instructions the driver and the builder work from, are not shown, so that
  their absence is no limit to report. They also tell the
  reviewer that proof the directory cannot hold (a file `HOST.md` says is
  not shown, a run the reviewer cannot make) is a limit to state under
  `not_examined`, and that its absence alone is no new finding. An earlier
  finding that rested only on such absence is resolved, with the limit
  named in its reason; one that named a fault stays unresolved until the
  correction can be checked. What the brief requires and the plan, the
  change or a shown receipt leaves out is a finding as before. The
  entry of a receipt taken by `consume` or `stop-evidence` on the task in
  `driver-state.json` has `at`, the time the host took it; the entries of
  the host's own review receipts, and entries written before this, have
  none. Evidence is written after the export of the files, in at most
  half of the time then left for preparing the review and never past the
  review's own time, whose end is fixed when the review is begun, nor past
  the working time of the shift, each less the 7.25 seconds the host
  allows the work between the directory and the start (the look at the
  repository, the listing of `receipts/`, the reviewer's start, the
  writes); what that leaves is named as not shown. The host asks what is
  left of both again once the files are exported and before each piece of
  evidence, since the signed deadline is a time of day and comes closer
  while a machine sleeps. When no such time is over, the review starts
  without `evidence/`: so it is for a review with less than eight seconds
  of its own time, which only one begun in the last seconds of the working
  time has, and for one prepared on a machine that slept until no more
  than those 7.25 seconds were left of the working time. Every receipt is
  shown before any file, and then
  one file of each receipt in turn from the latest receipt back, so a
  receipt that names many files takes the place of no other. If
  `evidence/` cannot be made at all, the review runs without it.
  `REVIEW.md` is written after the evidence, from what became of it. A
  review that was shown none reads there, in place of the lines on
  `evidence/`, that the host did not show the builder's receipts (nor,
  for a candidate, its own statement of the repository) and why,
  that this is the host's doing and not the builder's and no finding, that
  it is a limit to state under `not_examined` and, in a review of a
  candidate, that a requirement resting on a receipt, a log or the state of
  the repository is answered
  `not_checked`. Each attempt in the review receipt says under `evidence`
  which it was: `shown`; `no_time`, the host gave none to show because the
  time above left no room; `late`, preparing the directory used the time
  up before evidence began; `failed`, `evidence/` or the `HOST.md` in it
  could not be written, and receipts or files may lie there without the
  host's statement. For an attempt that was not shown them the host writes
  one line to `logs/host.log`, and the report says it under the review
  (see [reports](reports.md)). What is
  in `evidence/` is given to the reviewer's CLI: with a reviewer of another
  family the receipts and the files they name go to another provider, as
  the files of the repository already do. A log is not in the repository,
  so a builder should not put into a receipt what the reviewer may not see.
- It notes the repository's commit and status and a listing of `receipts/`,
  and starts the signed reviewer argv (`Admission.seat_start("reviewer", ...)`)
  in that directory as an owned process group, with no terminal input, the
  builder's environment rules, and its standard output and error output kept
  apart in `logs/review-TASK-N-ATTEMPT.out` and `.err` (4 MiB each). The
  prompt is a few lines that point at `REVIEW.md`.
- One run may take the signed review limit (`--review-limit`, ten minutes by
  default) and never more than the working time left. The host supervises it
  in its loop as it does a builder turn.

This is not a builder turn. It has its own record: `review` in
`driver-state.json` while a review is open (`task_id`, `review`, `request_id`,
`stage`, `round`, `target`, `prior`, `attempts`, `running`), `last_review`
for how the last one ended, and `reviews` on the task. The turn count and the
`continue` rules for turns are untouched, and a review costs no builder turn.

When the run ends the host applies [the review contract](review.md) to the
bytes it took from the reviewer's pipe itself, which it keeps in memory for
the run. The two logs are files in the shift, which another seat could write
to: the host reads them back through the files it opened and requires them to
be, byte for byte, what it took from the pipes. A verdict is taken only from
a reviewer that ended on its own with status 0, that the host saw ended
before its limit had passed (one first seen over after the limit is not known
to have ended within it), whose whole output was kept and is what the logs
hold, whose closing object keeps the contract for this run's marker and
target, and after which the repository's commit and status and the listing
of `receipts/` are what they were before. That last comparison is of the
commit and of Git's status lines: a file that was already modified before the
review and is changed again shows the same line, and is not seen. Anything
else is a failed attempt with its reasons. The host then writes the review receipt itself (role
`reviewer`; see [receipts](receipts.md)), has it checked and recorded like
any receipt, and moves the task:

- `ACCEPT`: a plan review moves the task to `build`; a candidate review makes
  it `accepted`.
- `CHANGES`: the task becomes `repair_needed`. The findings the builder has to
  answer (each earlier finding the reviewer calls unresolved, as first written
  and with the reviewer's reason, and each new one; the worst first, at most
  64) are written to `briefs/findings-REQUEST.json`, and `current-context.json`
  names that file once `repair` has opened the next plan or build phase. The
  driver sends `repair` and `continue`; the
  builder's next plan or build receipt answers each finding, and the next
  review is given those answers and says for each earlier finding whether it
  is resolved. An `ACCEPT` that leaves an earlier blocker or major finding
  unresolved is a failed attempt, not an accept.
  When a review that ends in `CHANGES` answers an earlier finding as not
  resolved and the review before it at that stage did so too, the task's
  reason and `last_review.detail` say so after the list of findings:
  `Answered as not resolved by reviews running: F1 (3 reviews)`, each such
  finding with the number of reviews that answered it so one after another,
  this one included. The answer then no longer ends in "send repair": it
  points to what `driver.md` says under "A finding the reviewer repeats".
  These are words only. No field of the state, no receipt and nothing
  signed changed for them: the host counts from what it already keeps, the
  list each earlier review of the task handed on, since the next review is
  asked about exactly that list and an answer whose new finding takes an id
  it was asked about is refused. A review that failed or was lost answered
  nothing and neither adds to the number nor ends it. The host does not
  judge who is right between the builder and the reviewer, a repair round is
  used for each such review as for any other, and no command lets a driver
  contest a finding or call it resolved: a driver that could strike a
  finding would be a way round the review. The host says nothing of the
  kind after an `ACCEPT`, which may leave a minor finding unresolved and
  asks for no repair; the report counts that answer (see
  [reports](reports.md), `repeated_findings`).
- A candidate review answers the brief requirement by requirement (see
  [the review contract](review.md)). An `ACCEPT` beside a requirement
  answered `not_met` is a failed attempt, not an accept, and so is an answer
  without the list. An `ACCEPT` beside one answered `not_checked` is taken,
  and the report lists and counts it; so is one beside a requirement
  answered `disclosed`, which the brief, as the reviewer read it, lets be
  closed by saying that it is not delivered. Each finding names the
  requirement it says is not delivered, or none, and an answer whose finding
  names a requirement marked `met` or `not_checked` is a failed attempt
  whatever its verdict. What goes to the builder of a finding is its text,
  without that name. A plan review is asked for no list.
- A failed attempt is never an accept and never `CHANGES`, and uses no repair
  round. The host makes one more attempt at once when the shift is active, a
  review may still start and enough of its lock is left to prepare one. The
  second attempt is given the same instructions and not the reason the first
  was refused. Two attempts at most. Both are kept in the one receipt of that review, so a
  failed first attempt does not weaken a later accept. After a second failure
  the task is `blocked` for the developer with `review failed twice`, and the
  shift goes on: the driver can brief the next task. When the first attempt
  failed and no second was made (the host is stopping, the limit no longer
  fits, or too little of the host's lock is left to prepare one), that review
  is closed with a receipt of one failed attempt, the work stays handed in
  and not reviewed, and the task gets a new `request_id`. A `continue` then
  starts a new review, with a receipt and two attempts of its own, while a
  review may still start.
- A reviewer that cannot be started (the pin changed, the program is gone) is
  a failed attempt with that reason.
  When it could not be started and the time under the host's lock ran out
  before a receipt for the review could be recorded, the review is kept as
  failed with no receipt and backs nothing; `last_review` and the report show
  it, and the task moves as after any failed review: blocked after two
  attempts, and otherwise handed in still, for a `continue` to start another.

While a review is open the driver cannot hand in a review receipt, and
`consume`, `repair` and `close` are answered `blocked`. `sortie wait` waits
for a running review as for a turn.

A review recorded as running by an earlier host process is lost: nothing is
known of its end and nothing is taken from it. The lifecycle is `blocked`
with next owner `developer`, as for a lost builder turn.

### What a CLI wrote last

A seat's CLI can fail for a reason only it knows: a model name it no longer
has, a flag it dropped, a sign-in that ran out. The host passes on what the
CLI itself wrote, so that nobody has to open a log to learn it
(`cli_words.py`). It decides nothing from those lines: no retry, no stage
and no verdict rest on them. It does not read them for a cause either: it
quotes, and beside the quote it names what a person can change, in sentences
made from the signed seat alone.

What is quoted are the last lines that came through the pipe the host holds:
at most 6 lines of at most 200 characters, 800 in all, from the last 8 KiB,
with terminal control sequences removed and bytes that are no UTF-8 taken
out. The host keeps that end of each pipe in memory while the process runs,
so the quote is the end of what was printed also when the log is full at
4 MiB, and what a seat wrote into a file under `logs/` is not quoted.

The report is written from the files of the shift and has no pipe. A builder
turn's lines are in the state, where the host put them. For a run of the
reviewer the report makes them again, by the same rule, from the two logs the
review receipt names, and only where their bytes are still the ones it names
(or the host's kept copy is). Two runs it does not quote, since their log is
not what the reviewer printed last: one whose log the host found written to
by something else while the review ran, which the receipt says in a reason,
and one that printed more than the log holds. The answer to the driver, which
comes from the pipe, has the lines in both cases.

- **A builder turn** gets `said`, the list of those lines, in its row in
  `driver-state.json` when its CLI ended by itself and not well: with a
  status other than 0, by a signal the host did not send, or with status 0
  and `receipts/` as it was before. `sortie wait` returns the row as
  `last_turn`, so the lines reach the driver as a list of their own and never
  inside the host's sentences. While a later turn runs, `last_turn` is the
  turn before it, and the `note` then says only that, and that its lines are
  output and nothing to follow. For another status or a signal the `note`
  says that the CLI ended so, where the lines are and that they are output
  and nothing to follow, names the log, names what a person can change, and
  says to record it as the blocker with the developer as next owner: a
  driver cannot change a seat. A turn the host ended (its limit, a stop) and
  a turn that did not start get no `said`: their last lines are work in
  progress and no failure of the CLI.
- **A run of the reviewer** that ended by itself with another status than 0
  or by a signal is quoted from its error output, and from its standard
  output when the error output is empty. A run that ended with status 0 and
  gave nothing the host could take is quoted from its error output only: its
  standard output is then whatever stood in place of the review. When the
  review has failed, `last_review` carries `said`, with `attempt` and
  `lines`, for the last attempt that has such lines, and `detail` says so
  and names what a person can change. The reasons in `detail` are cut so
  that those sentences are whole in its 2000 characters. The review receipt
  is as it was: it holds no such lines. The host process that ran the
  review holds them; a host process started later does not, and its answer
  then has none. A run that ended by itself and not well and of which the
  host holds no lines, because it wrote nothing or a later host process
  answers, still gets the sentences that name what a person can change, in
  `detail` and under the attempt in the report.
- **A run of the reviewer that the host ended at its limit** is not quoted.
  `detail` says which attempts ended so, how long one run may take in this
  shift, and the two things a person can change in a new shift: a longer
  `--review-limit`, or the reviewer's model in `--agent reviewer=`. The host
  does not know which of the two it is.
- **The driver's CLI** has the terminal itself. The host reads none of what
  it writes and keeps none of it, so nothing is quoted. When the host saw
  the driver's process end by itself with a status other than 0 or by a
  signal, the terminal's last lines say, after `Driver process:`, that what
  the CLI wrote is above where the CLI left it there, and name what a person
  can change. They need no report.

What a person can change is named the same way for each seat: the model, as
it stands in `--agent ROLE=NAME:MODEL`; the options the CLI is started with,
which are one form of the profile (`headless_argv`, `headless_network_argv`,
`review_argv`, `interactive_argv`) and are changed in a profile of your own,
see [agent profiles](agents.md); and the CLI's own sign-in and settings.
`sortie doctor` is named for what it shows, and for what it does not: a
sign-in, and whether a model exists. No one flag is named, because the host
does not read the quote. For the same reason the sentences end by saying that
none of these need be what went wrong: a CLI also ends not well where no
model name, option or sign-in is at fault, as when a model's provider refuses
the content of a request, and then the quoted lines say so and these
sentences only name what there is to change.

**What is left out.** A CLI's error can carry a credential, and the report is
the file people pass on. Before any line is cut, these are replaced by
`[left out]`:

1. the value of an assignment to a name that ends in `key`, `token`,
   `secret`, `password`, `passwd`, `credential` or `cookie`: the name, then
   `=` or `:`, then the value. A value that opens with a quote goes up to
   that quote again, spaces included; any other goes up to a space, quote,
   comma, brace or `&`. A quote may have any number of backslashes before
   it, as in a JSON text that is itself inside a string, once or more often.
   A quote inside the value has more of them than the one that opened it and
   does not end it; a value whose quote does not come back goes to the end of
   the line, and so may one that ends in a backslash. The name alone is prose
   and is kept: `invalid API key provided` stays as it is;
2. what follows `Authorization` and its scheme word, and the value of
   `Cookie` or `Set-Cookie`: as a header the whole rest of the line, whatever
   quotes it holds, and as a quoted field up to its closing quote, found as
   in 1; also what
   follows `Bearer` or `Basic` anywhere, unless that reads as a plain word;
3. the user and password of a URL;
4. a word that begins like a known credential (`sk-`, `sk_`, `ghp_`, `gho_`,
   `github_pat_`, `xox`, `AKIA`, `AIza`, `eyJ`) and goes on for 8 characters
   or more;
5. an unbroken run of 32 or more letters, digits, `+`, `=`, `_` and `-` that
   does not read as words: it is kept when every piece between `-`, `_` and
   `=` is at most 24 characters and is letters only, digits only, or at most
   4 characters. So a long model name or flag is kept, and a run of mixed
   letters and digits, a full commit ID or a SHA-256 is left out.

Where more than 8 KiB were printed, the first line of what is kept may be the
end of a longer one, which no mask could read as what it was: it is dropped
whole, also when it is the only line, so of one line longer than 8 KiB
nothing is quoted. A charset escape of a terminal and a byte that is no
UTF-8 are taken out before the masks run, so that neither parts a credential
into two words that are each too short to be seen as one.

**This is a filter by shape and no proof that no secret is shown.** A short
secret that none of the five names is shown. A path keeps its slashes and
dots and is shown. The whole output stays where it was, in the logs of the
shift on your machine; the report did not hold any of it before and now
holds these lines, and the driver, which could read the log before, is
handed them. Nothing is quoted of a turn or a review the host stopped, and
nothing of the driver's terminal.

The end of a review is finished by a later try of the same host process when
the first one ran out of time. Writing the end takes several writes under one
bounded lock: the receipt, its entry in the ledger, for `CHANGES` the findings
file, then the state. The host builds the receipt once and keeps it in memory
until the state is saved. When the time under the lock runs out anywhere
after that, the state on disk still shows the review running. At its next
chance (the next poll, the next command, or the closeout) the host works
nothing out a second time and finishes that same receipt: a file already
written is not written again and must be byte for byte what the host built; a
slot already recorded is not recorded again and must hold exactly that
receipt's id and digest. Then it goes on to the same outcome, the findings
file and the state. One receipt, one ledger entry. The serving host does not
end the shift for such a lock that ran out: it tries again at its next pass,
and ends with a failure only when several passes in a row run out.

A receipt on record for the open review that the host did not build backs
nothing, whatever it says and however well it is formed: the task is
`blocked` and not accepted. That covers a seat that records a receipt of its
own with `sortie receipt validate --record` while a review runs; a reviewer
knows its marker, its command and its own output, so only what the host
itself built is taken. A host process started later holds none of this in
memory and takes nothing up; for it the review is lost. Before this, the
second try found the slot taken, or its own receipt file in `receipts/`, and
blocked a task the reviewer had accepted.

## Wind-down

Wind-down is the last part of the signed window: twenty minutes, or the last
third of a window shorter than an hour. It is not the end of the shift. In it
nothing new starts: `brief`, `repair` and a `continue` that would start a
builder turn are answered `blocked`, say what can still be done, and leave
the lifecycle open. A builder turn that
started before it runs on to its own limit. A review is the one thing that can
still start: only when its whole signed limit fits in the working time left,
and the same test is made for its second attempt. When it does not fit, none
is started, the receipt that was handed in stays taken, and the report says
the work was handed in and not reviewed. With the default limit of ten
minutes that is every review in the wind-down of a shift of thirty minutes or
less.

That test is made once, when the review or its second attempt is begun, and
the time the review ends is fixed in the same moment. The host then prepares
the review's directory, which takes up to the time of one command, fifteen
seconds, and usually under one. It does not ask again afterwards whether the
review may start. Two things follow, and both are meant:

- A review begun shortly before wind-down can have its reviewer started
  after wind-down began, by up to fifteen seconds.
- A review begun in wind-down, while its whole limit fit, can have its
  reviewer started when up to fifteen seconds less than the limit are left.
  It runs that much shorter. The signed limit is the most a review may
  take, not a time it is owed.

The fifteen seconds are counted on the clock that runs while the machine is
awake. They are not a bound on the time of day: when the machine sleeps
while a review is prepared and working time is left when it wakes, the
reviewer is started that much later after wind-down began, with as much
less of the working time left, and is stopped with the working time as any
review is.

Neither review ends later than it would have, since its end was fixed
before it was prepared. Two checks are made after the directory is
prepared, and a review that fails one is not started and is a failed
attempt: its own time must not be over, and the working time of the shift
must not be over, which can happen without the first when the machine slept
while the review was prepared. `consume`, `close`, `set-aside`, `progress`,
`stop-evidence`, `note` and `finish` are served as before, so the driver can take what
that turn hands in and write its last statement. The host begins to stop all
work a short closing margin before the signed deadline: ten seconds, or half
of a wind-down shorter than twenty seconds. That moment is the start of a
bounded shutdown, not the instant every process is gone. The host ends the
shift with reason `working time over: stopped before the signed deadline`,
signals the builder turn and waits up to a second for it, then the driver and
up to two seconds, and only then drains the builder's last output into its
log, writes the turn's end into the state, looks at the repository and writes
the report; the margin and the signed
finalization allowance are what all of that has. No wait for a lock or for
the disk stands between the two stops. To reach that moment on time, nothing
the host waits for while it serves the seats is given more than the working
time left: its lock waits, the reading of a command, the handling of a
command, the validation of a receipt, the wait for a builder turn that ended
or ran into its limit to be gone, and the recording of that end all end with
it. This timing is best effort. The closing margin is when the host aims to
begin, and the two waits above are the most it gives each process group to be
gone, not a promise that either is. A stop can come later than planned
when something before it is slow, and a group
that was not seen gone within its wait is reported as a stop that could not
be confirmed. No order between the stops is promised. In particular, when a
builder turn ends just before the stop, the host can still write that turn's
last output into its log after the working time is over and before it stops
the driver.
There
is no minimum: an operation is not given time that the shift does not have.
Once the working time is over the serving host takes no command, `finish`
included: a command it had already read when the time ran out is answered
`invalid` with `working time over`, has changed nothing, and its answer has a
quarter of a second to be written, or what is left of the finalization time
when that is less; the host closes the shift itself. A turn
whose end the host saw but had no time left to write is written during that
shutdown. The host is one loop and checks the clock between file operations,
so a single slow operation can still carry it a little past; a command cut
off there is answered as not taken or as an incomplete effect. Only a wait
that ran out of the time it was given, exactly because the working time ran
out, is the ordinary ending. A storage error at that moment is an error like
at any other: the reason names it and `sortie start` exits with status 3.
One part is not clipped this way: the handler for builder-requested check
commands keeps its ten-second lock waits. No command line switches those
checks on.
In an earlier version the host ended the whole shift when wind-down began, which
stopped a build in flight with a third of the window unused while `sortie
wait` counted down to the deadline.

Expiry, withdrawal or source change stops delegation. The host observes
termination signals. At every ending it stops the running builder turn, then
the driver process, then looks at the repository (below), then writes the
report; the closeout's `processes` entry says what it
saw of the driver process, and its `cleanup` says `done` when the report was
written after that stopping, `incomplete: ...` when stopping or closing the
builder turn failed, and `not yet performed` in a report written earlier (a
`finish` command publishes one before the host stops anything). The stops are
bounded attempts. The driver gets a stop request, a kill after one second and
two seconds in all. On macOS a process that is inside its own exit can be
neither signaled nor reaped until the kernel is done with it, and a look at
its group is refused; when the two seconds end that way the driver line says
`it was exiting and had not finished when the host stopped waiting`. After
the look at the repository the host looks at the driver once more, without
waiting or signaling; a driver seen gone by then is recorded as `seen ended
after the host stopped waiting` and the shutdown counts as confirmed.

What could not be confirmed is kept apart from the reason, which stays what
ended the shift (often the driver's own closing words). The closeout's
`processes.shutdown` holds `confirmed`, a list of `problems` (`the driver
process was not seen to stop (...)`, `a builder turn was not seen to stop`,
`the terminal could not be restored`, `cleanup was incomplete: ...`) and
`groups`, the process group ids to look for. From it the report's Outcome and
the last lines of `sortie start` begin with two separate lines: `Tasks: N
accepted locally of M briefed (ID at COMMIT)` and `Shutdown: confirmed.` or
`Shutdown: NOT CONFIRMED.` with the problems and a `ps` command that lists
what is left of those groups (`NOT CLEAN` when every process was seen to stop
and only the terminal or cleanup is at fault). M counts the tasks the driver
briefed, which is all the host can count: it cannot read a direction. So the
`Tasks` line ends with what the driver stated about the rest in its `finish`:
`the driver names K more as not started`, `the driver says the direction held
no more` (an empty `not_started`; with no task briefed these read `names K as
not started` and `held nothing to brief`), or, when no list was given or the shift
ended without a `finish`, `whether the direction held more is not known to the
host`. Beside an unconfirmed shutdown
the report's Status line refers to it and neither `Cleanup: done` nor a bare
`None found.` is printed. A closeout from before this entry existed is read
by its driver line, its `cleanup` value, the note the earlier host put at the
end of the reason and the builder turns recorded with `stop not confirmed`;
an entry of the wrong shape is shown as `not recorded`, never as confirmed.
The terminal's `Shutdown` and `Driver process` lines repeat what the host
itself saw, the same values it gave the closeout, also when the report could
not be written; the `Tasks` line comes from the final report only. The reason
and the driver line are printed there with anything but printable ASCII shown
as an escape. After a driver whose CLI ended by itself with a status other
than 0 or by a signal, two more lines follow `Driver process:`; see "What a
CLI wrote last" above.

Exit status of `sortie start` for a shift that ran: 0 when it ended in an
ordinary way and the shutdown was confirmed, whether or not any task was
accepted; 3 when it was cut short, or the shutdown was not confirmed or not
clean, or the final report was not written (the returned reason then ends
with `the final report was not written`); 130 on a keyboard interrupt. A 3
after an accepted task with `Shutdown: NOT CONFIRMED` is about the shutdown,
not about the task. The next owner a driver named with `finish` is kept at a
normal ending. When a stop was not confirmed, the terminal was not restored
or cleanup was incomplete, and at every ending the driver did not close
itself, the next owner is `developer`. A turn recorded as started with no recorded end is listed in
the report as an issue. A report is not guaranteed: `sortie start` prints
`Report:` only when this host published its final report and the `report.md`
on disk names the digest of the `report.json` beside it, written after
cleanup. Any other `report.md` is called `Preliminary report`, and otherwise
the command says none was written; in both of those cases it names the
`sortie report` command. A `finish` or blocking command whose result was
saved is answered with that result even when the early report could not be
written; the host writes the report again at its end. `report.md` is a minimal driver
handoff written from retained state under a local budget of at most five seconds,
clipped to the signed finalization window and monotonic admission deadline.
Coordination lock waits share that bound, and every subsequent read/write
rechecks it; expiry cannot publish a completed file command or report.
The host's live state-read lock is bounded by the remaining shift and admission
budgets, checks live authority while waiting and after acquisition, and leaves
the signed finalization window for the local handoff. A lock that remains
unavailable throughout finalization can still prevent report publication.
If that window has already elapsed, the host writes no new report; retained
state remains available for later developer-owned audit.
It records local acceptance or missing acceptance, blockers/owners and known or
unknown builder cessation. It performs no network operation and starts no
agent. Observed exit of a process the host started is reported as that; only a
stop receipt is a seat's own statement, and toolkit exit is never treated as
seat cessation or remote delivery.

### What was left in the repository

This look is advice to the developer about their own repository, not
evidence. It is what Git reported when run as the developer, and Git reads
the developer's and the repository's Git configuration to answer. What the
report lists is the paths this bounded look saw, which can be fewer than
`git status` shows: ignored paths are left out, and a look stops at its limits
of time and size. The report tells three cases apart. When the look at the
end failed or was skipped, it says what is uncommitted could not be read and
to run `git status`. When a look was cut at a limit, it lists what was seen,
says that this is not the whole picture and to run `git status`. When the
look at the start failed, it lists what the look at the end saw and says that
whether those paths were already there when the shift began is not known.
When the look at the start was cut, paths it did see are still counted and
not listed, the others are listed, and the same is said of them.

A turn that is stopped, or gives up, can leave half a change in the
developer's working tree. The host therefore looks at the target repository
twice, when it begins and after it stopped both seats' processes (also when a
stop could not be confirmed), each time within three seconds, and records both
looks in the closeout as `repo`: `{start, end}`, each `{head, changes,
truncated, git_dir, common_dir, pinned, remotes}` or `{error}`. `remotes`
are those of the repository's own configuration, at most 20 (beyond that
`remotes_truncated` is true), each `{name, sha256}`: the SHA-256 is of the
remote's configured URLs, so that a later look can say that they changed,
and no URL is kept. A look whose read of the remotes failed has no
`remotes` key and is otherwise whole; a closeout of an earlier build has
none either. `git_dir` is the
working tree's own Git directory and `common_dir` the one its refs and
objects come from; the two differ for a linked worktree. The look at the end
reads only the ones the look at the start reported: if the repository
resolves to another of either by then (a `.git` file or a worktree's
`commondir` rewritten during the shift), it is an `error` and nothing is
listed. Every Git command of a look after the first is told all of these
places outright and looks none of them up again. `pinned` is true for a look
that was held to an earlier one in this way. It is false for the look at the
start, and for a look at the end when the look at the start failed: that one
describes whatever the repository resolved to then, and the report says the
age of what it lists is not known. When the look at the start saw uncommitted
paths, the host asks one more thing in what the look at the end left of its
time, and keeps the answer as `repo.held`: which of those paths a commit
between the HEAD at the start and the HEAD at the end added or changed
(`{paths, complete}`, or `{error}`). It is one `git log` over those commits,
bounded and told the same places, and it is skipped when HEAD did not move.
Each Git
command of a look has one deadline for running, being stopped and being
collected, so stopping a slow one does not take time kept for the report. `changes` rows are `{path, state}` with state
`untracked`, `staged` (the index differs from HEAD), `deleted` (in the index,
not on disk) or, at the end only, `changed`. At most 200 rows and 16 KiB of
them are kept from one look, Git's output is read as it comes and no further
than 8 MiB or the look's time, and `truncated` says when any of these cut the
picture short. The look at the start comes out of the working time and is
never given more than is left of it. The look at the end is skipped, with an
`error` saying so, when it would leave less than two and a half seconds: two
for the report and half a one for the question about the hold against idle
sleep.
`head` is null only when Git answered that HEAD names no commit; a HEAD that
could not be read makes the whole look an `error`.

The look runs Git for listings only (`rev-parse`, `ls-files`, `diff-index
--cached`) with hooks and the file-system monitor switched off, and tells Git
the Git directory and the working tree outright, so a `core.worktree` in the
repository's configuration cannot make it list another directory. It does not
ask Git to compare tracked files with the working tree, because Git runs the
repository's own filters for that and a builder may have written that
configuration. Tracked files are compared by the host instead: a file touched
since the host began whose bytes do not hash to what the index holds is
`changed`. The host opens such a file one path component at a time from the
repository root and follows no link, so a directory replaced by a link is
reported and not read through. Consequences: a tracked file that was already
modified before the shift and not touched during it is not listed, and one
that was touched during it is listed whoever made the edit; a change of the
executable bit alone is not seen; a file whose filter rewrites content on the
way in (line endings, large-file pointers) looks changed when touched; a file
larger than 8 MiB that was touched is called changed without being read;
ignored files are never listed. Git still reads the repository's
configuration, any file that configuration includes, and its ignore rules, as
for every command run there. A builder can write those, and an ignore rule
hides an untracked file from this listing, so an empty listing is not proof
that nothing was left. That is all those files can do to a look. Nothing
they hold is copied into it: Git's error output is discarded, and the
listings print names from this working tree and index only. No program runs
through them: hooks and the file-system monitor are switched off on the
command line, the listings use no filter, pager, alias or external diff, and
lazy fetching is off. The look does not confine what Git reads beyond that.
It runs as the developer, like the default builder, which can read whatever
the developer can. The report lists the rows that
are new at the end under "Not done" and gives the two HEADs under "Outcome".
It is an observation of the working tree, not a statement about which seat
wrote what.

The builder still works in the developer's own working tree. Building in a
scratch checkout, so that a stopped turn could leave nothing there, was
considered and is not done: it changes how the builder commits.

## Builder check exchange

**Off in a live shift.** The host started by `sortie start` runs no check
commands: a queued request is answered `blocked`, nothing executes, and the
response says so. The host runs as the developer, outside whatever the
builder's own agent configuration allows, and the toolkit makes no statement
about seat permissions, so it does not run commands for a seat. The builder
runs its tests and reproductions itself, inside its own CLI, and retains their
output as evidence. The rest of this section describes the helper as it
behaves where a caller switches it on (`run_shift(checks=True)`, used by the
toolkit's own tests); no command-line option does that.

The builder saves a raw reviewer check request and writes a closed JSON spec:

```json
{
  "version": 1, "id": "check-1", "task_id": "task-1",
  "request_id": "CURRENT_REVIEW_REQUEST", "round": 0,
  "finding_id": "F1", "candidate": "FULL_CANDIDATE_OID",
  "baseline": "FULL_BASELINE_OID", "baseline_reason": null,
  "review_request": {"path": "logs/reviewer-request.txt", "sha256": "SHA256"},
  "argv": ["python3", "probe.py"], "timeout_seconds": 30,
  "baseline_exit": 1, "candidate_exit": 0,
  "baseline_contains": "expected failure", "candidate_contains": "passes",
  "harness": null, "harness_reason": "Existing committed probe.py"
}
```

`baseline` may be null with a nonblank `baseline_reason`. An added `harness`
artifact is copied to the unused `.sortie-check` path in each scratch checkout;
use `{harness}` as a whole argv element for its path and set `harness_reason` null.
Expected output text must be nonblank; matched execution needs the expected exit
and text. Commands are direct argv, without shell interpolation. They must be
explicitly chosen authorized local work; this helper is not an OS sandbox.

```sh
sortie queue-check --shift SHIFT_PATH --request briefs/check-spec.json
```

This publishes `builder-check.json` only. It does not execute or grant authority.
The already-admitted BuilderHost checks the active review candidate/request/round,
raw request artifact, live grant, source pin, and remaining active time. It creates
detached baseline/candidate worktrees under `scratch/`, with checkout hooks
disabled, and HOME/TMP/output there. It runs bounded owned process groups, captures
at most 1 MiB per command, and retains exit, expected/observed output, commit and
harness identity. It stops on deadlines/guard changes and cleans up the owned
group; deliberately escaped descendants are not contained. Target repository Git
configuration and requested artifact code are trusted local execution inputs.
Worktrees/logs remain as evidence. No target source files are edited by the helper.

Wait for `builder-check-response.json` matching the ID/digest before replacing
the slot. A `recorded` response means execution observations matched, not that a
reviewer accepted them. `blocked`, `invalid`, missing or incomplete execution
cannot establish reproduction. Requests are durable and never re-executed under
the same ID. A crash after intent leaves uncertainty, not a successful check.
The helper accepts at most 128 requests per shift and finite timeout per request.

The helper predates the host-run review. A reviewer started by the host runs
no code and asks for no check, and its findings reach the builder through the
findings file; nothing in a live shift feeds this exchange.

Closeout now emits `report.json` and `report.md` from retained local evidence,
with a separate host observation in `closeout.json`. See [shift reports](reports.md)
for bounded publication, partial-start behavior, provenance and unknowns.
