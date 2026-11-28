"""The review contract. Nothing here starts a reviewer.

A reviewer is any agent CLI. It is told what to do by one of two packaged
texts and must end its answer with one JSON object between two marker lines
that carry a value made for that run. The host takes a verdict only from an
object that parses, has exactly the fields below, names the target the host
asked about and does not contradict itself. Anything else is a failed review:
never an accept and never a request for changes.

A commit review answers the brief requirement by requirement, under
`requirements`, and each of its findings names the requirement it says is not
delivered in some case, or none; its object is `sortie-kit.review.v3`. A plan
review does neither, and its object is `sortie-kit.review.v1`, as a commit
review's was in shifts kept from before the list. `sortie-kit.review.v2` is
the list without the name on a finding and without the status `disclosed`:
shifts kept from between the two hold it, and it is read as it was.
"""
from __future__ import annotations

from importlib.resources import files
import json
import re
import secrets
import textwrap

from . import channels
from .layout import Refused

SCHEMA = "sortie-kit.review.v1"
SCHEMA_V2 = "sortie-kit.review.v2"  # v1 and the requirement list; asked of commit reviews only
SCHEMA_V3 = "sortie-kit.review.v3"  # v2, a finding that names its requirement, and the status `disclosed`
SCHEMAS = (SCHEMA, SCHEMA_V2, SCHEMA_V3)
LISTED = (SCHEMA_V2, SCHEMA_V3)  # the schemas with a requirement list: of a commit review only
KINDS = ("plan", "commit")
VERDICTS = ("ACCEPT", "CHANGES")
SEVERITIES = ("blocker", "major", "minor")
BLOCKING = ("blocker", "major")
FIELDS = {"schema", "verdict", "target", "findings", "prior", "examined", "not_examined"}
FINDING = {"id", "severity", "location", "claim", "evidence", "recommendation"}
# What the host did with the builder's receipts for one run of a review (`review_dir.make`): shown
# under evidence/ with the host's statement; not asked for, since the review's time left no room
# for it; asked for and not begun, since preparing the directory used the time up; begun and not
# written in full. An attempt in a receipt carries one of these, or nothing where it is not known.
EVIDENCE = ("shown", "no_time", "late", "failed")
UNSEEN = "no time was left for it before this review had to start"
NOT_SHOWN = dict(no_time=UNSEEN, late=UNSEEN,
                 failed="the directory evidence/ could not be written in full; what lies there has no "
                        "statement of the host and is not to be relied on")
SHOWN = ("- `evidence/`: the builder's receipts for this task that the host has taken\n"
         "  and the files they name, as they were then. Those are the builder's\n"
         "  account, content like the rest. `evidence/HOST.md` is the host's own\n"
         "  statement of what it took, when, what it checked and what is not shown.\n")
# What a review of a candidate is told beside it: the host's own look at the repository.
SHOWN_REPOSITORY = ("  For this review it also states what the host itself saw of the\n"
                    "  repository when it prepared this directory: the branch, the commits of\n"
                    "  the task, the remotes, the remote-tracking refs and what is not\n"
                    "  committed. That section is the host's and not the builder's account.\n")
ANSWER = {"id", "resolved", "reason"}
REQUIREMENT = {"asked", "status", "where"}
STATUSES = ("met", "not_met", "not_checked")
DISCLOSED = "disclosed"  # asked for, not delivered, and the brief itself allows saying so instead: v3 only
NAMEABLE = ("not_met", DISCLOSED)  # what a finding may name: a requirement its own list says is not delivered
OUTPUT_LIMIT = 1_000_000  # characters of reviewer output the host will look through
ITEMS = 64
NONCE = re.compile(r"[0-9a-f]{16}\Z")
COMMIT = re.compile(r"([0-9a-f]{40}|[0-9a-f]{64})\Z")
NEWLINE = re.compile(r"\r\n|\n|\r")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def nonce():
    """A value for one run's marker lines. Text under review cannot know it, so an
    object quoted there is not taken for the reviewer's answer."""
    return secrets.token_hex(8)


def markers(value):
    if not isinstance(value, str) or not NONCE.fullmatch(value):
        raise Refused("a review marker is sixteen lower-case hexadecimal characters")
    return "=====BEGIN SORTIE REVIEW " + value + "=====", "=====END SORTIE REVIEW " + value + "====="


def target_checked(kind, target):
    """The reviewed thing in the shape receipts already use for it."""
    if kind not in KINDS:
        raise Refused("a review is of a plan or of a commit")
    if kind == "commit":
        good = (isinstance(target, dict) and set(target) == {"kind", "commit"} and target["kind"] == "commit"
                and isinstance(target["commit"], str) and COMMIT.fullmatch(target["commit"]))
    else:
        artifact = target.get("artifact") if isinstance(target, dict) else None
        good = (isinstance(target, dict) and set(target) == {"kind", "artifact"} and target["kind"] == "plan"
                and isinstance(artifact, dict) and set(artifact) == {"path", "sha256"}
                and isinstance(artifact["path"], str) and artifact["path"].strip() and len(artifact["path"]) <= 512
                and isinstance(artifact["sha256"], str) and SHA.fullmatch(artifact["sha256"]))
    if not good:
        raise Refused("a commit review names a full commit ID; a plan review names the plan's path and SHA256")
    return target


