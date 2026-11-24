"""Ask in the terminal for whatever a start still lacks. Nothing is guessed.

The questions follow one fixed order: driver agent and model, builder agent
and model, duration; a model its profile does not place in a family is
followed by a question for that family. Choices offered come from the
installed agent profiles. With nobody to ask, the same pass lists what is
missing instead. No answer is a secret and nothing here starts an agent.

Who reviews is never asked. The start picks the reviewer and shows it in what
is signed; a reviewer named with `--agent reviewer=` is checked here and
passed on.
"""
from __future__ import annotations

import os

from . import agents
from .bounds import duration
from .grants import MODEL, family as canonical_family
from .layout import Refused

ROLES = ("driver", "builder")
LINE_LIMIT = 256
TRIES = 3
# One model ID per family, to show what an answer looks like where a profile lists none.
EXAMPLES = {"gpt": "gpt-6.1-sol", "claude": "claude-sonnet-5-5", "gemini": "gemini-4-carbon-high"}


class Missing(Refused):
    """Choices a start needs, with nobody at a terminal to give them."""
    def __init__(self, names, why=""):
        super().__init__("missing: " + "; ".join(names) + ("; " + why if why else ""))
        self.names = list(names)


def model_checked(text):
    if not MODEL.fullmatch(text):
        raise ValueError("that is not a model ID")
    return text


def seat_profile(name, role, repo, home, network=False, model=None):
    """`network`: the start asked for the builder's form with the network on, so a
    builder profile without one cannot take the seat, nor one whose two forms are
    the same command in this repository, or with `model` once that is known."""
    profile = agents.load(name, repo, home=home)
    mode = agents.MODES[role]
    if profile["value"][mode + "_argv"] is None:
        raise Refused("agent " + name + " has no " + mode + " start and cannot be the " + role)
    if network and role == "builder":
        agents.network_argv(profile, model or profile["value"].get("default_model"), repo)
    return profile


def offered(role, repo, home, network=False):
    """Installed profiles that can take this seat. One that does not load is left out
    here and refuses with its reason if it is named."""
    names = []
    for name in agents.available(repo, home=home):
        try:
            seat_profile(name, role, repo, home, network)
        except (OSError, ValueError):
            continue
        names.append(name)
    return names


def unplaced(profile, model):
    """True when neither the profile's list nor its family for other models covers this model."""
    value = profile["value"]
    return model not in value["models"] and value.get("unlisted_family") is None


