"""What a seat's CLI stated of the tokens it used, as the host read it from the pipe.

Nothing here measures anything. A figure is a statement of the process a seat ran,
on a pipe that the seat's own work also reaches, and is kept as that: "as the CLI
reported it". A figure that is missing, malformed or out of bounds is not reported,
with a reason from a fixed list, and never 0. No I/O in this module.

One form is known: the text form of `codex exec`, which ends its transcript with a
line `tokens used` and a line with one number.
"""
import re

TAIL = 64 * 1024  # bytes of the end of a pipe that are looked through for a figure
BOUND = 10 ** 12  # the largest count that is taken for one
DIGITS = 17  # characters of the number line: checked before the number is made
MARKER = "tokens used"
NUMBER = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\Z")
BREAK = re.compile(r"\r\n|\r|\n")
CODEX_TEXT = "codex-text"
FORMS = (CODEX_TEXT,)
# Why a run has no figure. The state keeps the word, the report says the sentence.
REASONS = {
    "no_form": "its CLI states no usage in the form the seat runs it in",
    "not_ended": "the process did not end on its own with status 0",
    "no_end": "the end of what it printed was not seen",
    "no_figure": "no figure in what it printed",
    "malformed": "the figure is malformed or out of bounds",
    "not_recorded": "the host that ran it recorded none",
    "no_log": "the kept log is not there, is not what the host kept or could not be read",
}


def form(seat):
    """The form a signed seat states usage in, from the seat object of the grant alone,
    or None. It decides which reader runs and nothing else."""
    if not isinstance(seat, dict) or seat.get("agent") != "codex":
        return None
    argv = seat.get("argv")
    if not isinstance(argv, list) or not argv or argv[0] != "exec" or "--json" in argv:
        return None
    return CODEX_TEXT


def roll(held, chunk):
    """The rolling end of a pipe for the readers here: `(bytes, wrapped)`, at most TAIL
    bytes, `wrapped` once anything was let go at the front."""
    kept, wrapped = held if held is not None else (b"", False)
    joined = bytes(kept) + bytes(chunk)
    return joined[-TAIL:], wrapped or len(joined) > TAIL


def none(reason, named=None):
    """A record that says no figure is reported, and why."""
    record = {"not_reported": reason if reason in REASONS else "malformed"}
    if named in FORMS:
        record["form"] = named
    return record


def codex_text(raw, wrapped=False):
    """The one total of the text form. The LAST line that is exactly the marker decides:
    the line after it is the number or nothing is reported. An earlier pair, which a
    transcript can hold from a file the run read, is never taken in its place."""
    if not isinstance(raw, (bytes, bytearray)):
        return none("no_figure", CODEX_TEXT)
    lines = BREAK.split(bytes(raw[-TAIL:]).decode("utf-8", "replace"))
    if wrapped or len(raw) > TAIL:
        lines = lines[1:]  # the first may be the end of a line whose beginning was let go
    at = [i for i, line in enumerate(lines) if line == MARKER]
    if not at:
        return none("no_figure", CODEX_TEXT)
    after = lines[at[-1] + 1] if at[-1] + 1 < len(lines) else ""
    if len(after) > DIGITS or not NUMBER.match(after):
        return none("malformed" if after else "no_figure", CODEX_TEXT)
    count = int(after.replace(",", ""))
    if count > BOUND:
        return none("malformed", CODEX_TEXT)
    return {"form": CODEX_TEXT, "stated": {"total": count}}


def read(named, end, *, ended, whole):
    """The record of one run. `named` is the seat's form, `end` the `roll` of the pipe
    the form's figure stands on, `ended` whether the host saw the process end on its
    own with status 0, `whole` whether that pipe gave its end. A run that did not
    finish has no figure of its own, whatever its transcript holds."""
    if named not in FORMS:
        return none("no_form")
    if not ended:
        return none("not_ended", named)
    if not whole:
        return none("no_end", named)
    raw, wrapped = end if end is not None else (b"", False)
    return codex_text(raw, wrapped)


def checked(record):
    """A record as it may stand in the state or be shown: the shapes `read` gives and no
    other. Anything else is None."""
    if not isinstance(record, dict):
        return None
    named = record.get("form", CODEX_TEXT)
    if not isinstance(named, str) or named not in FORMS:
        return None
    why = record.get("not_reported")
    if set(record) <= {"form", "not_reported"} and isinstance(why, str) and why in REASONS:
        return record
    stated = record.get("stated")
    if (set(record) == {"form", "stated"} and isinstance(stated, dict)
            and set(stated) == {"total"} and type(stated["total"]) is int and 0 <= stated["total"] <= BOUND):
        return record
    return None


def total(record):
    """The one total a record states, or None: never 0 for a figure that is not there."""
    record = checked(record)
    return record["stated"]["total"] if record is not None and "stated" in record else None


def summed(runs, *names):
    """Totals of run rows by the keys `names`, in the order the groups first appear: for
    each group the sum of the totals stated, of how many runs it is, and how many runs
    there were. A group in which no run stated one has the total None."""
    groups = {}
    for run in runs:
        row = groups.setdefault(tuple(run.get(name) for name in names),
                                dict({name: run.get(name) for name in names}, total=None, counted=0, runs=0))
        row["runs"] += 1
        count = total({key: run[key] for key in ("form", "stated", "not_reported") if key in run})
        if count is not None:
            row["total"], row["counted"] = (row["total"] or 0) + count, row["counted"] + 1
    return list(groups.values())
