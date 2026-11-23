"""The sortie command: start a shift in your terminal, or inspect one offline."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from . import __version__
from .bounds import duration
from .launcher import report_read, start
from .layout import Request


def finite(value):
    try:
        return duration(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def safe(text):
    """One value for the terminal, on one line: anything but printable ASCII, a line
    break included, appears as an escape. Line breaks are the caller's to add."""
    return "".join(c if " " <= c <= "~" else c.encode("unicode_escape").decode() for c in str(text))


def pairs(items, flag, roles):
    result = {}
    for item in items:
        role, sep, value = item.partition("=")
        if not sep or not value or role not in roles or role in result:
            raise ValueError("use " + flag + " ROLE=VALUE with ROLE one of " + ", ".join(roles) + ", each once")
        result[role] = value
    return result


def seat_choices(args):
    """Flags to choices; profiles decide what is valid and nothing is assumed.

    What the flags leave open is asked for in the developer's terminal, before
    anything is prepared. Without such a terminal, or without the trust file
    and key that cannot be asked for, the start ends listing what is missing.
    A trust file or key not named by a flag is taken from where `sortie trust
    init` writes it, when a file is there; args.trust and args.key are set.
    Who reviews is not asked: the start picks the reviewer unless
    `--agent reviewer=` names one. Returns (choices, seconds).
    """
    from . import guided, signing, trust_init
    seats = ("driver", "builder")
    # Without a flag, the files `sortie trust init` writes are used when they are there.
    found = []
    for name, usual in (("trust", trust_init.default_trust()), ("key", trust_init.default_key())):
        if getattr(args, name) is None and usual.is_file():
            setattr(args, name, usual)
            found.append(("trust file " if name == "trust" else "signing key ") + safe(usual))
    if found:
        print("Using the " + " and the ".join(found) + ".", file=sys.stderr)
    roles = seats + ("reviewer",)
    if any(item.startswith("review=") for item in args.family):
        raise ValueError("--family review= was replaced: the reviewer is a seat now, so say --family "
                         "reviewer=FAMILY, together with --agent reviewer=NAME[:MODEL]")
    given = dict(agents=pairs(args.agent, "--agent", roles), families=pairs(args.family, "--family", roles),
                 seconds=args.seconds, network=args.builder_network)
    lacking = [name for name, value in (
        ("--trust FILE (sortie trust init writes one; none at " + safe(trust_init.default_trust()) + ")", args.trust),
        ("--key FILE (your encrypted signing key; none at " + safe(trust_init.default_key()) + ")", args.key))
        if value is None]
    repo = args.repo.resolve()
    try:
        result = guided.complete(given, repo)
    except guided.Missing as missing:
        if lacking:
            raise guided.Missing(lacking + missing.names) from None
        try:
            fd = signing.terminal()
        except (OSError, ValueError) as exc:
            raise guided.Missing(missing.names, "no terminal to ask in (" + str(exc) + ")") from None
        try:
            # Looked for again by the start itself; here so that a start which cannot
            # go on, for want of any agent that can review, says so before it asks anything.
            from .launcher import no_reviewer, reviewers
            if "reviewer" not in given["agents"] and not reviewers(repo):
                raise no_reviewer(repo)
            os.write(fd, b"Some choices were not given. Answer here, or press Ctrl-C and pass them as flags.\n")
            return guided.complete(given, repo, ask=guided.terminal_asker(fd))
        finally:
            os.close(fd)
    if lacking:
        raise guided.Missing(lacking)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sortie")
    parser.add_argument("--version", action="version", version=__version__)
    subs = parser.add_subparsers(dest="command", required=True)
    launch = subs.add_parser("start", help="sign and run a bounded shift in this terminal: a driver, a builder and a reviewer")
    launch.add_argument("--repo", type=Path, required=True)
    launch.add_argument("--direction", type=Path, required=True)
    launch.add_argument("--for", dest="seconds", type=finite,
                        help="how long the shift may run, e.g. 45m or 4h; asked for when a start in a terminal lacks it")
    launch.add_argument("--prepare-only", action="store_true",
                        help="offline preparation only; does not sign or start an agent")
    launch.add_argument("--sign-only", action="store_true", help="sign in your own terminal; starts no agent")
    launch.add_argument("--trust", type=Path, help="protected trust JSON outside the target repo "
                                                   "(default ~/.config/sortie-kit/trust.json when it exists)")
    launch.add_argument("--key", type=Path, help="encrypted private key outside the target repo "
                                                 "(default ~/.config/sortie-kit/signing-key when it exists)")
    launch.add_argument("--agent", action="append", default=[], metavar="ROLE=NAME[:MODEL]",
                        help="agent profile and exact model for the driver or builder seat; signed. "
                             "reviewer=NAME[:MODEL] names the reviewer, which is otherwise picked for you")
    launch.add_argument("--review-limit", type=finite, metavar="DURATION",
                        help="longest one run of the reviewer may take, 30s to 1h (default 10m); signed")
    # Replaced by the reviewer seat. Kept as an argument so that it is refused with a line
    # that names the new spelling, and so that it is not read as the start of --review-limit.
    launch.add_argument("--review", metavar="MODEL", help=argparse.SUPPRESS)
    launch.add_argument("--family", action="append", default=[], metavar="ROLE=FAMILY",
                        help="model family for driver, builder or reviewer when the profile does not list the model")
    for old in ("--driver", "--builder", "--runtime", "--builder-model", "--builder-family",
                "--review-model", "--review-family"):
        launch.add_argument(old, action="append", dest="obsolete", help=argparse.SUPPRESS)
    launch.add_argument("--allow-same-family-review", action="store_true",
                        help="keep a reviewer of the builder's own model family that you named with --agent "
                             "reviewer= while an agent of another family is installed")
    launch.add_argument("--builder-network", action="store_true",
                        help="start the builder in its profile's form with the network on (the built-in codex "
                             "builder has none without this); signed and shown, and refused for a builder "
                             "profile that has no such form")
    launch.add_argument("--no-wake", action="store_true",
                        help="do not hold the machine awake against idle sleep while the shift runs")
    check = subs.add_parser("verify", help="verify signed scope without admitting or starting an agent")
    check.add_argument("--shift", type=Path, required=True)
    check.add_argument("--trust", type=Path, required=True)
    stop = subs.add_parser("stop", help="write a local withdrawal marker; contacts no agent")
    stop.add_argument("--shift", type=Path, required=True)
    instructions = subs.add_parser("instructions", help="print packaged seat instructions; no authority")
    instructions.add_argument("role", choices=("driver", "builder"))
    report = subs.add_parser("report", help="summarize retained local evidence; no execution or admission")
    report.add_argument("--shift", type=Path, required=True)
    report.add_argument("--for", dest="seconds", type=finite, required=True, metavar="DURATION",
                        help="how long this command may wait for the shift's own lock before giving up, "
                             "e.g. 30s; 5s at most is used. A limit on this command, not the length of the shift")
    report.add_argument("--previous", help="prior report JSON retained inside this shift")
    waiting = subs.add_parser("wait", help="block until the host answered and no builder turn runs; reads only")
    waiting.add_argument("--shift", type=Path, required=True)
    waiting.add_argument("--for", dest="seconds", type=finite, default=540.0,
                         help="longest wait; at most 540s and never past the signed deadline")
    trusting = subs.add_parser("trust", help="set up the trust file that names your signing key")
    trusting_subs = trusting.add_subparsers(dest="trust_command", required=True)
    trust_new = trusting_subs.add_parser("init", help="write the trust file from your public key; signs nothing")
    trust_new.add_argument("--key", type=Path,
                           help="your private key; KEY.pub beside it is read (default ~/.config/sortie-kit/signing-key)")
    trust_new.add_argument("--out", type=Path, help="file to create (default ~/.config/sortie-kit/trust.json)")
    trust_new.add_argument("--identity", default="developer", help="the one signer name the file trusts")
    queued = subs.add_parser("queue-check", help="publish a builder check request; does not execute it")
    queued.add_argument("--shift", type=Path, required=True)
    queued.add_argument("--request", required=True, help="spec path relative to the shift")
    receipt = subs.add_parser("receipt", help="inspect local task evidence; no delegation authority")
    receipt_subs = receipt.add_subparsers(dest="receipt_command", required=True)
    from .receipts import PHASES
    template = receipt_subs.add_parser("template", help="print a versioned example to fill in")
    template.add_argument("phase", choices=PHASES)
    template.add_argument("--expected", action="store_true", help="print caller-owned expected context")
    validate = receipt_subs.add_parser("validate", help="validate receipt against caller-owned context")
    validate.add_argument("--shift", type=Path, required=True)
    validate.add_argument("--receipt", required=True, help="path relative to the shift")
    validate.add_argument("--expected", required=True, help="separate context path relative to the shift")
    validate.add_argument("--record", action="store_true", help="atomically consume the phase slot")
    writing = receipt_subs.add_parser(
        "write", help="check a filled template kept in the shift and write it once as receipts/RECEIPT_ID.json")
    writing.add_argument("--shift", type=Path, required=True)
    writing.add_argument("--expected", required=True, help="the phase's expected context, relative to the shift")
    writing.add_argument("--from", dest="draft", required=True,
                         help="the filled template, relative to the shift (for example under logs/)")
    reviewing = subs.add_parser("review", help="the review contract: what a reviewer is told and what is taken "
                                               "from its answer; starts no reviewer")
    reviewing_subs = reviewing.add_subparsers(dest="review_command", required=True)
    told = reviewing_subs.add_parser("instructions", help="print what the reviewer of a plan or a commit is told")
    told.add_argument("kind", choices=("plan", "commit"))
    judged = reviewing_subs.add_parser("check", help="say what the host would take from a saved reviewer output")
    judged.add_argument("output", type=Path, help="the reviewer's saved output")
    judged.add_argument("--expected", type=Path, required=True,
                        help='JSON with "marker", "target" and "prior", as the host held them for that run')
    args = parser.parse_args(argv)
    if args.command == "review":
        from . import review
        from .layout import Refused
        if args.review_command == "instructions":
            target = (dict(kind="commit", commit="0" * 40) if args.kind == "commit"
                      else dict(kind="plan", artifact=dict(path="briefs/PLAN.md", sha256="0" * 64)))
            print(review.instructions(args.kind, target, "0" * 16), end="")
            return 0
        try:
            if args.expected.stat().st_size > 1_000_000:
                raise Refused("the expected file is too large")
            expected = json.loads(args.expected.read_text(encoding="utf-8"), object_pairs_hook=review.unique)
            if not isinstance(expected, dict) or set(expected) != {"marker", "target", "prior"}:
                raise Refused('the expected file holds exactly "marker", "target" and "prior"')
            review.markers(expected["marker"])
            review.asked(expected["target"], expected["prior"])
            # One character past the limit is enough to know it is too long; the rest is never read.
            with args.output.open(encoding="utf-8", errors="replace", newline="") as stream:
                text = stream.read(review.OUTPUT_LIMIT + 1)
        except (OSError, ValueError, RecursionError, Refused) as exc:
            print(json.dumps({"status": "unusable", "detail": "expected context or output not usable: " + safe(str(exc))}))
            return 2
        result = review.check(text, expected["marker"], expected["target"], expected["prior"])
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "complete" else 1
    if args.command == "report":
        from .report import finalize
        try:
            result = finalize(args.shift, args.seconds, previous=args.previous)
            print(json.dumps({"status": result["status"], "report": "report.md", "summary": "report.json"}))
            return 0
        except (OSError, ValueError) as exc:
            print(json.dumps({"status": "unavailable", "detail": str(exc)}))
            return 3
    if args.command == "wait":
        from . import shift_wait
        try:
            print(json.dumps(shift_wait.wait(args.shift, args.seconds), sort_keys=True))
            return 0
        except (OSError, ValueError) as exc:
            print(json.dumps({"status": "refused", "detail": str(exc)}))
            return 3
    if args.command == "trust":
        from . import trust_init
        try:
            key = args.key or trust_init.default_key()
            path = trust_init.init(key, args.out or trust_init.default_trust(), args.identity)
        except (OSError, ValueError) as exc:
            print("sortie: refused: " + safe(exc), file=sys.stderr)
            return 3
        import shlex
        # Both as full paths: the hint is copied into a start run from another directory.
        used = [str(path), os.path.abspath(os.path.expanduser(key))]
        print("Trust file written: " + safe(path) + " (identity " + safe(args.identity) + ", public key from "
              + safe(str(key) + ".pub") + ").")
        if path == trust_init.default_trust() and Path(key) == trust_init.default_key():
            print("sortie start finds both there; no --trust or --key flag is needed.")
        elif all(safe(text) == text for text in used):
            print("Start a shift with: --trust " + shlex.quote(used[0]) + " --key " + shlex.quote(used[1]))
        else:
            # An escaped path is not the path: copying it would name another file.
            print("These paths hold characters that are shown here with escapes, so they are not offered as "
                  "ready flags. Give the real paths to sortie start as its trust file and key.")
        return 0
    if args.command == "instructions":
        from importlib.resources import files
        data = files("sortie_kit").joinpath("data", "templates")
        if args.role == "builder":
            print(data.joinpath("tiger.md").read_text(), end="\n")
        print(data.joinpath(args.role + ".md").read_text(), end="")
        return 0
    if args.command == "queue-check":
        from .builder_verification import queue_check
        try:
            print(json.dumps(queue_check(args.shift, args.request), sort_keys=True))
            return 0
        except (OSError, ValueError) as exc:
            print(json.dumps({"status": "invalid", "detail": str(exc)}))
            return 3
    if args.command == "receipt":
        from .receipt_schema import asset
        from .receipts import validate_receipt
        try:
            if args.receipt_command == "template":
                prefix = "expected-" if args.expected else ""
                print(asset(prefix + args.phase + "-v3.json").decode(), end="")
                return 0
            if args.receipt_command == "write":
                from .receipts import write_receipt_from
                print(json.dumps(write_receipt_from(args.shift, args.draft, args.expected), sort_keys=True))
                return 0
            result = validate_receipt(args.shift, args.receipt, args.expected, record=args.record)
            print(json.dumps(result, sort_keys=True))
            return {"satisfied": 0, "blocked": 1, "duplicate": 4, "diagnostic": 5}[result["status"]]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(json.dumps({"status": "invalid", "satisfies_phase": False,
                              "recorded": False, "reasons": [str(exc)]}))
            return 3
    try:
        if args.command in ("verify", "stop"):
            from . import grants
            if args.command == "stop":
                grants.stop(args.shift)
                print("Withdrawal recorded; this does not prove agent cessation.")
            else:
                trust = grants.Trust(args.trust, grants.shift_root(args.shift))
                # Two facts, said apart: who signed these bytes, and whether the window is open.
                raw, _, _ = grants.verify(args.shift, trust, legacy=True, current=False)
                value = grants.decoded(raw)
                version, (current, when) = value["version"], grants.window(value, args.shift)
                print("Signature: verified. grant.json and its scope are as signed, and this shift was signed "
                      "by the key in the trust file (identity " + trust.identity + "). No admission and no agent started.")
                print("Window: " + when + "." + ("" if current else " Nothing is admitted under this grant now."))
                if version == 3:
                    print("This is a format-3 grant. It has no reviewer seat and this version "
                          "of sortie-kit cannot admit it; sign the shift again.")
                elif version != grants.FORMAT:
                    print("This is a format-" + str(version) + " grant. It does not pin "
                          "agent seats and cannot be admitted; sign a fresh shift.")
                return 0 if current else 4
            return 0
        if args.obsolete:
            raise ValueError("--driver, --builder, --runtime and the --builder-/--review- model flags were "
                             "replaced; use --agent ROLE=NAME:MODEL and --family ROLE=FAMILY, with ROLE one of "
                             "driver, builder and, to name the reviewer yourself, reviewer")
        if args.review is not None:
            raise ValueError("--review MODEL was replaced: the reviewer is a seat now and is picked for you. "
                             "To name it yourself use --agent reviewer=NAME[:MODEL]")
        if args.prepare_only and args.sign_only:
            raise ValueError("--prepare-only and --sign-only cannot be combined")
        live = not args.prepare_only and not args.sign_only
        if args.no_wake and not live:
            raise ValueError("--no-wake applies only to a start that runs the shift")
        choices, seconds = None, args.seconds
        given = (args.agent or args.family or args.allow_same_family_review or args.review_limit is not None
                 or args.builder_network)
        if args.sign_only or live:
            choices, seconds = seat_choices(args)
        elif given:
            raise ValueError("agent, model and review choices apply only when signing")
        elif seconds is None:
            from . import guided
            raise guided.Missing(["--for DURATION"])
        request = Request(args.repo, args.direction, seconds)
        held = {}
        if live:
            # Built here and taken only once the shift is admitted. Not signed: it is how
            # this host runs and gives no seat anything.
            from .wake import Wake
            held["wake"] = Wake(not args.no_wake)
        shift = start(request, prepare_only=args.prepare_only, sign_only=args.sign_only,
                      trust_path=args.trust, key=args.key, choices=choices,
                      same_family=args.allow_same_family_review, review_seconds=args.review_limit,
                      builder_network=args.builder_network, **held)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if getattr(exc, "names", None) is not None:  # guided.Missing: a usage error, nothing was written
            print("sortie: " + safe(exc), file=sys.stderr)
            return 2
        print("sortie: refused: " + safe(exc), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("sortie: interrupted", file=sys.stderr)
        return 130
    if live:
        shift, code, reason, seen, published, *more = shift
        report, again = shift / "report.md", "sortie report --shift " + str(shift) + " --for 30s"
        state, raw = report_read(shift)
        if state == "final" and not published:
            state = "preliminary"  # only this host's own final publication makes it the final one
        from .launcher import closing_lines
        # The reason is often the driver's own words and the driver line names what a
        # process did: both are shown as inert text. The first line break is there
        # because the driver may have left the cursor after its last output.
        said = closing_lines(raw if state == "final" else None, code, safe, more[0] if more else None, seen)
        print("\nShift ended: " + safe(reason) + "\n" + "\n".join(said)
              + "\nDriver process: " + safe(seen) + "\n" + {
            "final": "Report: " + str(report),
            "preliminary": "Preliminary report, from before the host finished stopping its processes: "
                           + str(report) + "\nThe final one was not written; run: " + again,
            "none": "No report was written; run: " + again}[state])
        return code
    print(("Signed (not admitted, no agent started): " if args.sign_only else
           "Prepared (unsigned, no agent started): ") + str(shift))
    return 0
