import contextlib
from concurrent.futures import ThreadPoolExecutor
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from unittest.mock import patch

from sortie_kit import cli, receipts
from sortie_kit.bounds import Sources
from sortie_kit.layout import Refused, Request, prepare
from sortie_kit.receipt_schema import asset, decode, shape, validate_shape, check_schema
from support import LocalTest


class ReceiptTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.target = self.repo()
        (self.target / "source.txt").write_text("before\n")
        self.base = self.commit()
        (self.target / "source.txt").write_text("after\n")
        self.candidate = self.commit()
        self.shift = prepare(Request(self.target, self.target / "direction.md", 60), Sources())

    def commit(self):
        subprocess.run(["git", "-C", str(self.target), "add", "."], check=True, capture_output=True, timeout=5)
        subprocess.run(["git", "-C", str(self.target), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
                       check=True, capture_output=True, timeout=5)
        return subprocess.check_output(["git", "-C", str(self.target), "rev-parse", "HEAD"], timeout=5).decode().strip()

    def artifact(self, path, text="fixture evidence\n"):
        p = self.shift / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return {"path": path, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}

    NONCE = "4f2a9c0d17e84b6a"

    def said(self, record, nonce=None):
        """A reviewer's output that carries this record between the run's markers."""
        from sortie_kit import review
        begin, end = review.markers(nonce or self.NONCE)
        return "I read the patch.\n" + begin + "\n" + json.dumps(record, indent=1) + "\n" + end + "\n"

    def record(self, target, verdict="ACCEPT", findings=(), prior=()):
        return dict(schema="sortie-kit.review.v1", verdict=verdict, target=target, findings=list(findings),
                    prior=list(prior), examined=["change.patch"], not_examined=[])

    def finding(self, identity="F1", severity="major"):
        return dict(id=identity, severity=severity, location="source.txt:1", claim="wrong word", evidence="line 1",
                    recommendation="say after")

    def case(self, phase="review", record=None, prior=()):
        value = decode(asset(phase + "-v3.json"))
        expected = decode(asset("expected-" + phase + "-v3.json"))
        for obj in (value, expected):
            obj["shift_id"] = self.shift.name
            obj["brief"] = self.artifact("briefs/task.md")
        data = value["data"]
        if phase == "plan":
            data["plan"] = self.artifact("briefs/plan.md")
            expected["target"]["artifact"] = data["plan"]
        elif phase == "build":
            data["commit"] = expected["target"]["commit"] = self.candidate
            data["checks"][0]["output"] = self.artifact("logs/tests.txt")
        elif phase == "review":
            expected["target"]["commit"] = data["target"]["commit"] = self.candidate
            expected["required_findings"] = [item["id"] for item in prior]
            record = record or self.record(data["target"])
            attempt = data["attempts"][0]
            attempt.update(marker=self.NONCE, record=record, verdict=record["verdict"],
                           stdout=self.artifact("logs/review.out", self.said(record)),
                           stderr=self.artifact("logs/review.err", ""))
            data.update(outcome=record["verdict"], prior=list(prior), base=self.base)
        else:
            data["evidence"] = [self.artifact("logs/event.txt")]
            if phase == "stop":
                data["status"] = "stopped"
        return value, expected

    def store(self, value, expected):
        (self.shift / "expected.json").write_bytes(receipts.encoded(expected))
        path = "receipts/" + value["receipt_id"] + ".json"
        (self.shift / path).write_bytes(receipts.encoded(value))
        return path

    def validate(self, value, expected, **kwargs):
        return receipts.validate_receipt(self.shift, self.store(value, expected), "expected.json", **kwargs)

    def assertDiagnostic(self, result):
        self.assertEqual(result["status"], "diagnostic")
        self.assertFalse(result["satisfies_phase"])
        self.assertFalse(result["recorded"])

    def test_all_templates_and_five_phases(self):
        for phase in receipts.PHASES:
            with self.subTest(phase=phase):
                validate_shape(decode(asset(phase + "-v3.json")), "receipt")
                validate_shape(decode(asset("expected-" + phase + "-v3.json")), "expected")
                value, expected = self.case(phase)
                result = self.validate(value, expected)
                self.assertDiagnostic(result)

    def test_every_common_binding_and_expected_shift(self):
        for key, wrong in (("shift_id", "other"), ("task_id", "other"), ("request_id", "other"),
                           ("role", "builder"), ("phase", "build"), ("round", 1),
                           ("brief", {"path": "briefs/other.md", "sha256": "0" * 64})):
            with self.subTest(key=key):
                value, expected = self.case()
                value[key] = wrong
                with self.assertRaises(Refused):
                    self.validate(value, expected, record=True)
                self.assertFalse((self.shift / "receipts/.consumed.json").exists())
        value, expected = self.case()
        value["shift_id"] = expected["shift_id"] = "other"
        with self.assertRaisesRegex(Refused, "directory"):
            self.validate(value, expected)

    def test_strict_shape_and_json_wire(self):
        for change in ({"version": True}, {"round": False}, {"round": 4}, {"unknown": 1}, {"role": "builder"}):
            value, expected = self.case()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(Refused):
                self.validate(value, expected)
        for raw in (b'{"version":1,"version":1}', b'{"version":', b'{"n":NaN}', b'{"n":1.0}'):
            with self.subTest(raw=raw), self.assertRaises(Refused):
                decode(raw)
        with self.assertRaises(Refused):
            check_schema({"unsupported": True})

    def test_phase_candidate_plan_and_commit_existence(self):
        for phase in ("build", "review"):
            value, expected = self.case(phase)
            expected["target"]["commit"] = self.base
            with self.subTest(phase=phase), self.assertRaises(Refused):
                self.validate(value, expected)
        value, expected = self.case("build")
        value["data"]["commit"] = expected["target"]["commit"] = "0" * 40
        with self.assertRaises(Refused):
            self.validate(value, expected)
        value, expected = self.case("plan")
        expected["target"]["artifact"]["sha256"] = "0" * 64
        with self.assertRaises(Refused):
            self.validate(value, expected)

    def test_missing_changed_and_unsafe_artifacts(self):
        value, expected = self.case()
        ref = value["data"]["attempts"][0]["stdout"]
        for bad in ("/etc/passwd", "../outside", "logs/../outside", "logs//review.out", "logs\\file", "logs/\x00"):
            with self.subTest(path=bad):
                ref["path"] = bad
                with self.assertRaises(Refused):
                    self.validate(value, expected)
        ref["path"] = "logs/missing"
        with self.assertRaises(Refused):
            self.validate(value, expected)
        ref["path"] = "logs/review.out"
        (self.shift / ref["path"]).write_text("changed")
        with self.assertRaisesRegex(Refused, "hash"):
            self.validate(value, expected)

    def test_symlink_and_special_file_reads_are_refused_without_hanging(self):
        value, expected = self.case()
        ref = value["data"]["attempts"][0]["stdout"]
        outside = self.root / "outside"
        outside.write_text("private")
        (self.shift / "logs/link").symlink_to(outside)
        (self.shift / "linked-dir").symlink_to(self.root, target_is_directory=True)
        os.mkfifo(self.shift / "logs/pipe")
        for path in ("logs/link", "linked-dir/outside", "logs/pipe", "logs"):
            ref["path"] = path
            with self.subTest(path=path), self.assertRaises(Refused):
                self.validate(value, expected)

    def test_bounded_reads_and_deadline(self):
        value, expected = self.case()
        with patch.object(receipts, "TOTAL_LIMIT", 10), self.assertRaisesRegex(Refused, "limit"):
            self.validate(value, expected)
        with self.assertRaises(Refused):
            self.validate(value, expected, seconds=0)
        ref = value["data"]["attempts"][0]["stdout"]
        with (self.shift / ref["path"]).open("wb") as stream:
            stream.truncate(receipts.ARTIFACT_LIMIT + 1)
        with self.assertRaisesRegex(Refused, "bounded"):
            self.validate(value, expected)

    def test_a_review_receipt_rests_on_the_kept_output_and_the_signed_reviewer(self):
        self.assertDiagnostic(self.validate(*self.case()))
        mutations = [lambda d: d["reviewer"].update(model="other"),
                     lambda d: d["reviewer"].update(agent="another"),
                     lambda d: d["reviewer"].update(family="another-family"),
                     lambda d: d["reviewer"].update(choice="given"),
                     lambda d: d.update(implementer_family="another-family"),
                     lambda d: d.update(base="0" * 40),
                     lambda d: d.update(outcome="CHANGES"),
                     lambda d: d.update(outcome="FAILED"),
                     lambda d: d["attempts"][0].update(verdict="CHANGES"),
                     lambda d: d["attempts"][0].update(marker="0" * 16),
                     lambda d: d["attempts"][0].update(marker=None),
                     lambda d: d["attempts"][0].update(stdout=None),
                     lambda d: d["attempts"][0].update(repository="changed"),
                     lambda d: d["attempts"][0].update(repository="not checked"),
                     lambda d: d["attempts"][0].update(attempt=2),
                     lambda d: d["attempts"][0]["record"].update(examined=["something else"]),
                     lambda d: d["attempts"][0].update(status="failed"),
                     lambda d: d["attempts"].append(copy.deepcopy(d["attempts"][0])),
                     lambda d: d.update(prior=[dict(id="F1", severity="major")])]
        for index, mutate in enumerate(mutations):
            value, expected = self.case()
            mutate(value["data"])
            with self.subTest(mutation=index), self.assertRaises(Refused):
                self.validate(value, expected)
        # The output file decides: one that says something else than the receipt is refused.
        value, expected = self.case()
        other = self.record(value["data"]["target"], "CHANGES", [self.finding()])
        value["data"]["attempts"][0]["stdout"] = self.artifact("logs/review.out", self.said(other))
        with self.assertRaisesRegex(Refused, "kept review output"):
            self.validate(value, expected)
        # A seat cannot hand one in through the receipt writer.
        value, expected = self.case()
        (self.shift / "expected.json").write_bytes(receipts.encoded(expected))
        with self.assertRaisesRegex(Refused, "written by the host"):
            receipts.write_receipt_atomic(self.shift, value, "expected.json")
        value["receipt_id"] = "the-hosts-own"
        with receipts.session(self.shift, 10) as view:
            self.assertEqual(receipts.written(view, value, "expected.json", host=True)["status"], "written")

    def test_same_family_review_is_what_the_signed_reviewer_says(self):
        value, expected = self.case()
        family = expected["implementer_family"]
        for side in (expected["review"], value["data"]["reviewer"]):
            side["family"] = family
        with self.assertRaisesRegex(Refused, "contradicts"):
            self.validate(value, expected)
        for side in (expected["review"], value["data"]["reviewer"]):
            side.update(same_family=True, choice="picked-same-family")
        self.assertDiagnostic(self.validate(value, expected))
        # A trailing newline must not make one family look like two.
        expected["implementer_family"] = family + "\n"
        with self.assertRaisesRegex(Refused, "invalid string format|supported variant"):
            self.validate(value, expected)
        value, expected = self.case()
        for side in (expected["review"], value["data"]["reviewer"]):
            side["same_family"] = True
        with self.assertRaisesRegex(Refused, "contradicts"):
            self.validate(value, expected)

    def test_reviewer_comes_from_expected_context_not_a_compiled_model(self):
        value, expected = self.case()
        expected["review"].update(model="operator/chosen-model@2", family="operator-family")
        with self.assertRaisesRegex(Refused, "signed reviewer"):
            self.validate(value, expected)
        value["data"]["reviewer"].update(model="operator/chosen-model@2", family="operator-family")
        self.assertDiagnostic(self.validate(value, expected))
        for name in ("review", "implementer_family"):
            value, expected = self.case()
            expected[name] = None
            with self.subTest(name=name), self.assertRaisesRegex(Refused, "must bind"):
                self.validate(value, expected)
        value, expected = self.case("build")
        expected["review"] = self.case()[1]["review"]
        with self.assertRaisesRegex(Refused, "another phase"):
            self.validate(value, expected)
        value, expected = self.case("build")
        value["role"] = expected["role"] = "reviewer"
        with self.assertRaises(Refused):
            self.validate(value, expected)
        value, expected = self.case()
        expected["role"] = "builder"
        with self.assertRaises(Refused):
            self.validate(value, expected)
        for old in (1, 2):
            value, expected = self.case()
            value["version"] = expected["version"] = old
            with self.subTest(version=old), self.assertRaisesRegex(Refused, "unsupported format version"):
                self.validate(value, expected)
        for mutate in (lambda v, e: e["review"].update(model=e["review"]["model"] + "\n"),
                       lambda v, e: e["target"].update(commit=e["target"]["commit"] + "\n")):
            value, expected = self.case()
            mutate(value, expected)
            with self.subTest(mutate=mutate), self.assertRaisesRegex(Refused, "invalid string format|supported variant"):
                self.validate(value, expected)
        from sortie_kit.receipt_schema import shape
        text = lambda pattern: {"type": "string", "pattern": pattern}
        shape("cost$", text("cost\\$"))          # an escaped dollar stays a literal
        shape("a\\", text("^a\\\\$"))            # escaped backslash, then a real anchor
        for value, pattern in (("cost$\n", "^cost\\$$"), ("a\\\n", "^a\\\\$"), ("a\n", "^a$")):
            with self.subTest(pattern=pattern), self.assertRaises(Refused):
                shape(value, text(pattern))
        source = Path(receipts.__file__).read_text()
        self.assertNotIn("gemini", source.casefold())
        self.assertNotIn("agy", source.casefold())

    def test_a_plan_review_and_a_repair_round_that_answers_the_earlier_findings(self):
        plan = {"kind": "plan", "artifact": self.artifact("briefs/plan.md")}
        value, expected = self.case(record=self.record(plan))
        expected["target"] = value["data"]["target"] = plan
        value["data"]["base"] = None
        self.assertDiagnostic(self.validate(value, expected))
        prior = [dict(id="F1", severity="major"), dict(id="F2", severity="minor")]
        target = dict(kind="commit", commit=self.candidate)
        answered = [dict(id="F1", resolved=True, reason="says after now"), dict(id="F2", resolved=False, reason="still there")]
        value, expected = self.case(record=self.record(target, prior=answered), prior=prior)
        self.assertDiagnostic(self.validate(value, expected))  # an unresolved minor does not stand in the way
        expected["required_findings"] = ["F1"]
        with self.assertRaisesRegex(Refused, "earlier findings"):
            self.validate(value, expected)
        # An ACCEPT that leaves an earlier major unresolved is no accept: the host records it as failed.
        answered[0]["resolved"] = False
        value, expected = self.case(record=self.record(target, prior=answered), prior=prior)
        with self.assertRaisesRegex(Refused, "kept review output"):
            self.validate(value, expected)

    def test_a_failed_review_and_one_that_asks_for_changes_stay_blocked(self):
        value, expected = self.case(record=self.record(dict(kind="commit", commit=self.candidate), "CHANGES", [self.finding()]))
        result = self.validate(value, expected)
        self.assertEqual((result["status"], result["reasons"]), ("blocked", ["review requests changes"]))
        value, expected = self.case()
        failed = dict(value["data"]["attempts"][0], status="failed", verdict=None, record=None,
                      reasons=["no review object between this run's markers"],
                      stdout=self.artifact("logs/review-1.out", "I could not finish.\n"))
        # A failed first attempt does not weaken the accept of the second.
        value["data"]["attempts"] = [failed, dict(value["data"]["attempts"][0], attempt=2)]
        self.assertDiagnostic(self.validate(value, expected))
        value["data"]["attempts"] = [failed, dict(failed, attempt=2, stdout=None, stderr=None, exit_status=None,
                                                  ended_by="not started: no such file", marker=None, directory=None,
                                                  instructions_sha256=None, argv=[], repository="not checked")]
        value["data"]["outcome"] = "FAILED"
        result = self.validate(value, expected)
        self.assertEqual((result["status"], result["reasons"]), ("blocked", ["review failed"]))
        for mutate in (lambda a: a.update(reasons=[]), lambda a: a.update(verdict="ACCEPT"),
                       lambda a: a.update(record=dict(schema="x"))):
            changed = copy.deepcopy(value)
            mutate(changed["data"]["attempts"][1])
            with self.subTest(mutate=mutate), self.assertRaises(Refused):
                self.validate(changed, expected)

    def test_a_plan_or_build_receipt_answers_each_finding_it_was_handed(self):
        for phase in ("plan", "build"):
            with self.subTest(phase=phase):
                value, expected = self.case(phase)
                expected["required_findings"] = ["F1", "F2"]
                with self.assertRaisesRegex(Refused, "answer each finding"):
                    self.validate(value, expected)
                value["data"]["answers"] = [
                    dict(id="F1", disposition="fixed", reason="pads to the width now",
                         evidence=self.artifact("logs/reproduction.txt", "fails before, passes after\n")),
                    dict(id="F2", disposition="rejected", reason="the brief asks for exactly this", evidence=None)]
                self.assertDiagnostic(self.validate(value, expected))
                value["data"]["answers"][0]["evidence"]["sha256"] = "0" * 64
                with self.assertRaisesRegex(Refused, "hash"):
                    self.validate(value, expected)
                value["data"]["answers"][0]["evidence"] = None
                value["data"]["answers"].append(dict(id="F3", disposition="rejected", reason="not asked", evidence=None))
                with self.assertRaisesRegex(Refused, "answer each finding"):
                    self.validate(value, expected)
                value["data"]["answers"][2]["id"] = "F2"
                with self.assertRaisesRegex(Refused, "twice"):
                    self.validate(value, expected)
                value["data"]["answers"] = value["data"]["answers"][:2]
                value["data"]["answers"][1]["disposition"] = "open"
                with self.assertRaises(Refused):
                    self.validate(value, expected)
        for phase in ("acknowledgment", "stop"):
            value, expected = self.case(phase)
            expected["required_findings"] = ["F1"]
            with self.subTest(phase=phase), self.assertRaisesRegex(Refused, "findings to answer"):
                self.validate(value, expected)

    def test_build_failure_and_unknown_cessation_do_not_satisfy(self):
        value, expected = self.case("build")
        value["data"]["checks"][0]["exit_status"] = 1
        self.assertEqual(self.validate(value, expected)["status"], "blocked")
        value["data"]["checks"] = []
        with self.assertRaises(Refused):
            self.validate(value, expected)
        value["data"]["no_checks_reason"] = "Documentation-only task."
        self.assertDiagnostic(self.validate(value, expected))
        value, expected = self.case("stop")
        value["data"]["status"] = "unknown"
        self.assertEqual(self.validate(value, expected)["status"], "blocked")
        value["data"]["status"] = "stopped"
        value["data"]["scope"] = "toolkit"
        with self.assertRaisesRegex(Refused, "stop scope"):
            self.validate(value, expected)

    def test_recording_is_atomic_and_replay_or_conflict_cannot_advance(self):
        value, expected = self.case()
        self.assertTrue(self.validate(value, expected, record=True)["recorded"])
        for record in (False, True):
            result = self.validate(value, expected, record=record)
            self.assertEqual(result["status"], "duplicate")
            self.assertFalse(result["satisfies_phase"])
        value["receipt_id"] = "replacement"
        self.assertEqual(self.validate(value, expected, record=True)["status"], "duplicate")
        value["round"] = expected["round"] = 1
        self.assertTrue(self.validate(value, expected, record=True)["recorded"])
        self.assertEqual(len(decode((self.shift / "receipts/.consumed.json").read_bytes())["entries"]), 2)

    def kept(self, ref):
        return self.shift / receipts.kept_path(ref["sha256"])

    def test_consuming_a_receipt_keeps_the_hosts_own_copy_of_every_file_it_names(self):
        value, expected = self.case("build")
        first = value["data"]["checks"][0]["output"]
        self.validate(value, expected)  # a check that records nothing keeps nothing
        self.assertFalse((self.shift / "receipts/.kept").exists())
        self.assertTrue(self.validate(value, expected, record=True)["recorded"])
        for ref in (first, value["brief"]):
            self.assertEqual(receipts.digest(self.kept(ref).read_bytes()), ref["sha256"])
        self.assertEqual(self.kept(first).read_bytes(), b"fixture evidence\n")
        # A later task's build writes its check output over the same name, as a builder may.
        later, context = self.case("build")
        for obj in (later, context):
            obj.update(task_id="second", request_id="second-build")
        later["receipt_id"] = "build-second"
        second = later["data"]["checks"][0]["output"] = self.artifact("logs/tests.txt", "the second task's output\n")
        self.assertEqual(second["path"], first["path"])
        self.assertTrue(self.validate(later, context, record=True)["recorded"])
        self.assertEqual(self.kept(first).read_bytes(), b"fixture evidence\n")
        self.assertEqual(self.kept(second).read_bytes(), b"the second task's output\n")
        # The look at receipts/ around a builder turn does not count the host's copies.
        from sortie_kit import lifecycle
        with receipts.session(self.shift, 5) as view:
            self.assertEqual(sorted(lifecycle.receipt_marks(view)), ["build-second.json", value["receipt_id"] + ".json"])
        info = (self.shift / "receipts/.kept").stat()
        self.assertEqual(info.st_mode & 0o777, 0o700)

    def test_a_receipt_whose_copies_cannot_be_kept_is_not_consumed(self):
        value, expected = self.case("build")
        real = receipts.Evidence.atomic
        def atomic(view, path, raw, **kw):
            if path.startswith("receipts/.kept/"):
                raise OSError("no room")
            return real(view, path, raw, **kw)
        with patch.object(receipts.Evidence, "atomic", atomic):
            with self.assertRaises(Refused):
                self.validate(value, expected, record=True)
        self.assertFalse((self.shift / "receipts/.consumed.json").exists())
        self.assertTrue(self.validate(value, expected, record=True)["recorded"])  # and can be taken once it can

    def test_a_copy_that_is_already_there_is_checked_and_never_replaced(self):
        value, expected = self.case("build")
        ref = value["data"]["checks"][0]["output"]
        (self.shift / "receipts/.kept").mkdir(mode=0o700)
        self.kept(ref).write_bytes(b"not those bytes")
        with self.assertRaisesRegex(Refused, "kept copy"):
            self.validate(value, expected, record=True)
        self.assertEqual(self.kept(ref).read_bytes(), b"not those bytes")
        self.assertFalse((self.shift / "receipts/.consumed.json").exists())

    def test_a_copy_that_is_already_there_is_not_written_again_nor_counted_as_evidence_read(self):
        big = "x" * 200_000 + "\n"
        value, expected = self.case("build")
        value["data"]["checks"][0]["output"] = self.artifact("logs/tests.txt", big)
        self.assertTrue(self.validate(value, expected, record=True)["recorded"])
        # A second task's receipt names the same bytes: the same brief and the same output.
        later, context = self.case("build")
        for obj in (later, context):
            obj.update(task_id="second", request_id="second-build")
        later["receipt_id"] = "build-second"
        ref = later["data"]["checks"][0]["output"] = self.artifact("logs/tests.txt", big)
        totals, written = [], []
        read, atomic = receipts.Evidence.read, receipts.Evidence.atomic
        def reading(view, *args, **kw):
            try:
                return read(view, *args, **kw)
            finally:
                totals.append(view.total)
        def writing(view, path, raw, **kw):
            written.append(path)
            return atomic(view, path, raw, **kw)
        with patch.object(receipts.Evidence, "read", reading):
            self.validate(later, context)  # records nothing: what the check alone reads
        once = max(totals)
        self.assertGreater(once, len(big))
        # Room for the check and the ledger, and not for the kept copy a second time.
        with patch.object(receipts, "TOTAL_LIMIT", once + 65536), patch.object(receipts.Evidence, "atomic", writing):
            self.assertTrue(self.validate(later, context, record=True)["recorded"])
        self.assertEqual([p for p in written if p.startswith("receipts/.kept/")], [])
        self.assertEqual(self.kept(ref).read_bytes(), big.encode())

    def test_a_review_receipt_of_the_hosts_gets_copies_too(self):
        # The host names a review's output files itself, but under logs/, where a builder writes.
        value, expected = self.case("review")
        self.assertTrue(self.validate(value, expected, record=True)["recorded"])
        named = list(receipts.references(value))
        self.assertGreater(len(named), 1)
        for ref in named:
            self.assertEqual(receipts.digest(self.kept(ref).read_bytes()), ref["sha256"])

    def test_a_copy_that_has_another_name_is_refused(self):
        value, expected = self.case("build")
        ref = value["data"]["checks"][0]["output"]
        (self.shift / "receipts/.kept").mkdir(mode=0o700)
        os.link(self.shift / ref["path"], self.kept(ref))  # the right bytes, and still writable through logs/
        with self.assertRaisesRegex(Refused, "kept copy"):
            self.validate(value, expected, record=True)
        self.assertFalse((self.shift / "receipts/.consumed.json").exists())

    def test_unrecorded_checks_never_satisfy_then_record_and_replay(self):
        for phase in receipts.PHASES:
            with self.subTest(phase=phase):
                value, expected = self.case(phase)
                path = self.store(value, expected)
                for _ in range(2):
                    result = receipts.validate_receipt(self.shift, path, "expected.json")
                    self.assertEqual(result["status"], "diagnostic")
                    self.assertFalse(result["satisfies_phase"])
                    self.assertFalse(result["recorded"])
                result = receipts.validate_receipt(self.shift, path, "expected.json", record=True)
                self.assertEqual(result["status"], "satisfied")
                self.assertTrue(result["satisfies_phase"])
                self.assertTrue(result["recorded"])
                for record in (False, True):
                    result = receipts.validate_receipt(self.shift, path, "expected.json", record=record)
                    self.assertEqual(result["status"], "duplicate")
                    self.assertFalse(result["satisfies_phase"])
                    self.assertFalse(result["recorded"])

    def test_cli_diagnostic_exit_then_record_and_replay(self):
        value, expected = self.case()
        path = self.store(value, expected)
        args = ["receipt", "validate", "--shift", str(self.shift),
                "--receipt", path, "--expected", "expected.json"]
        for record, status, code, satisfies in ((False, "diagnostic", 5, False),
                                                (False, "diagnostic", 5, False),
                                                (True, "satisfied", 0, True),
                                                (True, "duplicate", 4, False)):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(args + (["--record"] if record else [])), code)
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], status)
            self.assertEqual(result["satisfies_phase"], satisfies)
            self.assertEqual(result["recorded"], satisfies)

    def test_blocked_evidence_recorded_without_satisfaction(self):
        value, expected = self.case(record=self.record(dict(kind="commit", commit=self.candidate), "CHANGES", [self.finding()]))
        result = self.validate(value, expected, record=True)
        self.assertTrue(result["recorded"])
        self.assertFalse(result["satisfies_phase"])
        self.assertEqual(self.validate(value, expected)["status"], "duplicate")

    def test_two_consumers_record_only_once(self):
        value, expected = self.case()
        path = self.store(value, expected)
        barrier, real_open = threading.Barrier(2), os.open
        def concurrent_open(name, flags, *args, **kwargs):
            if name == ".ledger.lock" and flags & os.O_CREAT:
                barrier.wait(timeout=5)
            return real_open(name, flags, *args, **kwargs)
        with patch("sortie_kit.receipts.os.open", side_effect=concurrent_open), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: receipts.validate_receipt(
                self.shift, path, "expected.json", record=True), range(2)))
        self.assertEqual(sorted(r["status"] for r in results), ["duplicate", "satisfied"])
        self.assertEqual(sum(r["recorded"] for r in results), 1)
        self.assertEqual(len(decode((self.shift / "receipts/.consumed.json").read_bytes())["entries"]), 1)

    def test_corrupt_ledger_and_lock_contention_fail_closed(self):
        value, expected = self.case()
        (self.shift / "receipts/.consumed.json").write_text('{"version":1,"entries":[{}]}')
        with self.assertRaisesRegex(Refused, "ledger"):
            self.validate(value, expected)
        (self.shift / "receipts/.consumed.json").unlink()
        with (self.shift / "receipts/.ledger.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            began = time.monotonic()
            with self.assertRaisesRegex(Refused, "elapsed"):
                self.validate(value, expected, record=True, seconds=0.15)
            self.assertLess(time.monotonic() - began, 1)
        self.assertFalse((self.shift / "receipts/.consumed.json").exists())

    def test_atomic_writer_partial_input_and_failed_commit(self):
        value, expected = self.case("build")
        (self.shift / "expected.json").write_bytes(receipts.encoded(expected))
        path = receipts.write_receipt_atomic(self.shift, value, "expected.json")
        self.assertDiagnostic(receipts.validate_receipt(self.shift, path, "expected.json"))
        with self.assertRaises(Refused):
            receipts.write_receipt_atomic(self.shift, value, "expected.json")
        (self.shift / path).write_text('{"version":')
        with self.assertRaises(Refused):
            receipts.validate_receipt(self.shift, path, "expected.json", record=True)
        self.assertFalse((self.shift / "receipts/.consumed.json").exists())
        self.store(value, expected)
        with patch("sortie_kit.receipts.os.replace", side_effect=OSError("disk fault")), self.assertRaises(Refused):
            receipts.validate_receipt(self.shift, path, "expected.json", record=True)
        self.assertFalse((self.shift / "receipts/.consumed.json").exists())
        self.assertEqual(list((self.shift / "receipts").glob(".receipt-*")), [])

    def test_pinned_expected_digest_refuses_other_contents_before_recording(self):
        value, expected = self.case()
        pin = receipts.digest(receipts.encoded(expected))
        self.assertDiagnostic(self.validate(value, expected, expected_sha256=pin))
        changed = dict(expected, round=expected["round"] + 1)
        value["round"] = changed["round"]  # the receipt agrees with the replacement, not the pin
        with self.assertRaisesRegex(Refused, "expected context changed"):
            self.validate(value, changed, record=True, expected_sha256=pin)
        self.assertFalse((self.shift / "receipts/.consumed.json").exists())
        self.assertDiagnostic(self.validate(value, changed))  # unpinned callers still own their input

    def test_receipt_write_takes_a_filled_template_checks_it_and_writes_it_once(self):
        value, expected = self.case("acknowledgment")
        value["data"]["evidence"] = [self.artifact("logs/ack.txt")]
        (self.shift / "logs/expected.json").write_bytes(receipts.encoded(expected))
        (self.shift / "logs/draft.json").write_text(json.dumps(value, indent=2))
        args = ["receipt", "write", "--shift", str(self.shift), "--expected", "logs/expected.json",
                "--from", "logs/draft.json"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args), 0)
        said = json.loads(out.getvalue())
        path = "receipts/" + value["receipt_id"] + ".json"
        self.assertEqual(said, dict(status="written", path=path, check=dict(status="diagnostic", reasons=[]),
                                    sha256=hashlib.sha256((self.shift / path).read_bytes()).hexdigest()))
        self.assertEqual(decode((self.shift / path).read_bytes()), value)
        self.assertDiagnostic(receipts.validate_receipt(self.shift, path, "logs/expected.json"))
        self.assertEqual([p.name for p in (self.shift / "receipts").iterdir() if p.name.endswith(".tmp")], [])
        kept = (self.shift / path).read_bytes()
        # Written once: a second write of the same receipt ID replaces nothing.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args), 3)
        self.assertEqual(json.loads(out.getvalue())["status"], "invalid")
        self.assertEqual((self.shift / path).read_bytes(), kept)

    def test_receipt_write_refuses_a_draft_the_check_refuses_and_writes_nothing(self):
        value, expected = self.case("acknowledgment")
        value["data"]["evidence"] = [self.artifact("logs/ack.txt")]
        (self.shift / "logs/expected.json").write_bytes(receipts.encoded(expected))
        before = sorted(p.name for p in (self.shift / "receipts").iterdir())
        stale = dict(value, request_id="another-request")
        unfilled = dict(value, shift_id="shift-example")
        missing = {k: v for k, v in value.items() if k != "receipt_id"}
        for name, draft in (("stale", json.dumps(stale)), ("unfilled", json.dumps(unfilled)),
                            ("missing", json.dumps(missing)), ("cut", '{"version":')):
            with self.subTest(name=name):
                (self.shift / "logs/draft.json").write_text(draft)
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = cli.main(["receipt", "write", "--shift", str(self.shift),
                                     "--expected", "logs/expected.json", "--from", "logs/draft.json"])
                said = json.loads(out.getvalue())
                self.assertEqual((code, said["status"]), (3, "invalid"), said)
                self.assertTrue(said["reasons"] and all(said["reasons"]), said)
                self.assertEqual(sorted(p.name for p in (self.shift / "receipts").iterdir()), before)
        # A draft outside the shift is not read.
        outside = self.root / "draft.json"
        outside.write_text(json.dumps(value))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["receipt", "write", "--shift", str(self.shift),
                                       "--expected", "logs/expected.json", "--from", str(outside)]), 3)
        self.assertEqual(sorted(p.name for p in (self.shift / "receipts").iterdir()), before)

    def test_receipt_write_reports_what_the_receipt_check_would_and_refuses_a_consumed_slot(self):
        # The writer's answer and `receipt validate` must not disagree.
        value, expected = self.case("acknowledgment")
        value["data"]["evidence"] = [self.artifact("logs/ack.txt")]
        (self.shift / "logs/expected.json").write_bytes(receipts.encoded(expected))
        args = ["receipt", "write", "--shift", str(self.shift), "--expected", "logs/expected.json",
                "--from", "logs/draft.json"]
        (self.shift / "logs/draft.json").write_text(json.dumps(value))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        path = "receipts/" + value["receipt_id"] + ".json"
        self.assertEqual(receipts.validate_receipt(self.shift, path, "logs/expected.json", record=True)["status"],
                         "satisfied")
        before = sorted(p.name for p in (self.shift / "receipts").iterdir())
        (self.shift / "logs/draft.json").write_text(json.dumps(dict(value, receipt_id="a-second-try")))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args), 3)
        said = json.loads(out.getvalue())
        self.assertEqual(said["status"], "invalid")
        self.assertIn("already consumed", said["reasons"][0])
        self.assertEqual(sorted(p.name for p in (self.shift / "receipts").iterdir()), before)
        # A ledger that cannot be read refuses here as it does in the check.
        (self.shift / "receipts/.consumed.json").write_text("{}")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args), 3)
        self.assertIn("ledger", json.loads(out.getvalue())["reasons"][0])
        self.assertEqual(sorted(p.name for p in (self.shift / "receipts").iterdir()), before)

    def test_receipt_write_keeps_a_truthful_receipt_the_phase_will_not_take(self):
        # "not acknowledged" is a true thing to hand in; the writer keeps it and says what the check made of it.
        value, expected = self.case("acknowledgment")
        value["data"].update(status="not_acknowledged", evidence=[self.artifact("logs/ack.txt")])
        (self.shift / "logs/expected.json").write_bytes(receipts.encoded(expected))
        (self.shift / "logs/draft.json").write_text(json.dumps(value))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["receipt", "write", "--shift", str(self.shift),
                                       "--expected", "logs/expected.json", "--from", "logs/draft.json"]), 0)
        said = json.loads(out.getvalue())
        self.assertEqual((said["status"], said["check"]["status"]), ("written", "blocked"))
        self.assertTrue(said["check"]["reasons"])

    def test_cli_codes_and_never_executes_receipt_commands(self):
        value, expected = self.case("build")
        marker = self.root / "should-not-exist"
        value["data"]["checks"][0]["command"] = "touch " + str(marker)
        path = self.store(value, expected)
        args = ["receipt", "validate", "--shift", str(self.shift), "--receipt", path, "--expected", "expected.json"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args + ["--record"]), 0)
        self.assertTrue(json.loads(out.getvalue())["recorded"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 4)
        (self.shift / path).write_text("partial")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 3)
        self.assertFalse(marker.exists())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["receipt", "template", "review", "--expected"]), 0)
        validate_shape(json.loads(out.getvalue()), "expected")

    def test_driver_md_says_when_to_go_on_and_when_to_finish(self):
        from importlib.resources import files
        text = files("sortie_kit").joinpath("data", "templates", "driver.md").read_text()
        for part in ("means `repair`, then `continue`", "means `continue`: the next turn reads that diagnostic",
                     "`finish` before wind-down is for two cases only"):
            self.assertIn(part, text)