def schema_for(kind):
    """The schema the host asks of a review of this kind today."""
    if kind not in KINDS:
        raise Refused("a review is of a plan or of a commit")
    return SCHEMA_V3 if kind == "commit" else SCHEMA


def prior_checked(prior):
    """The earlier findings a repair round must answer: their ids and severities."""
    if not isinstance(prior, list) or len(prior) > ITEMS:
        raise Refused("earlier findings are a short list")
    for item in prior:
        channels.fields(item, {"id", "severity"})
        channels.token(item["id"])
        if item["severity"] not in SEVERITIES:
            raise Refused("unknown severity of an earlier finding")
    if len({item["id"] for item in prior}) != len(prior):
        raise Refused("earlier findings repeat an id")
    return prior


def unshown(kind, evidence):
    """The lines that stand where `evidence/` is described, for a review the host showed none."""
    said = ("The host did not show the builder's receipts to this review" + (
            ", nor its own statement of what it saw of the repository" if kind == "commit" else "")
            + ": " + NOT_SHOWN[evidence] + ". That is the "
            "host's doing and not the builder's, and no finding. It is a limit: name it under `not_examined`.")
    if kind == "commit":
        said += " Answer a requirement that rests on a receipt, a log or the state of the repository `not_checked`."
    return "\n".join(textwrap.wrap(said, 76, initial_indent="- ", subsequent_indent="  ",
                                   break_long_words=False, break_on_hyphens=False)) + "\n"


def instructions(kind, target, value, prior=(), evidence="shown"):
    """What the reviewer of one run is told, as one text.

    `evidence` is what the host did with the builder's receipts for this run, one
    of EVIDENCE: where it showed none the text says so and why, and describes no
    directory that is not there.

    The text names this run's two marker lines inside sentences and shows the
    example object without them, so the instructions printed back by a CLI
    hold no marker line and are never taken for an answer. The example is an
    object the host would take in this very round.
    """
    target = target_checked(kind, target)
    begin, end = markers(value)
    if not isinstance(prior, (list, tuple)):
        raise Refused("earlier findings are a short list")
    prior = prior_checked(list(prior))
    earlier_ids = {item["id"] for item in prior}
    new = next(name for name in ("F" + str(i) for i in range(1, ITEMS + 2)) if name not in earlier_ids)
    # In the order a reviewer writes it: the list stands before the findings that count into it.
    example = dict(schema=schema_for(kind), verdict="CHANGES", target=target)
    found = dict(id=new, severity="major", location="path/to/file.py:42" if kind == "commit" else "plan.md, step 3",
                 claim="One sentence: what is wrong.", evidence="What you read that shows it.",
                 recommendation="What would put it right.")
    if example["schema"] == SCHEMA_V3:
        example["requirements"] = [
            dict(asked="One thing the brief asks for, in a few of your own words.", status="met",
                 where="path/to/other.py:17"),
            dict(asked="Another thing it asks for.", status="not_met", where="path/to/file.py:42")]
        found["requirement"] = 2
    example.update(findings=[found],
                   prior=[dict(id=item["id"], resolved=True, reason="What you checked that shows it is resolved, or is not.")
                          for item in prior],
                   examined=["plan.md" if kind == "plan" else "change.patch", "tree/path/to/file.py"], not_examined=[])
    answers = earlier = ""
    if prior:
        names = ", ".join(item["id"] + " (" + item["severity"] + ")" for item in prior)
        answers = ("- `answers.json`: the earlier review's findings and what the builder says it did about each.\n"
                   "  A claim there is not evidence: check it against the " + ("plan" if kind == "plan" else "patch") + ".\n")
        earlier = ("This is a repair round. Under `prior`, answer every one of these earlier findings by its id\n"
                   "and no other: " + names + ". `resolved` is true only when you checked that it is, or\n"
                   "when the finding rested only on proof this directory cannot hold. Give new findings new ids.\n\n")
    if evidence not in EVIDENCE:
        raise Refused("what the host did with the evidence is one of " + ", ".join(EVIDENCE))
    text = files("sortie_kit").joinpath("data", "templates", "review-" + kind + ".md").read_text()
    for name, filled in (("$EVIDENCE\n", SHOWN + (SHOWN_REPOSITORY if kind == "commit" else "") if evidence == "shown"
                          else unshown(kind, evidence)),
                         ("$ANSWERS\n", answers), ("$PRIOR", earlier), ("$BEGIN", begin), ("$END", end),
                         ("$EXAMPLE", json.dumps(example, indent=2))):
        if text.count(name) != 1:
            raise Refused("packaged review instructions are damaged")
        text = text.replace(name, filled)
    return text


