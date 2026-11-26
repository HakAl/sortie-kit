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


V1, V2, V3 = "sortie-kit.review.v1", "sortie-kit.review.v2", "sortie-kit.review.v3"


def need(asked="pad keeps the last cell of a row", status="met", where="src/pad.py:12", **changed):
    return dict(dict(asked=asked, status=status, where=where), **changed)


def listed(verdict="ACCEPT", requirements=None, **changed):
    """A commit review as the host asks for it now: the brief answered requirement by requirement."""
    return record(verdict, **dict(dict(schema=V2, requirements=[need()] if requirements is None else requirements),
                                  **changed))


def third(verdict="ACCEPT", requirements=None, findings=(), **changed):
    """A commit review as the host asks for it now: each finding names the requirement it says is not
    delivered in some case, by its position in the list, or none."""
    return listed(verdict, requirements, **dict(dict(
        schema=V3, findings=[dict(dict(requirement=None), **item) for item in findings]), **changed))


def said(value, nonce=NONCE, before="I read the patch.\n", after=""):
    begin, end = review.markers(nonce)
    body = value if isinstance(value, str) else json.dumps(value, indent=1)
    return before + begin + "\n" + body + "\n" + end + "\n" + after


class ContractTests(unittest.TestCase):
    def check(self, text, target=COMMIT, prior=(), schema=None):
        return review.check(text, NONCE, target, list(prior), schema)

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
                ("another schema", record(schema="sortie-kit.review.v9")),
                ("a requirement list where none belongs", record(requirements=[need()])),
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

    def test_a_commit_review_answers_the_brief_requirement_by_requirement(self):
        self.assertEqual((review.schema_for("commit"), review.schema_for("plan")), (V3, V1))
        with self.assertRaises(Refused):
            review.schema_for("branch")
        several = listed(requirements=[need(), need("the old rows still load", "not_checked", ""),
                                       need("nothing is written outside the cache", "not_checked", "tree/src/cache.py")])
        for schema in (V2, None):
            with self.subTest(schema=schema):
                result = self.check(said(several), schema=schema)
                self.assertEqual((result["status"], result["verdict"], result["reasons"]), ("complete", "ACCEPT", []))
                self.assertEqual(result["record"], several)
        self.assertEqual(review.validate(several, COMMIT, [], V2), "ACCEPT")

    def test_the_host_takes_only_the_schema_it_asked_for(self):
        # The live host knows what it asked: an answer in the earlier shape, written by habit, is no review.
        result = self.failed(said(record()), "contract", schema=V2)
        self.assertIn("the schema asked of this review is " + V2, result["reasons"][0])
        result = self.failed(said(listed()), "contract", schema=V1)
        self.assertIn("the schema asked of this review is " + V1, result["reasons"][0])
        # The schema is looked at before the fields, so the reason names it and not the field that follows from it.
        without = {key: value for key, value in listed().items() if key != "requirements"}
        self.assertIn("requirements", self.failed(said(without), "contract", schema=V2)["reasons"][0])
        # A plan is not asked for the list, and a record that carries one for a plan is refused whoever reads it.
        plan = listed(target=PLAN)
        self.failed(said(plan), "contract", target=PLAN)
        self.failed(said(plan), "contract", target=PLAN, schema=V1)
        self.assertEqual(self.check(said(record(target=PLAN)), target=PLAN, schema=V1)["verdict"], "ACCEPT")

    def test_the_reason_names_the_list_only_where_the_list_is_what_is_wrong(self):
        reason = lambda value: self.failed(said(value), "contract")["reasons"][0]
        self.assertIn("this schema has no requirements list", reason(record(requirements=[need()])))
        for value in (record(score=9), {k: v for k, v in record().items() if k != "not_examined"}, listed(score=9)):
            self.assertNotIn("requirements list", reason(value))
            self.assertIn("missing or unknown fields", reason(value))

    def test_a_review_kept_from_before_the_list_is_still_read(self):
        # With no schema named the record's own decides: this is a kept receipt read again, not a live answer.
        self.assertEqual(self.check(said(record()))["verdict"], "ACCEPT")
        self.assertEqual(self.check(said(record()), schema=V1)["verdict"], "ACCEPT")
        self.assertEqual(review.validate(record(), COMMIT, []), "ACCEPT")

    def test_a_schema_the_host_cannot_ask_for_is_the_callers_fault(self):
        for target, schema in ((COMMIT, "sortie-kit.review.v9"), (PLAN, V2), (PLAN, V3), (COMMIT, ""), (COMMIT, 2)):
            with self.subTest(target=target, schema=schema):
                with self.assertRaises(Refused):
                    review.asked(target, [], schema)
                with self.assertRaises(Refused):
                    review.check(said(listed()), NONCE, target, [], schema)
                with self.assertRaises(Refused):
                    review.validate(listed(), target, [], schema)
        for target, schema in ((COMMIT, V3), (COMMIT, V2), (COMMIT, V1), (PLAN, V1), (COMMIT, None), (PLAN, None)):
            review.asked(target, [], schema)

    def test_the_requirement_list_has_exactly_its_shape(self):
        for name, value in (
                ("no list", {key: value for key, value in listed().items() if key != "requirements"}),
                ("an empty list", listed(requirements=[])),
                ("not a list", listed(requirements=dict(asked="x", status="met", where="y"))),
                ("text for a list", listed(requirements="all met")),
                ("too many", listed(requirements=[need("thing " + str(i)) for i in range(65)])),
                ("an item that is not an object", listed(requirements=["pad keeps the last cell: met"])),
                ("an unknown field", listed(requirements=[need(note="sure")])),
                ("no where", listed(requirements=[dict(asked="x", status="not_checked")])),
                ("a blank asked", listed(requirements=[need(" ")])),
                ("asked that is not text", listed(requirements=[need(3)])),
                ("an overlong asked", listed(requirements=[need("a" * 501)])),
                ("an overlong where", listed(requirements=[need(where="w" * 501)])),
                ("a status with a space", listed(requirements=[need(status="not met")])),
                ("a status in capitals", listed(requirements=[need(status="MET")])),
                ("a status that is not text", listed(requirements=[need(status=True)])),
                ("met without where", listed(requirements=[need(where="")])),
                ("not_met without where", listed("CHANGES", findings=[finding()], requirements=[need(status="not_met", where="")])),
                ("a blank where", listed(requirements=[need(status="not_checked", where=" ")])),
                ("where that is not text", listed(requirements=[need(status="not_checked", where=None)]))):
            with self.subTest(name=name):
                for schema in (V2, None):
                    result = self.failed(said(value), "contract", schema=schema)
                    self.assertIn("requirement", result["reasons"][0])
        most = listed(requirements=[need("thing " + str(i), where="w" * 500) for i in range(63)] + [need("a" * 500)])
        self.assertEqual(self.check(said(most), schema=V2)["verdict"], "ACCEPT")

    def test_an_accept_beside_a_requirement_not_met_is_not_an_accept(self):
        missed = need("retry seconds and the minimum wait agree", "not_met", "src/advice.py:88")
        for schema in (V2, None):
            result = self.failed(said(listed(requirements=[need(), missed])), "ACCEPT with a requirement answered not_met "
                                 "(retry seconds and the minimum wait agree) is not an accept", schema=schema)
            self.assertIsNone(result["record"])
        # The same with the defect listed as a minor finding beside it, as a real shift's reviewer did.
        self.failed(said(listed(findings=[finding(severity="minor")], requirements=[missed])), "answered not_met", schema=V2)
        # What the refusal quotes of the reviewer's words is one short line.
        long = need("first\x1b[31m line\nsecond\tline\u2028" + "x" * 200, "not_met")
        reason = self.failed(said(listed(requirements=[long])), "answered not_met", schema=V2)["reasons"][0]
        self.assertIn("(first [31m line second line " + "x" * 52 + ")", reason)
        self.assertTrue(reason.isprintable(), reason)
        # Sent back with a finding it is a review like any other; the host rewrites no verdict.
        changes = listed("CHANGES", findings=[finding()], requirements=[need(), missed])
        result = self.check(said(changes), schema=V2)
        self.assertEqual((result["verdict"], result["record"]), ("CHANGES", changes))
        # A requirement not met is not by itself a finding: CHANGES still names something to change.
        self.failed(said(listed("CHANGES", requirements=[missed])), "CHANGES names nothing to change", schema=V2)

    def test_an_accept_beside_a_requirement_not_checked_is_taken(self):
        unchecked = listed(requirements=[need(), need("no caller outside the module changes", "not_checked", "")])
        result = self.check(said(unchecked), schema=V2)
        self.assertEqual((result["status"], result["verdict"]), ("complete", "ACCEPT"))
        self.assertEqual(result["record"]["requirements"][1]["status"], "not_checked")
        self.assertEqual(self.check(said(listed(requirements=[need(status="not_checked")])), schema=V2)["verdict"], "ACCEPT")

    MISSED = need("retry seconds and the minimum wait agree", "not_met", "src/advice.py:88")

    def test_a_finding_of_a_commit_review_names_the_requirement_it_says_is_not_delivered_or_none(self):
        changes = third("CHANGES", [need(), self.MISSED], [finding(requirement=2), finding("F2", "minor")])
        self.assertEqual([item["requirement"] for item in changes["findings"]], [2, None])
        for schema in (V3, None):
            result = self.check(said(changes), schema=schema)
            self.assertEqual((result["status"], result["verdict"], result["record"]), ("complete", "CHANGES", changes))
        self.assertEqual(self.check(said(third()), schema=V3)["verdict"], "ACCEPT")
        # The field is on every finding, and is a position in this answer's own list or null.
        for name, value in (("no field", listed("CHANGES", [self.MISSED], schema=V3, findings=[finding()])),
                            ("zero", 0), ("past the list", 3), ("below it", -1), ("true", True), ("a fraction", 2.0),
                            ("text", "2"), ("a list", [2])):
            with self.subTest(name=name):
                answer = value if isinstance(value, dict) else third(
                    "CHANGES", [need(), self.MISSED], [finding(requirement=value)])
                for schema in (V3, None):
                    reason = self.failed(said(answer), "contract", schema=schema)["reasons"][0]
                    self.assertIn("`requirement`", reason)
        # A plan review has no list and so no such field; nor has a review kept in an earlier shape.
        self.failed(said(third(target=PLAN)), "contract", target=PLAN)
        for earlier in (record("CHANGES", findings=[finding(requirement=None)]),
                        listed("CHANGES", [self.MISSED], findings=[finding(requirement=1)])):
            self.failed(said(earlier), "contract")

    def test_a_finding_that_names_a_requirement_marked_met_or_not_checked_is_no_review(self):
        """Seen in a replay of a kept review: the requirement marked `met`, and a minor finding that said of
        the same requirement that a case fails, under ACCEPT. The answer holds two things at once; the host
        takes neither."""
        for status, where in (("met", "src/advice.py:88"), ("not_checked", ""), ("not_checked", "src/advice.py:88")):
            asked = need("retry seconds and the minimum wait agree", status, where)
            for verdict, severity in (("ACCEPT", "minor"), ("CHANGES", "minor"), ("CHANGES", "major")):
                with self.subTest(status=status, verdict=verdict, severity=severity):
                    answer = third(verdict, [need(), asked], [finding(severity=severity, requirement=2)])
                    for schema in (V3, None):
                        result = self.failed(said(answer), "finding F1 names requirement 2 (retry seconds and the minimum "
                                             "wait agree), which the list marks `" + status + "`", schema=schema)
                        self.assertNotIn("contract", result["reasons"][0])
                        self.assertIsNone(result["record"])
        # The same finding with no requirement named is the reviewer saying it is about something else: taken.
        aside = third(requirements=[need(), need("retry seconds and the minimum wait agree")],
                      findings=[finding(severity="minor")])
        self.assertEqual(self.check(said(aside), schema=V3)["verdict"], "ACCEPT")
        # What the refusal quotes of the reviewer's words is one short printable line.
        long = need("first\x1b[31m line\nsecond\tline\u2028" + "x" * 200)
        reason = self.failed(said(third("CHANGES", [long], [finding(requirement=1)])), "names requirement 1", schema=V3)["reasons"][0]
        self.assertIn("(first [31m line second line " + "x" * 52 + ")", reason)
        self.assertTrue(reason.isprintable(), reason)
        self.assertLess(len(reason), 300)

    def test_an_accept_beside_not_met_stays_no_accept_and_a_finding_may_name_what_is_not_met(self):
        named = third(requirements=[need(), self.MISSED], findings=[finding(severity="minor", requirement=2)])
        self.failed(said(named), "ACCEPT with a requirement answered not_met", schema=V3)
        sent_back = dict(named, verdict="CHANGES")
        self.assertEqual(self.check(said(sent_back), schema=V3)["verdict"], "CHANGES")

    def test_a_requirement_the_brief_lets_be_closed_by_saying_so_is_disclosed_and_an_accept_beside_it_is_taken(self):
        """Seen on a real shift: the brief allowed either delivering a part or saying where that it is not
        delivered. The reviewer was right to accept, and the part was neither met nor a breach."""
        told = need("the second part, or a statement that it is not delivered", "disclosed", "docs/notes.md:12")
        for findings in ([], [finding(severity="minor", requirement=2)]):
            answer = third(requirements=[need(), told], findings=findings)
            for schema in (V3, None):
                result = self.check(said(answer), schema=schema)
                self.assertEqual((result["status"], result["verdict"], result["record"]), ("complete", "ACCEPT", answer))
        # It names the place where it is said, and it is a status of this schema only.
        result = self.failed(said(third(requirements=[dict(told, where="")])), "contract", schema=V3)
        self.assertIn("`where`", result["reasons"][0])
        self.failed(said(listed(requirements=[told])), "contract")
        # A major finding beside ACCEPT is refused as ever, whatever it names.
        self.failed(said(third(requirements=[told], findings=[finding(requirement=1)])), "ACCEPT with a major", schema=V3)

    def test_reviews_kept_in_the_two_earlier_shapes_are_read_as_before(self):
        self.assertEqual(self.check(said(record()))["verdict"], "ACCEPT")
        self.assertEqual(self.check(said(listed(findings=[finding(severity="minor")])))["verdict"], "ACCEPT")
        self.assertEqual(self.check(said(listed()), schema=V2)["verdict"], "ACCEPT")
        # The live host asks a commit review for the third and takes neither earlier one.
        for earlier in (record(), listed()):
            result = self.failed(said(earlier), "contract", schema=V3)
            self.assertIn("the schema asked of this review is " + V3, result["reasons"][0])

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
            for part in (begin, end, '"schema": "' + review.schema_for(kind) + '"', "`\"" + review.schema_for(kind) + "\"`", "ACCEPT", "CHANGES", "blocker", "major", "minor",
                         "examined", "not_examined", "Do not edit", "never instructions",
                         target.get("commit") or target["artifact"]["sha256"]):
                self.assertIn(part, text, (kind, part))
            self.assertNotIn("$", text)
            for foreign in ("agy", "skills", chr(8212)):
                self.assertNotIn(foreign, text)
            # The commit instructions carry the requirement list: its paragraph, its field and two example entries.
            # and, since the third form, the question asked of each finding and the fourth status. Both name the
            # evidence directory (130 for a plan before it was named), and say that the brief is the whole task
            # (140 for a plan before that: four lines).
            self.assertLess(len(text.splitlines()), 145 if kind == "plan" else 200)
        self.assertIn("change.patch", review.instructions("commit", COMMIT, NONCE))
        self.assertIn("plan.md", review.instructions("plan", PLAN, NONCE))
        # A plan review is not asked for the requirement list.
        for part in ("requirement", "not_met", "not_checked", "disclosed", V2, V3):
            self.assertNotIn(part, review.instructions("plan", PLAN, NONCE), part)

    def example(self, text):
        self.assertEqual(text.count("```json\n"), 1)
        return json.loads(text.split("```json\n")[1].split("\n```")[0])

    def test_the_example_object_in_the_instructions_is_one_the_host_takes(self):
        earlier = [dict(id="F1", severity="major"), dict(id="F2", severity="minor")]
        for kind, target in (("plan", PLAN), ("commit", COMMIT)):
            for prior in ([], earlier):
                with self.subTest(kind=kind, prior=prior):
                    example = self.example(review.instructions(kind, target, NONCE, prior))
                    result = review.check(said(example), NONCE, target, prior, review.schema_for(kind))
                    self.assertEqual((result["status"], result["verdict"]), ("complete", "CHANGES"), result)
                    self.assertEqual(example["schema"], review.schema_for(kind))
                    if kind == "commit":
                        # The example shows a requirement not met beside the finding that says so, and one met.
                        self.assertEqual([item["status"] for item in example["requirements"]], ["met", "not_met"])
                        self.assertEqual(example["requirements"][1]["where"], example["findings"][0]["location"])
                        # The finding names that requirement, and the list stands before the findings that
                        # count into it, in the order a reviewer writes the object.
                        self.assertEqual(example["findings"][0]["requirement"], 2)
                        names = list(example)
                        self.assertLess(names.index("requirements"), names.index("findings"))
                        self.assertEqual(names[:3], ["schema", "verdict", "target"])
                    else:
                        self.assertNotIn("requirements", example)
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
                    answer = (third if kind == "commit" else record)(target=target, prior=answers)
                    result = review.check(text + said(answer), NONCE, target, prior, review.schema_for(kind))
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

    def test_the_commit_review_is_told_to_answer_the_brief_requirement_by_requirement(self):
        """Seen on a real shift: the brief asked for one thing, the reviewer found that the reported defect
        could still happen, rated it minor and accepted. The must-hold rule did not fire, because the brief
        stated the wanted behaviour as the task itself. The reviewer now lists what the brief asks for and
        answers each; the host refuses an accept beside one not met."""
        text = " ".join(review.instructions("commit", COMMIT, NONCE).split())
        for part in ("Start from the brief.", "under `requirements`", "`met`, `not_met`, `not_checked` or `disclosed`",
                     "`met` means you read the code that does it, not that a test or a message says so.",
                     "Every `not_met` is a finding.",
                     "for a finding that something the brief asks for is not delivered, unless you marked it "
                     "`disclosed`, and, where the task is a "
                     "defect, for a finding that the reported defect can still happen after the change: at least "
                     "`major`, whatever its size",
                     "The host does not take an `ACCEPT` beside a `not_met`",
                     "is no reason to accept and does not lower a severity.",
                     "- `requirements`: a list, never empty.", "`asked`", "`where`"):
            self.assertIn(part, text, part)
        # Words that were tried on kept reviews and changed nothing are not shipped; the field has one spelling.
        for gone in ("callers outside the task", "`not met`", "`not checked`"):
            self.assertNotIn(gone, text, gone)

    def test_the_commit_review_is_asked_of_each_finding_which_requirement_it_says_is_not_delivered(self):
        """Seen in a replay: a requirement marked `met` beside a finding that said a case of it fails. The
        question is put per finding, and what the brief lets be closed by saying so has its own status."""
        text = " ".join(review.instructions("commit", COMMIT, NONCE).split())
        for part in ("does this finding mean that something the brief asks for is not delivered in some case? Then "
                     "name it under `requirement`",
                     "by its position in your list, counted from 1",
                     "A test that proves less than it claims is `null` unless the brief asks for that test by name.",
                     "The host does not take a finding that names a requirement you marked `met` or `not_checked`",
                     "`disclosed` is only for a thing that `brief.md` itself allows to be closed by saying that it "
                     "is not delivered",
                     "A gap, a limit or a note of work left that the builder wrote down by its own choice is `not_met`",
                     "`met`, `not_met`, `not_checked` or `disclosed`", "`requirement` (", "`null`",
                     # From the one read of this form: a disclosure the brief allows is no breach to rate major,
                     # and the two paragraphs on findings and on proof that is not here say the same status.
                     "A requirement you marked `disclosed` needs no finding",
                     "unless you marked it `disclosed`",
                     "a requirement that rests on it is `not_checked`",
                     "where in the patch, the tree or `evidence/` you saw it",
                     "the place in the patch, the tree or `evidence/` where you saw it"):
            self.assertIn(part, text, part)

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
            # A saved output is checked by the schema its own object names: one of a commit review of now, with
            # its requirement list, and one kept from before the list.
            for kept in (listed(), third(findings=[finding(severity="minor")])):
                output.write_text(said(kept))
                code, text = self.run_cli("review", "check", str(output), "--expected", str(expected))
                self.assertEqual((code, json.loads(text)["verdict"]), (0, "ACCEPT"))
            output.write_text(said(listed(requirements=[need(status="not_met")])))
            code, text = self.run_cli("review", "check", str(output), "--expected", str(expected))
            self.assertEqual((code, json.loads(text)["status"]), (1, "failed"))
            self.assertIn("answered not_met", json.loads(text)["reasons"][0])
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
            self.assertIn('"schema": "' + review.schema_for(kind) + '"', text)
            self.assertIn("BEGIN SORTIE REVIEW", text)


if __name__ == "__main__":
    unittest.main()
