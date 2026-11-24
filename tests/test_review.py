"""The review contract: what a reviewer is told, and what of its answer the host takes."""
import contextlib
import io
import json
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from sortie_kit import cli, review
from sortie_kit.layout import Refused

NONCE = "4f2a9c0d17e84b6a"
COMMIT = dict(kind="commit", commit="a" * 40)
PLAN = dict(kind="plan", artifact=dict(path="briefs/plan.md", sha256="b" * 64))


def finding(identity="F1", severity="major", **changed):
    return dict(dict(id=identity, severity=severity, location="src/pad.py:12", claim="pad drops the last cell",
                     evidence="line 12 slices to width - 1", recommendation="slice to width"), **changed)


def record(verdict="ACCEPT", target=COMMIT, findings=(), prior=(), **changed):
    return dict(dict(schema="sortie-kit.review.v1", verdict=verdict, target=target,
                     findings=list(findings) if isinstance(findings, (list, tuple)) else findings,
                     prior=list(prior), examined=["change.patch", "src/pad.py"], not_examined=[]), **changed)


def said(value, nonce=NONCE, before="I read the patch.\n", after=""):
    begin, end = review.markers(nonce)
    body = value if isinstance(value, str) else json.dumps(value, indent=1)
    return before + begin + "\n" + body + "\n" + end + "\n" + after


