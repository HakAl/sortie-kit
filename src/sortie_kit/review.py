"""The review contract. Nothing here starts a reviewer.

A reviewer is any agent CLI. It is told what to do by one of two packaged
texts and must end its answer with one JSON object between two marker lines
that carry a value made for that run. The host takes a verdict only from an
object that parses, has exactly the fields below, names the target the host
asked about and does not contradict itself. Anything else is a failed review:
never an accept and never a request for changes.
"""
from __future__ import annotations

from importlib.resources import files
import json
import re
import secrets

from . import channels
from .layout import Refused

SCHEMA = "sortie-kit.review.v1"
KINDS = ("plan", "commit")
VERDICTS = ("ACCEPT", "CHANGES")
SEVERITIES = ("blocker", "major", "minor")
BLOCKING = ("blocker", "major")
FIELDS = {"schema", "verdict", "target", "findings", "prior", "examined", "not_examined"}
FINDING = {"id", "severity", "location", "claim", "evidence", "recommendation"}
ANSWER = {"id", "resolved", "reason"}
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


def instructions(kind, target, value, prior=()):
    """What the reviewer of one run is told, as one text.

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
    example = dict(schema=SCHEMA, verdict="CHANGES", target=target, findings=[dict(
        id=new, severity="major", location="path/to/file.py:42" if kind == "commit" else "plan.md, step 3",
        claim="One sentence: what is wrong.", evidence="What you read that shows it.",
        recommendation="What would put it right.")],
        prior=[dict(id=item["id"], resolved=True, reason="What you checked that shows it is resolved, or is not.")
               for item in prior],
        examined=["plan.md" if kind == "plan" else "change.patch", "tree/path/to/file.py"], not_examined=[])
    answers = earlier = ""
    if prior:
        names = ", ".join(item["id"] + " (" + item["severity"] + ")" for item in prior)
        answers = ("- `answers.json`: the earlier review's findings and what the builder says it did about each.\n"
                   "  A claim there is not evidence: check it against the " + ("plan" if kind == "plan" else "patch") + ".\n")
        earlier = ("This is a repair round. Under `prior`, answer every one of these earlier findings by its id\n"
                   "and no other: " + names + ". `resolved` is true only when you checked that it is. Give\n"
                   "new findings new ids.\n\n")
    text = files("sortie_kit").joinpath("data", "templates", "review-" + kind + ".md").read_text()
    for name, filled in (("$ANSWERS\n", answers), ("$PRIOR", earlier), ("$BEGIN", begin), ("$END", end),
                         ("$EXAMPLE", json.dumps(example, indent=2, sort_keys=True))):
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


def validate(record, target, prior):
    """The verdict of a record that keeps the contract; refused otherwise."""
    asked(target, prior)
    try:
        channels.fields(record, FIELDS)
        if record["schema"] != SCHEMA or record["verdict"] not in VERDICTS:
            raise Refused("unknown schema or verdict")
        if not isinstance(record["findings"], list) or len(record["findings"]) > ITEMS:
            raise Refused("findings are a short list")
        for item in record["findings"]:
            channels.fields(item, FINDING)
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
    if record["verdict"] == "ACCEPT":
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


def asked(target, prior):
    """What the host held for the run. A fault here is the caller's, not a failed review."""
    target_checked(target.get("kind") if isinstance(target, dict) else None, target)
    prior_checked(prior)


def check(text, value, target, prior):
    """What the host takes from one reviewer run's output.

    `status` is `complete` with a `verdict` and the `record`, or `failed` with
    the reason and neither. A failed review settles nothing about the work.
    A marker, target or list of earlier findings that the host itself passed
    wrongly is refused outright: that is no review at all.
    """
    markers(value)
    asked(target, prior)
    try:
        record = extract(text, value)
        verdict = validate(record, target, prior)
    except Refused as exc:
        return dict(status="failed", verdict=None, reasons=[str(exc)[:300]], record=None)
    return dict(status="complete", verdict=verdict, reasons=[], record=record)
