# Shift state

This file is written once, when the shift is prepared, and is not updated
afterwards. It describes the moment of preparation: nothing signed, nothing
admitted, no agent started. If you are a seat reading it, the shift has been
signed and admitted since, and this file is not its state.

- Direction: `direction.md`
- Grant draft: `grant.draft.json` (no authority). Once signed, the grant is
  `grant.json` with its signature beside it.
- The record of a running or finished shift is `driver-state.json`, with the
  open task phase in `current-context.json`; driver.md describes the lifecycle
  and its file protocol.
- Before signing, the next action is the developer's: sign and start the shift
  from their own terminal.

Delivery, acknowledgment, progress, acceptance, and actual cessation are separate
observations. A started builder turn or an exited process does not establish
completion.