def complete(given, repo, *, ask=None, home=None):
    """Fill what the flags left open. `given` holds agents ({role: "NAME[:MODEL]"}),
    families ({role: FAMILY}) and seconds; a role is driver, builder or reviewer.

    Returns (choices, seconds) in the shape the launcher takes. `choices` has
    a reviewer only when one was named by flag: nothing about the reviewer is
    ever asked. With `ask` None, raises Missing naming every choice that could
    be determined to lack; a question that depends on a missing answer is not
    listed. `given` may hold `network` (the `--builder-network` flag): it is never
    asked for, and with it a builder whose profile has no form with the network
    on is refused, or not offered, before any question.
    """
    lacking = []
    network = bool(given.get("network"))

    def need(flag, label, options, check, hint=""):
        if ask is None:
            lacking.append(flag)
            return None
        return ask(label, options, check, hint)

    # The reviewer: taken from the flag when there is one, checked before anything is
    # asked, and never asked for.
    reviewer = None
    name, _, model = given["agents"].get("reviewer", "").partition(":")
    stated = given["families"].get("reviewer")
    if model and not MODEL.fullmatch(model):
        raise Refused("invalid model ID for the reviewer")
    if name:
        profile = agents.load(name, repo, home=home)
        if not agents.can_review(profile):
            raise Refused("agent " + name + " has no review start and cannot be the reviewer")
        if stated is None and unplaced(profile, model or profile["value"]["review_model"]):
            raise Refused("model " + (model or profile["value"]["review_model"]) + " is not in the " + name
                          + " profile; say who makes it with --family reviewer=FAMILY")
        if stated is not None:  # refused here, before any question, when it contradicts the profile
            agents.choose_model(profile, model or profile["value"]["review_model"], stated)
        reviewer = {"agent": name, "model": model or None,
                    "family": canonical_family(stated) if stated is not None else None}
    elif stated is not None:
        raise Refused("--family reviewer= goes with --agent reviewer=NAME[:MODEL]; a picked reviewer takes "
                      "its family from its profile")

    # A builder named by flag that cannot take the network form is refused here, before
    # a question about any other seat.
    name, _, model = given["agents"].get("builder", "").partition(":")
    if network and name:
        if model and not MODEL.fullmatch(model):
            raise Refused("invalid model ID for the builder")
        seat_profile(name, "builder", repo, home, network, model or None)

    choices = {}
    for role in ROLES:
        title = role.capitalize()
        name, _, model = given["agents"].get(role, "").partition(":")
        if model and not MODEL.fullmatch(model):
            raise Refused("invalid model ID for the " + role)  # before any message repeats it
        stated = given["families"].get(role)
        if not name:
            def usable(text, role=role):
                seat_profile(text, role, repo, home, network)
                return text
            name = need("--agent " + role + "=NAME:MODEL", title + " agent", offered(role, repo, home, network), usable,
                        "an installed agent profile")
            if name is None:
                continue
        profile = seat_profile(name, role, repo, home, network, model or None)
        model = model or profile["value"].get("default_model")
        if not model:
            listed = list(profile["value"]["models"])
            hint = "the exact model ID the " + name + " CLI is started with"
            if not listed:
                # Nothing to pick from: say what to type. The example shows the shape of an
                # ID only; it is not checked and may not be a model the developer can use.
                example = EXAMPLES.get(profile["value"].get("unlisted_family"))
                hint = ("the " + name[:40] + " profile lists no models: type the model ID you give its CLI"
                        + (", for example " + example if example else ""))
            model = need("--agent " + role + "=" + name + ":MODEL (no model named)", title + " model",
                         listed, model_checked, hint)
            if model is None:
                continue
            if network and role == "builder":
                agents.network_argv(profile, model, repo)  # with the model just named in its place
        if stated is None and unplaced(profile, model):
            stated = need("--family " + role + "=FAMILY (model " + model + " is not in the " + name + " profile)",
                          title + " model family", [], canonical_family,
                          "who makes " + model + ", for example gpt, claude or gemini")
        choices[role] = {"agent": name, "model": model, "family": stated}

    if reviewer is not None:
        choices["reviewer"] = reviewer
    seconds = given["seconds"]
    if seconds is None:
        seconds = need("--for DURATION", "How long may the shift run", [], duration, "for example 45m or 2h")
    if lacking:
        raise Missing(lacking)
    return choices, seconds


def plain(text):
    """Printable ASCII only, so nothing typed or read from a profile can drive the terminal."""
    return "".join(c if " " <= c <= "~" else "?" for c in str(text))[:200]


def asker(readline, write):
    """Questions over two functions: `readline()` gives one line or None at the
    end of input, `write(text)` shows text. A number picks a listed option."""
    def ask(label, options, check, hint=""):
        notes = "; ".join(note for note in (hint, "a number picks from the list" if options else "") if note)
        write("\n" + plain(label) + (" (" + plain(notes) + ")" if notes else "") + "\n" + "".join(
            "  " + str(n) + ". " + plain(option) + "\n" for n, option in enumerate(options, 1)))
        for _ in range(TRIES):
            write("  " + plain(label) + ": ")
            line = readline()
            if line is None:
                raise Refused("no answer for: " + label)
            text = line.strip()
            if options and text.isascii() and text.isdigit() and 1 <= int(text) <= len(options):
                text = options[int(text) - 1]
            try:
                if not text:
                    raise ValueError("nothing was typed")
                return check(text)
            except (OSError, ValueError) as exc:
                write("  " + plain(exc) + "\n")
        raise Refused("no usable answer for: " + label)
    return ask


def terminal_asker(fd):
    """Questions on an open terminal descriptor. Lines are read a byte at a time,
    so nothing typed ahead for a later reader (the signer's passphrase prompt)
    is taken from the terminal."""
    def readline():
        raw = b""
        while not raw.endswith(b"\n"):
            byte = os.read(fd, 1)
            if not byte:
                return None
            raw += byte
            if len(raw) > 16 * LINE_LIMIT:
                raise Refused("answer too long")
        return raw.decode("utf-8", "replace") if len(raw) <= LINE_LIMIT else "\x00"

    def write(text):
        os.write(fd, text.encode())
    return asker(readline, write)
