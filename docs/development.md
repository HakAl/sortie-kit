# Development

How to run the tests and the checks, where they have been run, and how the
tree is laid out.

## Running the tests

```sh
python3 tests/run_suites.py
```

This one command runs every test in `tests/`, found by discovery, from the
checkout with nothing installed. It gives Git an empty configuration and
removes the SSH agent from the run, so the signing tests reach only the
throwaway keys they make. A name narrows it to a module, a class or a
method: `python3 tests/run_suites.py test_report`.

Tests keep scratch beneath `.scratch/`, use fixture agents and disposable
terminals, and create and
reap their own bounded fixture subprocesses. They never run a real agent CLI.
The agent CLIs themselves are covered by fixtures only: no test starts
`claude`, `codex` or `agy`.

## The other checks

`tests/readme_run.py` runs the documented commands from a fresh install: the
README's, and those of [the complete example](example.md),
[other ways to run it](running.md) and this page, each taken from the page
that holds it. `tests/check_install.py`
checks an installed wheel. `tests/check_sdist.py` and `tests/check_wheel.py`
account for every member of a built source distribution and of a built wheel.

## Where they have been run

The tests have been run on macOS with Python
3.11, 3.13 and 3.14 on the developers' machine. On hosted runners, the
workflow `ci.yml` has passed on public commit `225e91a` in all eight of its
jobs: macOS 15 and Ubuntu 24.04, each with Python 3.11, 3.12, 3.13 and 3.14,
with the tests, the build, the install check, the wheel and source
distribution checks and the documented commands. The Ubuntu jobs report and
do not gate, and passing there does not make Linux supported. A hosted
runner is not a real shift: no job starts a real agent CLI, and no real
shift has run on Linux. [What real shifts have shown](evidence.md) lists
what has and has not happened on one.

## What the code holds to

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

The earlier pane transport, its journal and the runtime permission examples
were removed; `sortie verify` still reads grants signed for them, and
`sortie report` still reads their shifts.

## How the tree is laid out

- `src/sortie_kit/`: the package. `cli.py` is the `sortie` command.
  `data/agents/` holds the built-in agent profiles, `data/templates/` the
  texts the seats and the reviewer are given, and `data/receipts/` the
  receipt templates and schemas.
- `tests/`: the tests, their fixtures, the runner `run_suites.py` and the
  checks named above. `tests/sdist-files.txt` lists what the source
  distribution holds.
- `docs/`: these pages, and `source-provenance.json`.
- `.github/workflows/`: `ci.yml`, described above, and `release.yml`, which
  runs for a `v*` tag whose commit has a successful run of `ci.yml` on
  `main`, builds and checks the wheel and the source distribution, and
  attaches them with `SHA256SUMS` to a pre-release. Pushing such a tag
  publishes.

See [source provenance](source-provenance.json) for what was adapted from
earlier code, and [SECURITY.md](../SECURITY.md) for how to report a security
fault privately.
