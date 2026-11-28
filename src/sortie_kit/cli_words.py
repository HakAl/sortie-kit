"""What an agent CLI wrote last before it ended in failure, and what a person can change.

The host quotes these lines to the driver and in the report so that nobody has to
open a log to learn that a model name is gone, a flag was dropped or a sign-in ran
out. Nothing here has state and nothing is decided from the lines: no retry, no
stage, no verdict. They are a CLI's output, shown as text beside a record that is
made as before. The sentences that name what can be changed are built from the
signed seat alone and are the same whatever was quoted.

What is quoted is bounded and passed through masks (see `masked`). The masks are a
filter by shape and no proof that no secret is shown.
"""
from __future__ import annotations

from importlib.resources import files
import json
import re

TAIL = 8 * 1024  # bytes of the end of a pipe that are looked at
LINES = 6
LINE = 200  # characters of one line
TOTAL = 800  # characters of all lines together
LEFT_OUT = "[left out]"
CUT = " [cut]"  # ends a line that was longer than LINE; of fixed length, and counted in LINE
LATE = "exit seen after its limit"  # as lifecycle.LATE: a reviewer first seen over after its limit
# How the host's reason begins for a review log that is not what the reviewer printed: something
# else wrote to it while the review ran. The report, which has the log alone, quotes nothing of it.
ALTERED = "what is in the review's log ("
# The host's reason for a review of which a pipe did not give its end: its log is what the
# host read, and is not known to be all the reviewer printed. The report reads no usage from it.
UNDRAINED = "what the reviewer printed could not be read to its end"

NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")  # as agents.NAME
FORMS = {"driver": "interactive_argv", "builder": "headless_argv", "reviewer": "review_argv"}

# Terminal control sequences: CSI, OSC up to its end, the escapes with intermediate bytes
# (a charset designation such as ESC ( B, which a terminal reset writes) and the
# two-character escapes.
CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[ -/]+[0-~]|\x1b[@-Z\\-_]")
NO_TEXT = "\ufffd"  # what a byte that is no UTF-8 decodes to: taken out, so that it cannot part a credential in two
BREAK = re.compile(r"\r\n|\n|\r")
MARK = "\x00"  # stands for LEFT_OUT while the masks run; no cleaned line holds it

SECRET_NAME = r"(?:key|token|secret|password|passwd|credential|cookie)"
# What stands before a value: a name, a quote that may close the name, and `=` or `:`. The quote may
# have any number of backslashes before it, as it has in a JSON text that is itself a string, once or
# more often. ASSIGNED begins with the word a name ends in and not with the name's beginning: what
# stands before the word is kept as it is, and a pattern that began there was tried again from every
# character of a long word.
HEAD = r"(?:\\*[\"'])?\s*[:=]\s*"
AUTHORIZATION = re.compile(r"(?i)\b(?:proxy-)?authorization" + HEAD)
COOKIE = re.compile(r"(?i)\b(?:set-)?cookie" + HEAD)
ASSIGNED = re.compile(r"(?i)" + SECRET_NAME + HEAD)
OPEN = re.compile(r"(\\*)([\"'])")  # a value that opens with a quote: the backslashes before it, and the quote
SCHEME = re.compile(r"(?i)(?:(?:bearer|basic|digest|token)\s+)?")  # the word before a credential in Authorization
EVERYTHING = re.compile(r"[^\x00]+")  # a header's value: the rest of the line
WORD = re.compile(r"(?:[^\s\"',{}&\\\x00]|\\(?![\"']))+")  # a value with no quotes: up to a space, quote, comma, brace or `&`
BEARER = re.compile(r"\b((?i:bearer|basic)\s+)(?![A-Za-z][a-z]*(?![A-Za-z0-9._~+/=-]))[A-Za-z0-9._~+/=-]{8,}")
URL = re.compile(r"://[^/\s:@\x00]+:[^/\s@\x00]+@")
KNOWN = re.compile(r"(?<![A-Za-z0-9])(?:sk-|sk_|ghp_|gho_|github_pat_|xox|AKIA|AIza|eyJ)[A-Za-z0-9_.=+/-]{8,}")
RUN = re.compile(r"[A-Za-z0-9+=_-]{32,}")


def closes(line, begin, quote, depth):
    """Where a value ends that began at `begin` behind `quote` with `depth` backslashes before it:
    before the next such quote that has `depth` backslashes or fewer before it, or at the end of
    the line where none comes. A quote with more backslashes is one inside the value, written as
    an encoder writes it, and does not end it. A value that ends in a backslash has more of them
    before its closing quote too and is taken to go on, to the next quote that qualifies or to the
    end of the line: more is left out then, never less."""
    at = begin
    while True:
        at = line.find(quote, at)
        if at < 0:
            return len(line)
        before = at
        while before > begin and line[before - 1] == "\\":
            before -= 1
        if at - before <= depth:
            return before
        at += 1