def unique(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("repeated key")
    return dict(pairs)


def constant(name):
    raise ValueError("not a JSON value: " + name)


def extract(text, value):
    """The one object between this run's marker lines."""
    begin, end = markers(value)
    if not isinstance(text, str) or len(text) > OUTPUT_LIMIT:
        raise Refused("the reviewer's output is too long to look through")
    # Lines end at a line feed or carriage return only: U+2028 and its kind may stand in a JSON string.
    lines = [line.strip() for line in NEWLINE.split(text)]
    starts = [i for i, line in enumerate(lines) if line == begin]
    ends = [i for i, line in enumerate(lines) if line == end]
    if len(starts) > 1 or len(ends) > 1:
        raise Refused("more than one review object between this run's markers")
    if not starts or not ends or ends[0] < starts[0]:
        raise Refused("no review object between this run's markers")
    body = [line for line in lines[starts[0] + 1:ends[0]] if line]
    if len(body) >= 2 and body[0].startswith("```") and body[0][3:].strip().lower() in ("", "json") and body[-1] == "```":
        body = body[1:-1]
    try:
        value = json.loads("\n".join(body), object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        raise Refused("what is between the markers is not one JSON object") from exc
    if not isinstance(value, dict):
        raise Refused("what is between the markers is not one JSON object")
    return value


def strings(value, limit, *, empty):
    return (isinstance(value, list) and len(value) <= ITEMS and (empty or value)
            and all(isinstance(item, str) and item.strip() and len(item) <= limit for item in value))


def short(words):
    """A reviewer's own words as they go into a reason that is kept and shown: one short printable line."""
    return " ".join("".join(c if c.isprintable() else " " for c in words).split())[:80]


def requirements_checked(value, disclosed=False):
    """What the brief asks for, each thing with whether the change does it and where that was seen.
    `disclosed` is a status of the third schema only."""
    statuses = STATUSES + ((DISCLOSED,) if disclosed else ())
    if not isinstance(value, list) or not value or len(value) > ITEMS:
        raise Refused("requirements is a list of at least one and at most " + str(ITEMS) + " things the brief asks for")
    for item in value:
        if not isinstance(item, dict) or set(item) != REQUIREMENT:
            raise Refused("a requirement has exactly `asked`, `status` and `where`")
        if not isinstance(item["asked"], str) or not item["asked"].strip() or len(item["asked"]) > 500:
            raise Refused("the `asked` of a requirement is nonblank text of at most 500 characters")
        if not isinstance(item["status"], str) or item["status"] not in statuses:
            raise Refused("the `status` of a requirement is `met`, `not_met`" + (
                ", `not_checked` or `disclosed`" if disclosed else " or `not_checked`"))
        where = item["where"]
        if not isinstance(where, str) or len(where) > 500 or (where and not where.strip()):
            raise Refused("the `where` of a requirement is text of at most 500 characters")
        if not where and item["status"] != "not_checked":
            raise Refused("the `where` of a requirement may be empty only when its status is `not_checked`")


def validate(record, target, prior, schema=None):
    """The verdict of a record that keeps the contract; refused otherwise.

    With `schema` named the record must be of that schema: the host that runs a
    review knows what it asked. With none the record's own decides, the earlier
    one or, for a commit, the one with the requirement list: that is a kept
    output read again, which may be from before the list.
    """
    asked(target, prior, schema)
    try:
        if not isinstance(record, dict):
            raise Refused("missing or unknown fields")
        # The schema is looked at before the fields, so an answer in another shape is told which one was asked.
        named = record.get("schema")
        if schema is not None and (not isinstance(named, str) or named != schema):
            raise Refused("the schema asked of this review is " + schema)
        if not isinstance(named, str) or named not in SCHEMAS or (named in LISTED and target["kind"] != "commit"):
            raise Refused("unknown schema, or the schema of a commit review on a plan")
        listed, naming = named in LISTED, named == SCHEMA_V3
        if listed and "requirements" not in record:
            raise Refused("a commit review answers the brief under `requirements`, which is missing")
        try:
            channels.fields(record, FIELDS | ({"requirements"} if listed else set()))
        except Refused:
            raise Refused("missing or unknown fields" + ("; this schema has no requirements list"
                                                         if not listed and "requirements" in record else "")) from None
        if listed:
            requirements_checked(record["requirements"], disclosed=naming)
        if record["verdict"] not in VERDICTS:
            raise Refused("unknown verdict")
        if not isinstance(record["findings"], list) or len(record["findings"]) > ITEMS:
            raise Refused("findings are a short list")
        for item in record["findings"]:
            if naming and (not isinstance(item, dict) or "requirement" not in item
                           or not (item["requirement"] is None or (type(item["requirement"]) is int
                                   and 1 <= item["requirement"] <= len(record["requirements"])))):
                raise Refused("each finding has `requirement`: the position in `requirements`, counted from 1, of "
                              "the thing it says is not delivered in some case, or null")
            channels.fields(item, FINDING | ({"requirement"} if naming else set()))
            channels.token(item["id"])
            if item["severity"] not in SEVERITIES:
                raise Refused("unknown severity")
            for key in ("location", "claim", "evidence", "recommendation"):
                channels.text(item[key], 500 if key == "location" else 2000)
        if not isinstance(record["prior"], list) or len(record["prior"]) > ITEMS:
            raise Refused("prior is a short list")
        for item in record["prior"]:
            channels.fields(item, ANSWER)
            channels.token(item["id"])
            channels.text(item["reason"], 2000)
            if type(item["resolved"]) is not bool:
                raise Refused("resolved is true or false")
        if not strings(record["not_examined"], 500, empty=True):
            raise Refused("not_examined is a list of short texts")
        if not isinstance(record["examined"], list) or (record["examined"] and not strings(record["examined"], 500, empty=False)):
            raise Refused("examined is a list of short texts")
        ids = [item["id"] for item in record["findings"]]
        if len(set(ids)) != len(ids):
            raise Refused("a finding id is repeated")
    except Refused as exc:
        raise Refused("the object does not keep the review contract: " + str(exc)) from exc
    if not record["examined"]:
        raise Refused("the review says nothing of what was examined")
    if record["target"] != target:
        raise Refused("the review names another target than the one it was given")
    answered = [item["id"] for item in record["prior"]]
    expected = {item["id"]: item["severity"] for item in prior}
    if sorted(answered) != sorted(expected):
        raise Refused("the review must answer each earlier finding once and no other: "
                      + (", ".join(sorted(expected)) or "there are none"))
    if set(ids) & set(expected):
        raise Refused("a new finding reuses the id of an earlier one")
    unresolved = [item["id"] for item in record["prior"] if not item["resolved"]]
    for item in record["findings"]:
        # A finding that names a requirement says a case of it is not delivered. The same answer may not
        # also hold that requirement to be met, or not looked at: whatever the verdict, it is no review.
        position = item.get("requirement")
        if position is not None and record["requirements"][position - 1]["status"] not in NAMEABLE:
            need = record["requirements"][position - 1]
            raise Refused("finding " + item["id"] + " names requirement " + str(position) + " (" + short(need["asked"])
                          + "), which the list marks `" + need["status"] + "`: a finding names only what is "
                          "`not_met` or `disclosed`, and null when it is about something else")
    if record["verdict"] == "ACCEPT":
        for item in record.get("requirements", ()):
            if item["status"] == "not_met":
                raise Refused("ACCEPT with a requirement answered not_met (" + short(item["asked"]) + ") is not an accept")
        for item in record["findings"]:
            if item["severity"] in BLOCKING:
                raise Refused("ACCEPT with a " + item["severity"] + " finding (" + item["id"] + ") is not an accept")
        for identity in unresolved:
            if expected[identity] in BLOCKING:
                raise Refused("ACCEPT while earlier " + expected[identity] + " finding " + identity
                              + " is not resolved is not an accept")
    elif not record["findings"] and not unresolved:
        raise Refused("CHANGES names nothing to change: no finding and no unresolved earlier finding")
    return record["verdict"]


def asked(target, prior, schema=None):
    """What the host held for the run. A fault here is the caller's, not a failed review."""
    target_checked(target.get("kind") if isinstance(target, dict) else None, target)
    prior_checked(prior)
    if schema is not None and (not isinstance(schema, str) or schema not in SCHEMAS
                               or (schema in LISTED and target["kind"] != "commit")):
        raise Refused("a review is asked for by a schema this build knows, and the requirement list of a commit only")


def check(text, value, target, prior, schema=None):
    """What the host takes from one reviewer run's output.

    `status` is `complete` with a `verdict` and the `record`, or `failed` with
    the reason and neither. A failed review settles nothing about the work.
    A marker, target, list of earlier findings or schema that the host itself
    passed wrongly is refused outright: that is no review at all. `schema` is
    what the host asked of the run, `schema_for` of its kind; none is given
    only where a kept output is read again (see `validate`).
    """
    markers(value)
    asked(target, prior, schema)
    try:
        record = extract(text, value)
        verdict = validate(record, target, prior, schema)
    except Refused as exc:
        return dict(status="failed", verdict=None, reasons=[str(exc)[:300]], record=None)
    return dict(status="complete", verdict=verdict, reasons=[], record=record)