class ContractTests(unittest.TestCase):
    def check(self, text, target=COMMIT, prior=()):
        return review.check(text, NONCE, target, list(prior))

    def failed(self, text, part, **kw):
        result = self.check(text, **kw)
        self.assertEqual((result["status"], result["verdict"]), ("failed", None), result)
        self.assertIn(part, " ".join(result["reasons"]))
        return result

    def test_an_accept_and_a_changes_are_taken_with_their_record(self):
        result = self.check(said(record()))
        self.assertEqual((result["status"], result["verdict"], result["reasons"]), ("complete", "ACCEPT", []))
        self.assertEqual(result["record"], record())
        changes = record("CHANGES", findings=[finding(), finding("F2", "minor")])
        result = self.check(said(changes))
        self.assertEqual((result["status"], result["verdict"]), ("complete", "CHANGES"))
        self.assertEqual(result["record"]["findings"][1]["id"], "F2")
        # An accept may carry minor findings.
        self.assertEqual(self.check(said(record(findings=[finding("m1", "minor")])))["verdict"], "ACCEPT")
        # A plan is reviewed by the same contract.
        self.assertEqual(self.check(said(record(target=PLAN)), target=PLAN)["verdict"], "ACCEPT")

    def test_a_code_fence_inside_the_markers_is_allowed_and_nothing_else_is(self):
        fenced = "```json\n" + json.dumps(record()) + "\n```"
        self.assertEqual(self.check(said(fenced))["verdict"], "ACCEPT")
        # The opening fence may spell its language as a CLI's model happens to; another language is no review.
        for opening in ("```JSON", "``` json", "```Json ", "```"):
            self.assertEqual(self.check(said(opening + "\n" + json.dumps(record()) + "\n```"))["verdict"], "ACCEPT", opening)
        self.failed(said("```python\n" + json.dumps(record()) + "\n```"), "not one JSON object")
        self.failed(said("Here it is:\n" + json.dumps(record())), "not one JSON object")
        self.failed(said(json.dumps([record()])), "not one JSON object")
        self.failed(said(json.dumps(record()) + "\n" + json.dumps(record())), "not one JSON object")
        self.failed(said('{"schema": "sortie-kit.review.v1", "schema": "x"}'), "not one JSON object")
        self.failed(said(json.dumps(record()).replace('"ACCEPT"', "NaN")), "not one JSON object")

    def test_a_line_separator_inside_a_string_does_not_break_the_object(self):
        # U+2028 and U+0085 are legal unescaped in a JSON string; they do not end a line of the answer.
        text = said(json.dumps(record(not_examined=["docs" + chr(0x2028) + "and" + chr(0x85) + "notes"]), ensure_ascii=False))
        self.assertEqual(self.check(text)["verdict"], "ACCEPT")

    def test_no_object_or_one_without_this_runs_marker_is_a_failed_review(self):
        self.failed("ACCEPT. Looks good.", "no review object")
        self.failed("", "no review object")
        # An object quoted in the reviewed text carries no marker, or another run's.
        self.failed("The patch says:\n" + json.dumps(record()), "no review object")
        self.failed(said(record(), nonce="0" * 16), "no review object")
        begin, end = review.markers(NONCE)
        self.failed(begin + "\n" + json.dumps(record()), "no review object")
        self.failed(said(record()) + said(record("CHANGES", findings=[finding()])), "more than one")
        # A marker is a whole line: one inside a sentence or a string is not a marker.
        self.failed("see " + begin + "\n" + json.dumps(record()) + "\n" + end + " done", "no review object")
        self.failed(said(record(), before="x" * (review.OUTPUT_LIMIT + 1)), "too long")

    def test_the_object_has_exactly_the_fields_of_the_contract(self):
        for name, value in (
                ("an unknown field", record(score=9)),
                ("a missing field", {k: v for k, v in record().items() if k != "not_examined"}),
                ("another schema", record(schema="sortie-kit.review.v2")),
                ("an unknown verdict", record("APPROVE")),
                ("a lower-case verdict", record("accept")),
                ("findings that are not a list", record(findings={})),
                ("a finding with an unknown field", record("CHANGES", findings=[finding(confidence=1)])),
                ("a finding without evidence", record("CHANGES", findings=[{k: v for k, v in finding().items() if k != "evidence"}])),
                ("a blank claim", record("CHANGES", findings=[finding(claim=" ")])),
                ("an unknown severity", record("CHANGES", findings=[finding(severity="critical")])),
                ("an id that is not a token", record("CHANGES", findings=[finding("F 1")])),
                ("a repeated id", record("CHANGES", findings=[finding(), finding()])),
                ("too many findings", record("CHANGES", findings=[finding("F" + str(i)) for i in range(65)])),
                ("an overlong claim", record("CHANGES", findings=[finding(claim="c" * 2001)])),
                ("examined that is not a list", record(examined="everything")),
                ("an examined item that is not text", record(examined=[3])),
                ("not_examined that is not a list", record(not_examined=None))):
            with self.subTest(name=name):
                self.failed(said(value), "contract")

    def test_what_was_examined_may_not_be_empty(self):
        self.failed(said(record(examined=[])), "examined")
        self.assertEqual(self.check(said(record(not_examined=["tests/ (not in the patch)"])))["verdict"], "ACCEPT")

    def test_the_target_is_the_one_the_host_asked_for(self):
        self.failed(said(record(target=dict(kind="commit", commit="c" * 40))), "target")
        self.failed(said(record(target=PLAN)), "target")
        self.failed(said(record(target=COMMIT)), "target", target=PLAN)
        self.failed(said(record(target="a" * 40)), "target")

    def test_what_the_host_itself_passes_wrongly_is_refused_and_is_not_a_failed_review(self):
        # A failed review is the reviewer's doing. A bad target or list of earlier findings is the caller's.
        for target, prior in ((None, []), (dict(kind="commit", commit="HEAD"), []), ("a" * 40, []),
                              (COMMIT, [dict(id="F1")]), (COMMIT, [dict(id="F1", severity="major")] * 2), (COMMIT, None)):
            with self.subTest(target=target, prior=prior):
                with self.assertRaises(Refused):
                    review.check(said(record()), NONCE, target, prior)
                with self.assertRaises(Refused):
                    review.validate(record(), target, prior)

    def test_a_target_has_the_shape_receipts_use(self):
        long_commit = dict(kind="commit", commit="c" * 64)  # a repository with SHA-256 object names
        self.assertEqual(self.check(said(record(target=long_commit)), target=long_commit)["verdict"], "ACCEPT")
        for path in (" ", "", "\t\n"):
            with self.subTest(path=path), self.assertRaises(Refused):
                review.target_checked("plan", dict(kind="plan", artifact=dict(path=path, sha256="b" * 64)))

    def test_an_accept_that_its_own_findings_contradict_is_not_an_accept(self):
        for severity in ("blocker", "major"):
            result = self.failed(said(record(findings=[finding(severity=severity)])), "ACCEPT with a " + severity)
            self.assertIsNone(result["verdict"])
        self.failed(said(record("CHANGES")), "CHANGES names nothing to change")
        # A CHANGES with only minor findings is the reviewer's call and is taken.
        self.assertEqual(self.check(said(record("CHANGES", findings=[finding(severity="minor")])))["verdict"], "CHANGES")

    def test_a_repair_round_answers_every_earlier_finding_and_no_other(self):
        prior = [dict(id="F1", severity="major"), dict(id="F2", severity="minor")]
        yes = [dict(id="F1", resolved=True, reason="width is used now"), dict(id="F2", resolved=True, reason="renamed")]
        self.assertEqual(self.check(said(record(prior=yes)), prior=prior)["verdict"], "ACCEPT")
        for name, answers in (("one missing", yes[:1]), ("one unknown", yes + [dict(id="F9", resolved=True, reason="x")]),
                              ("one twice", yes + yes[:1]), ("none", [])):
            with self.subTest(name=name):
                self.failed(said(record(prior=answers)), "earlier finding", prior=prior)
        self.failed(said(record(prior=yes)), "earlier finding")  # a first round has none to answer
        for name, answer in (("resolved not a boolean", dict(id="F1", resolved="yes", reason="x")),
                             ("no reason", dict(id="F1", resolved=True)),
                             ("an unknown field", dict(id="F1", resolved=True, reason="x", note="y"))):
            with self.subTest(name=name):
                self.failed(said(record(prior=[answer, yes[1]])), "contract", prior=prior)
        # A new finding may not reuse the id of an earlier one.
        self.failed(said(record("CHANGES", prior=yes, findings=[finding("F1")])), "reuses", prior=prior)

    def test_an_unresolved_earlier_major_blocks_an_accept_and_an_unresolved_minor_does_not(self):
        prior = [dict(id="F1", severity="major"), dict(id="F2", severity="minor")]
        open_major = [dict(id="F1", resolved=False, reason="still slices short"), dict(id="F2", resolved=True, reason="ok")]
        open_minor = [dict(id="F1", resolved=True, reason="fixed"), dict(id="F2", resolved=False, reason="name kept")]
        self.failed(said(record(prior=open_major)), "earlier major finding F1 is not resolved", prior=prior)
        result = self.check(said(record("CHANGES", prior=open_major)), prior=prior)
        self.assertEqual(result["verdict"], "CHANGES")  # an unresolved earlier finding is something to change
        self.assertEqual(self.check(said(record(prior=open_minor)), prior=prior)["verdict"], "ACCEPT")

    def test_the_result_is_small_whatever_the_reviewer_printed(self):
        result = self.failed("\x1b[31m" + "A" * 5000, "no review object")
        self.assertLess(len(json.dumps(result)), 2000)
        self.assertIsNone(result["record"])