def after(line, head, scheme, rest):
    """`line` with a MARK in place of each value that follows a match of `head`. A value that
    opens with a quote is left out up to where `closes` says; any other as far as `rest`
    matches, and where that is nothing, nothing is left out. With `scheme` the word that
    stands before a credential in an Authorization header is kept."""
    out, at = [], 0
    while True:
        found = head.search(line, at)
        if found is None:
            break
        begin = found.end()
        opened = OPEN.match(line, begin)
        if opened is not None:
            begin = opened.end()
        if scheme:
            begin = SCHEME.match(line, begin).end()
        if opened is not None:
            end = closes(line, begin, opened.group(2), len(opened.group(1)))
        else:
            value = rest.match(line, begin)
            if value is None:
                # A name and `=` with nothing after it. The search goes on behind the head.
                out.append(line[at:found.end()])
                at = found.end()
                continue
            end = value.end()
        out.append(line[at:begin] + MARK)
        at = end
    out.append(line[at:])
    return "".join(out)


def wordlike(run):
    """Whether a long unbroken run reads as words: every piece between `-`, `_` and `=`
    is at most 24 characters and is letters only, digits only, or at most 4 characters.
    A model name, a flag and a name in capitals with underscores do; a run of mixed
    letters and digits, a full commit ID and a SHA-256 do not."""
    return all(len(piece) <= 24 and (len(piece) <= 4 or piece.isalpha() or piece.isdigit())
               for piece in re.split(r"[-_=]", run))


def masked(line):
    """One cleaned line with everything left out that has the shape of a credential.
    Run on the whole line, before anything is cut, so that no cut can shorten a
    secret into something the masks pass."""
    # What follows Authorization and its scheme word, and a Cookie or Set-Cookie header. As a header
    # the value is the rest of the line, whatever quotes it holds; as a quoted field it ends with its quote.
    line = after(line, AUTHORIZATION, True, EVERYTHING)
    line = after(line, COOKIE, False, EVERYTHING)
    # What follows Bearer or Basic, unless it reads as a plain word ("Basic authentication failed").
    line = BEARER.sub(r"\1" + MARK, line)
    # The value of an assignment to a name that ends in one of the words. The name alone, followed
    # by a space, is prose and is kept.
    line = after(line, ASSIGNED, False, WORD)
    # The user and password of a URL, and a word that begins like a known credential and goes on.
    line = URL.sub("://" + MARK + "@", line)
    line = KNOWN.sub(MARK, line)
    line = RUN.sub(lambda found: found.group() if wordlike(found.group()) else MARK, line)
    return re.sub(MARK + r"(?:\s*" + MARK + r")*", LEFT_OUT, line)


def last(raw):
    """The last lines of what a CLI wrote, as a list of strings: at most LINES lines of
    at most LINE characters, TOTAL in all, from the last TAIL bytes of `raw`.

    Decoded as UTF-8 with replacement; terminal control sequences removed; every
    other character that is not printable becomes a space; a byte that is no UTF-8
    is taken out; empty lines dropped; masked; and only then cut. Where `raw` holds
    TAIL bytes or more its beginning may be the middle of a line, which the masks
    could not read as what it was: the first line is then dropped whole, also when
    it is the only one. What stands before a space in it may be the name or the
    scheme word that tells a mask what the value after it is."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return []
    partial = len(raw) >= TAIL
    text = CONTROL.sub("", bytes(raw[-TAIL:]).decode("utf-8", "replace")).replace(NO_TEXT, "")
    lines = BREAK.split(text)
    if partial:
        lines = lines[1:]
    cleaned = []
    for line in lines:
        line = " ".join("".join(c if c.isprintable() else " " for c in line).split())
        if line:
            cleaned.append(masked(line))
    out = [line if len(line) <= LINE else line[:LINE - len(CUT)] + CUT for line in cleaned[-LINES:]]
    while out and sum(len(line) for line in out) > TOTAL:
        out = out[1:]
    return out


def tail(kept, chunk):
    """The rolling end of a pipe: `kept` with `chunk` after it, at most TAIL bytes."""
    return (bytes(kept) + bytes(chunk))[-TAIL:]


def how(ended_by):
    """The first part of how the host recorded an end, before any `;` it put after it."""
    return str(ended_by).split(";", 1)[0].strip()


def at_limit(ended_by):
    """True for a turn or review that the host ended at its limit."""
    return how(ended_by) == "timeout"


def own_failure(ended_by, code):
    """True for a process that ended by itself and not well: with a status other than 0,
    or by a signal the host did not send."""
    first = how(ended_by)
    return first == "signal" or (first in ("exit", LATE) and type(code) is int and code != 0)


def turn_said(outcome, code, receipts_changed, end):
    """What is quoted of a builder turn: its last lines when it ended by the CLI's own
    doing and not well, which is a status other than 0, a signal, or status 0 with
    the receipts folder as it was before. Nothing of a turn the host ended or that
    did not start: its last lines are work in progress and no failure of the CLI."""
    if own_failure(outcome, code) or (how(outcome) == "exit" and code == 0 and receipts_changed is False):
        return last(end)
    return []


def review_said(ended_by, code, failed, out, err):
    """What is quoted of one run of the reviewer, from the ends of its two outputs.
    Of a run that ended by itself with another status than 0 or by a signal: its
    error output, and where that is empty its standard output. Of a run that ended
    with status 0 and gave nothing the host could take: its error output only,
    since its standard output is then whatever stood in place of the review.
    Nothing of a run the host stopped, and nothing of one that was taken."""
    if own_failure(ended_by, code):
        return last(err) or last(out)
    if failed and how(ended_by) in ("exit", LATE) and code == 0:
        return last(err)
    return []


def builtin(name):
    """The profile of that name that is built into sortie-kit, or None."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return None
    try:
        value = json.loads(files("sortie_kit").joinpath("data", "agents", name + ".json").read_bytes())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def named(seat, inert):
    """(agent, model) of a signed seat as text that is safe to show, with a word in
    place of what a seat of an unexpected shape does not give."""
    agent, model = (seat.get(key) if isinstance(seat, dict) else None for key in ("agent", "model"))
    return (inert(agent[:64]) if isinstance(agent, str) and agent else "NAME",
            inert(model[:128]) if isinstance(model, str) and model else "MODEL")


