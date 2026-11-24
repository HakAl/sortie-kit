# Who reviews

The reviewer is a third seat. You sign it like the other two: its program, its
arguments, its model and how long one run may take. You are not asked who it
is.

- With no `--agent reviewer=`, the start looks at the agent profiles that can
  review and whose program is installed, in the order `claude`, `codex`,
  `agy`, then your own profiles by name. It takes the first whose model family
  differs from the builder's. The driver's CLI is a fine reviewer. The summary
  says `picked: the first installed agent of another family than the builder`.
- When none of another family is installed, it takes the builder's own CLI.
  That is the one-CLI case and needs no flag. The summary, the terminal and
  the report say `same-family review` and the reason, that no agent of
  another family is installed. A same-family review shares the builder's
  blind spots; read its accepts with that in mind.
- `--agent reviewer=NAME[:MODEL]` names it yourself, and the summary says
  `given with --agent reviewer=`. Naming one of the builder's own family while
  an agent of another family is installed is refused unless you also pass
  `--allow-same-family-review`.
- When no installed profile can review, the start refuses before anything is
  prepared or signed and says so in one sentence.

A picked reviewer runs with the model its profile names for reviews
(`claude-sonnet-5-5`, `gpt-6.1-sol` and `gemini-4-hydrogen-high` in the
built-in profiles). If your CLI does not accept that model, name one with
`--agent reviewer=NAME:MODEL`.

The start does not check that a CLI is signed in: nothing shows
that without a model call. A reviewer that is not signed in fails its
reviews, and a task whose review failed twice is blocked, not accepted.

The host starts the reviewer each time the builder hands in a plan or a build,
with a limit on its time, and reads its answer. The answer has to end with one
object in a fixed form ([the review contract](review.md)); the host checks
it and writes the review's receipt itself. The builder does not run, check or
report its own review.

- `ACCEPT` moves the task on.
- `CHANGES` hands the findings to the builder, which answers each one in its
  next receipt; the next review is given those answers. Three rounds at most.
- A run that gives nothing usable (no object, a wrong one, an error exit, the
  limit passed, or a repository whose commit or status changed while it ran)
  is a failed review.
  It is not an accept and it uses no repair round. The host runs it once
  more; a second failure blocks that task for you and the shift goes on with
  the next one.