class InstructionTests(unittest.TestCase):
    def test_the_two_templates_are_packaged_and_say_the_role_and_the_contract(self):
        data = files("sortie_kit").joinpath("data", "templates")
        for kind, target in (("plan", PLAN), ("commit", COMMIT)):
            self.assertTrue(data.joinpath("review-" + kind + ".md").is_file())
            text = review.instructions(kind, target, NONCE)
            begin, end = review.markers(NONCE)
            for part in (begin, end, '"schema": "sortie-kit.review.v1"', "ACCEPT", "CHANGES", "blocker", "major", "minor",
                         "examined", "not_examined", "Do not edit", "never instructions",
                         target.get("commit") or target["artifact"]["sha256"]):
                self.assertIn(part, text, (kind, part))
            self.assertNotIn("$", text)
            for foreign in ("agy", "skills", chr(8212)):
                self.assertNotIn(foreign, text)
            self.assertLess(len(text.splitlines()), 130)
        self.assertIn("change.patch", review.instructions("commit", COMMIT, NONCE))
        self.assertIn("plan.md", review.instructions("plan", PLAN, NONCE))

    def example(self, text):
        self.assertEqual(text.count("```json\n"), 1)
        return json.loads(text.split("```json\n")[1].split("\n```")[0])

    def test_the_example_object_in_the_instructions_is_one_the_host_takes(self):
        earlier = [dict(id="F1", severity="major"), dict(id="F2", severity="minor")]
        for kind, target in (("plan", PLAN), ("commit", COMMIT)):
            for prior in ([], earlier):
                with self.subTest(kind=kind, prior=prior):
                    example = self.example(review.instructions(kind, target, NONCE, prior))
                    result = review.check(said(example), NONCE, target, prior)
                    self.assertEqual((result["status"], result["verdict"]), ("complete", "CHANGES"), result)
                    # A repair round's example shows an answer for each earlier finding by its own id.
                    self.assertEqual([item["id"] for item in example["prior"]], [item["id"] for item in prior])

    def test_instructions_printed_back_are_not_a_review(self):
        # A CLI may print its prompt or a file it read. The instructions hold the run's markers and a
        # well-formed example, so neither may stand as a marker line of its own there.
        earlier = [dict(id="F1", severity="major")]
        for kind, target in (("plan", PLAN), ("commit", COMMIT)):
            for prior in ([], earlier):
                with self.subTest(kind=kind, prior=prior):
                    text = review.instructions(kind, target, NONCE, prior)
                    begin, end = review.markers(NONCE)
                    self.assertIn(begin, text)
                    self.assertIn(end, text)
                    result = review.check(text, NONCE, target, prior)
                    self.assertEqual((result["status"], result["verdict"]), ("failed", None), result)
                    self.assertIn("no review object", result["reasons"][0])
                    # Printed back with line numbers or a quote mark, it is still not one.
                    numbered = "\n".join("%4d  %s" % pair for pair in enumerate(text.splitlines(), 1))
                    self.assertEqual(review.check(numbered, NONCE, target, prior)["status"], "failed")
                    # And it does not spoil the real answer that follows it.
                    answers = [dict(id=item["id"], resolved=True, reason="checked") for item in prior]
                    result = review.check(text + said(record(target=target, prior=answers)), NONCE, target, prior)
                    self.assertEqual((result["status"], result["verdict"]), ("complete", "ACCEPT"), result)

    def test_the_instructions_say_how_long_and_how_named(self):
        for kind, target in (("plan", PLAN), ("commit", COMMIT)):
            text = review.instructions(kind, target, NONCE)
            self.assertIn("2000 characters", text)
            self.assertIn("letters, digits", text)
            self.assertIn("starts with a letter or digit", text)
            self.assertIn("at most 64 characters", text)
        self.assertNotIn("the patch", review.instructions("plan", PLAN, NONCE))

    def test_a_breach_of_what_the_brief_requires_is_at_least_major(self):
        """Seen on real shifts: a reviewer found a breach of what the brief said must hold, rated
        it minor and accepted. The severity is not left to the reviewer there. What is not such a breach
        is named, so that the rule does not make every weak check major, and a doubt stays no finding."""
        weak = {"plan": "A case the plan's checks do not cover, or a check that proves less than it claims,",
                "commit": "A case that is not tested, or a test that proves less than it claims,"}
        for kind, target, where in (("plan", PLAN, "plan"), ("commit", COMMIT, "change")):
            text = " ".join(review.instructions(kind, target, NONCE).split())
            severity = text.split("Severity: ", 1)[1].split(" Verdict: ", 1)[0]
            subject = "the plan" if kind == "plan" else "the commit"
            for part in ("A finding that " + subject + " breaks something the brief says must hold, or does something the "
                         "brief rules out, is at least `major`, whatever its size.",
                         "That means a breach you can point to, with the brief's line and the place in the " + where
                         + " that goes against it.",
                         weak[kind] + " is not on its own such a breach and keeps the severity it would have had.",
                         "A doubt you could not settle is still not a finding."):
                self.assertIn(part, severity, (kind, part))
            for kept in ("`blocker` means the task's outcome is not met or is unsafe to keep; `major` means material harm "
                         "or rework if kept as is; `minor` is local and does not stand in the way.",):
                self.assertIn(kept, severity, kind)
            # The verdict rule is as it was, so such a finding leads to CHANGES.
            self.assertIn("Verdict: `ACCEPT` when no blocker or major finding stands. `CHANGES` when one does, or when "
                          "you judge the minor ones must be fixed first. You may accept and still list minor findings.", text)

    def test_a_repair_round_lists_the_earlier_findings_to_answer(self):
        prior = [dict(id="F1", severity="major"), dict(id="F2", severity="minor")]
        text = review.instructions("commit", COMMIT, NONCE, prior)
        self.assertIn("answers.json", text)
        self.assertIn("F1 (major), F2 (minor)", text)
        self.assertNotIn("answers.json", review.instructions("commit", COMMIT, NONCE))

    def test_instructions_refuse_a_kind_a_target_or_a_marker_they_do_not_know(self):
        for args in (("branch", COMMIT, NONCE), ("plan", COMMIT, NONCE), ("commit", PLAN, NONCE),
                     ("commit", COMMIT, "short"), ("commit", COMMIT, "g" * 16), ("commit", dict(kind="commit", commit="HEAD"), NONCE)):
            with self.subTest(args=args), self.assertRaises(Refused):
                review.instructions(*args)

    def test_instructions_refuse_earlier_findings_that_are_not_a_list(self):
        for prior in (None, 5, "F1", dict(id="F1", severity="major"), iter([dict(id="F1", severity="major")])):
            with self.subTest(prior=prior), self.assertRaises(Refused):
                review.instructions("commit", COMMIT, NONCE, prior)
        self.assertIn("F1 (major)", review.instructions("commit", COMMIT, NONCE, (dict(id="F1", severity="major"),)))

    def test_a_new_marker_is_made_for_each_run(self):
        first, second = review.nonce(), review.nonce()
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"\A[0-9a-f]{16}\Z")