def knobs(role, seat, inert=str):
    """One paragraph that names what a person can change about a seat whose CLI failed,
    from the signed seat alone. `inert` makes the seat's own names safe where they are
    shown. Nothing of what a CLI wrote goes into it: it names places and never one flag. It
    ends by saying that none of them need be the cause: a CLI also ends not well where no model
    name, option or sign-in is at fault, as when a provider refuses the content of a request."""
    agent, model = named(seat, inert)
    profile = builtin(seat.get("agent") if isinstance(seat, dict) else None)
    form = FORMS[role]
    if role == "builder":
        argv = seat.get("argv") if isinstance(seat, dict) else None
        if profile is not None and argv is not None and argv == profile.get("headless_network_argv"):
            form = "headless_network_argv"
        elif profile is None:
            form = "headless_argv (headless_network_argv in a shift started with --builder-network)"
    where = ("built into sortie-kit: to change them, copy the profile under a new name into "
             "~/.config/sortie-kit/agents/ and name that" if profile is not None
             else "your own file ~/.config/sortie-kit/agents/" + agent + ".json")
    return ("What a person can change, in a new shift (a seat is signed and cannot be changed in this one): "
            "the model, which is " + model + " in --agent " + role + "=" + agent + ":" + model + "; the options the "
            "CLI is started with, which are " + form + " of the profile " + agent + " (" + where + "); and the CLI's "
            "own sign-in and settings, outside sortie-kit. "
            + ("sortie doctor" if profile is not None else "sortie doctor --agent " + agent) + " shows whether the "
            "program is found and which of those options its help lists today; it does not show a sign-in or "
            "whether a model exists. None of these need be what went wrong: the host saw how the process ended, "
            "and not why.")


def limit_words(seat, limit, inert=str):
    """For a run of the reviewer that the host ended at its limit: how long one run may
    take in this shift and the two things a person can change. No guess at which."""
    agent, model = named(seat, inert)
    most = (str(limit) + " seconds" if type(limit) is int and limit > 0 else "the signed review limit")
    return ("One run of the reviewer may take at most " + most + " in this shift, and less when less working time "
            "is left. What a person can change, in a new shift: a longer --review-limit, or the reviewer's model, "
            "which is " + model + " in --agent reviewer=" + agent + ":" + model + ". The host does not know which "
            "of the two it is: it saw a run that was not over at its limit, and not why.")


DRIVER_EXIT = re.compile(r"exited on its own with status ([0-9]+)")


def driver_failed(seen):
    """True when what the host saw of the driver's process says that it ended by itself
    and not well, whatever follows a `;`."""
    text = str(seen)
    found = DRIVER_EXIT.match(text)
    return text.startswith("ended on its own by signal") or (found is not None and int(found.group(1)) != 0)


def driver_words(seen, seat, inert=str, terminal=True):
    """Lines for a driver whose CLI ended by itself and not well, or none. Nothing is
    quoted: the driver's CLI has the terminal itself, and the host reads none of it.
    `terminal` is false where the lines are not printed in that terminal."""
    if not driver_failed(seen):
        return []
    where = ("is above in this terminal, where the CLI left it there" if terminal
             else "was in the terminal the shift was started in, where the CLI left it there")
    return ["The driver's CLI ended by itself and not well. What it wrote " + where + "; the host does not read or "
            "keep what a driver writes.", knobs("driver", seat, inert)]