class CommandTests(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_review_check_validates_a_saved_output_offline(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".scratch") as root:
            root = Path(root)
            expected = root / "expected.json"
            expected.write_text(json.dumps(dict(marker=NONCE, target=COMMIT, prior=[])))
            output = root / "output.txt"
            output.write_text(said(record()))
            code, text = self.run_cli("review", "check", str(output), "--expected", str(expected))
            self.assertEqual((code, json.loads(text)["verdict"]), (0, "ACCEPT"))
            output.write_text("no object here")
            code, text = self.run_cli("review", "check", str(output), "--expected", str(expected))
            self.assertEqual((code, json.loads(text)["status"]), (1, "failed"))
            expected.write_text(json.dumps(dict(marker=NONCE, target=COMMIT)))
            code, text = self.run_cli("review", "check", str(output), "--expected", str(expected))
            self.assertEqual(code, 2)
            self.assertIn("expected", text)
            code, text = self.run_cli("review", "check", str(root / "none.txt"), "--expected", str(expected))
            self.assertEqual(code, 2)

    def test_review_check_calls_an_overlong_output_failed_and_a_bad_expected_file_unusable(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".scratch") as root:
            root = Path(root)
            expected = root / "expected.json"
            good = json.dumps(dict(marker=NONCE, target=COMMIT, prior=[]))
            expected.write_text(good)
            output = root / "output.txt"
            # Whatever its size, an output longer than the host looks through is a failed review (exit 1).
            for size in (review.OUTPUT_LIMIT + 1, 5 * review.OUTPUT_LIMIT):
                output.write_text("x" * size + "\n" + said(record()))
                code, text = self.run_cli("review", "check", str(output), "--expected", str(expected))
                self.assertEqual((code, json.loads(text)["status"]), (1, "failed"), size)
                self.assertIn("too long", json.loads(text)["reasons"][0])
            output.write_text(said(record()))
            for name, body in (("a repeated key", good[:-1] + ', "prior": []}'),
                               ("nesting too deep to read", '{"marker": ' + "[" * 100000 + "]" * 100000 + "}"),
                               ("not an object", "[]")):
                with self.subTest(name=name):
                    expected.write_text(body)
                    code, text = self.run_cli("review", "check", str(output), "--expected", str(expected))
                    self.assertEqual((code, json.loads(text)["status"]), (2, "unusable"))

    def test_review_instructions_prints_what_a_reviewer_is_told(self):
        for kind in ("plan", "commit"):
            code, text = self.run_cli("review", "instructions", kind)
            self.assertEqual(code, 0)
            self.assertIn('"schema": "sortie-kit.review.v1"', text)
            self.assertIn("BEGIN SORTIE REVIEW", text)


if __name__ == "__main__":
    unittest.main()
