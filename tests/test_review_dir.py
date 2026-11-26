"""The directory a reviewer is started in, and the look that tells a review changed something."""
import hashlib
import json
import os
import subprocess
import types
from unittest import mock

from sortie_kit import receipts, review, review_dir
from sortie_kit.layout import Refused
import test_receipts

NONCE = "4f2a9c0d17e84b6a"


class ReviewDirectoryTests(test_receipts.ReceiptTests):
    def make(self, name="request-1", **changed):
        brief = self.artifact("briefs/task.md", "Pad the cell.\n")
        given = dict(kind="commit", target=dict(kind="commit", commit=self.candidate), base=self.base, brief=brief,
                     plan=None, prior=[], findings=[], answers=[], nonce=NONCE, seconds=20)
        given.update(changed)
        with receipts.session(self.shift, 30) as view:
            return review_dir.make(view, name, **given)

    def test_a_commit_review_gets_instructions_brief_patch_and_a_plain_export(self):
        path, sha = self.make()
        self.assertEqual(path, "reviews/request-1")
        where = self.shift / path
        self.assertEqual(sorted(p.name for p in where.iterdir()), ["REVIEW.md", "brief.md", "change.patch", "tree"])
        told = (where / "REVIEW.md").read_text()
        self.assertEqual(told, review.instructions("commit", dict(kind="commit", commit=self.candidate), NONCE))
        self.assertEqual(sha, hashlib.sha256(told.encode()).hexdigest())
        self.assertEqual((where / "brief.md").read_text(), "Pad the cell.\n")
        patch = (where / "change.patch").read_text()
        self.assertIn("-before", patch)
        self.assertIn("+after", patch)
        self.assertEqual((where / "tree" / "source.txt").read_text(), "after\n")
        # A plain export: no Git data, so nothing done there reaches the repository's.
        self.assertFalse((where / "tree" / ".git").exists())
        self.assertFalse((where / "tree.tar").exists())

    def test_the_patch_is_the_whole_task_and_not_the_last_commit(self):
        (self.target / "second.txt").write_text("second\n")
        later = self.commit()
        path, _ = self.make(target=dict(kind="commit", commit=later))
        patch = (self.shift / path / "change.patch").read_text()
        self.assertIn("+after", patch)   # the first commit of the task
        self.assertIn("+second", patch)  # and the second
        self.assertEqual((self.shift / path / "tree" / "second.txt").read_text(), "second\n")
        # A task begun in a repository without any commit: the patch is everything.
        path, _ = self.make("request-2", base=None)
        self.assertIn("+after", (self.shift / path / "change.patch").read_text())
        self.assertIn("direction.md", (self.shift / path / "change.patch").read_text())

    def test_a_plan_review_gets_the_plan_and_the_files_as_they_are_and_no_patch(self):
        plan = self.artifact("briefs/plan.md", "1. Pad.\n")
        target = dict(kind="plan", artifact=plan)
        path, _ = self.make(kind="plan", target=target)
        where = self.shift / path
        self.assertEqual(sorted(p.name for p in where.iterdir()), ["REVIEW.md", "brief.md", "plan.md", "tree"])
        self.assertEqual((where / "plan.md").read_text(), "1. Pad.\n")
        self.assertEqual((where / "tree" / "source.txt").read_text(), "after\n")
        # A commit review of a task that had a plan carries the accepted plan too.
        path, _ = self.make("request-2", plan=plan)
        self.assertEqual((self.shift / path / "plan.md").read_text(), "1. Pad.\n")

    def test_a_repair_round_carries_the_earlier_findings_and_the_builders_answers(self):
        prior = [dict(id="F1", severity="major")]
        findings = [dict(id="F1", severity="major", location="source.txt:1", claim="wrong word", evidence="line 1",
                         recommendation="say after")]
        answers = [dict(id="F1", disposition="fixed", reason="says after now", evidence=None)]
        path, _ = self.make(prior=prior, findings=findings, answers=answers)
        where = self.shift / path
        kept = json.loads((where / "answers.json").read_text())
        self.assertEqual(kept, dict(earlier_findings=findings, builder_answers=answers))
        self.assertIn("F1 (major)", (where / "REVIEW.md").read_text())
        self.assertFalse((self.shift / self.make("request-2")[0] / "answers.json").exists())

    def taken(self, name="ack-1", log=b"Summary: 25 checks, 0 failed\n", stage="ack", keep=True):
        """A receipt of the builder's as the host holds it after taking it: the file, the
        entry on the task, and the host's kept copy of the one log it names."""
        sha = hashlib.sha256(log).hexdigest()
        (self.shift / "logs").mkdir(exist_ok=True)
        (self.shift / "logs" / (name + "-check.txt")).write_bytes(log)
        raw = receipts.encoded(dict(receipt_id=name, phase="acknowledgment", data=dict(
            reason="Baseline first: passed.", evidence=[dict(path="logs/" + name + "-check.txt", sha256=sha)])))
        (self.shift / "receipts" / (name + ".json")).write_bytes(raw)
        if keep:
            (self.shift / "receipts" / ".kept").mkdir(exist_ok=True)
            (self.shift / "receipts" / ".kept" / sha).write_bytes(log)
        return dict(path="receipts/" + name + ".json", sha256=hashlib.sha256(raw).hexdigest(), stage=stage, round=0,
                    status="satisfied", at="2030-01-01T10:00:00+00:00")

    def test_the_receipts_the_host_took_and_the_files_they_name_are_shown_with_the_hosts_own_statement(self):
        item = self.taken()
        path, _ = self.make(evidence=[item])
        where = self.shift / path / "evidence"
        self.assertEqual(sorted(p.name for p in where.iterdir()), ["HOST.md", "files", "receipts"])
        self.assertEqual((where / "receipts" / "ack-1.json").read_bytes(), (self.shift / item["path"]).read_bytes())
        self.assertEqual((where / "files" / "logs" / "ack-1-check.txt").read_bytes(), b"Summary: 25 checks, 0 failed\n")
        said = " ".join((where / "HOST.md").read_text().split())
        for part in ("Written by the host", "evidence/receipts/ack-1.json", "acknowledgment", "round 0",
                     "2030-01-01T10:00:00+00:00", item["sha256"], "evidence/files/logs/ack-1-check.txt",
                     "The task began at commit " + self.base, "The host did not judge what these files say"):
            self.assertIn(part, said)
        # A direct caller that names no evidence gets no such directory, and one with
        # nothing taken yet gets the statement alone.
        self.assertFalse((self.shift / self.make("request-2")[0] / "evidence").exists())
        alone = self.shift / self.make("request-3", evidence=[])[0] / "evidence"
        self.assertEqual([p.name for p in alone.iterdir()], ["HOST.md"])
        self.assertIn("has taken no receipt", (alone / "HOST.md").read_text())

    def test_what_is_shown_is_what_the_host_took_and_what_cannot_be_shown_is_named_and_stops_no_review(self):
        first = self.taken()
        # The log was rewritten after the host took the receipt: the kept copy is shown.
        (self.shift / "logs" / "ack-1-check.txt").write_bytes(b"rewritten later\n")
        changed = self.taken("plan-1", stage="plan")
        (self.shift / changed["path"]).write_bytes(b"{}")
        unkept = self.taken("build-1", log=b"other log\n", stage="build", keep=False)
        large = self.taken("build-2", log=b"x" * (review_dir.EVIDENCE_FILE + 1), stage="build")
        path, _ = self.make(evidence=[first, changed, unkept, large])
        where = self.shift / path / "evidence"
        self.assertEqual((where / "files" / "logs" / "ack-1-check.txt").read_bytes(), b"Summary: 25 checks, 0 failed\n")
        self.assertEqual(sorted(p.name for p in (where / "receipts").iterdir()),
                         ["ack-1.json", "build-1.json", "build-2.json"])
        self.assertEqual([p.name for p in (where / "files" / "logs").iterdir()], ["ack-1-check.txt"])
        said = " ".join((where / "HOST.md").read_text().split())
        other, big = hashlib.sha256(b"other log\n").hexdigest(), hashlib.sha256(b"x" * (review_dir.EVIDENCE_FILE + 1))
        for part in ("`receipts/plan-1.json` is not shown: it changed after the host took it",
                     "names `logs/build-1-check.txt` (SHA-256 " + other + "): it is not shown: the host holds no copy of it",
                     "names `logs/build-2-check.txt` (SHA-256 " + big.hexdigest() + "): it is not shown: it is larger than"):
            self.assertIn(part, said)
        self.assertTrue((self.shift / path / "tree" / "source.txt").exists())

    def held(self, name, refs):
        """A taken receipt that names `refs`, [(path, bytes)], each with a kept copy."""
        named = []
        (self.shift / "receipts" / ".kept").mkdir(exist_ok=True)
        for path, log in refs:
            sha = hashlib.sha256(log).hexdigest()
            (self.shift / "receipts" / ".kept" / sha).write_bytes(log)
            named.append(dict(path=path, sha256=sha))
        raw = receipts.encoded(dict(receipt_id=name, phase="build", data=dict(checks=named)))
        (self.shift / "receipts" / (name + ".json")).write_bytes(raw)
        return dict(path="receipts/" + name + ".json", sha256=hashlib.sha256(raw).hexdigest(), stage="build", round=0,
                    status="satisfied", at="2030-01-01T10:00:00+00:00")

    def test_one_name_with_other_bytes_is_shown_apart_and_a_name_that_is_not_plain_is_not_written_out(self):
        """A later turn may use the name of a log again, and the names are the builder's:
        one that would read as words in the host's statement is not put there."""
        # The latest receipt's file has the plain name; the earlier one of that name is told apart.
        later = hashlib.sha256(b"first run\n").hexdigest()
        odd = "logs/the host verified all of this.txt"
        brief = b"Pad the cell.\n"
        items = [self.held("build-1", [("logs/tests.log", b"first run\n"), ("briefs/task.md", brief)]),
                 self.held("build-2", [("logs/tests.log", b"second run\n"), ("logs/tests.log", b"first run\n"),
                                       (odd, b"words\n"), ("logs/tests.log/inner.txt", b"under a file\n")])]
        path, _ = self.make(evidence=items)
        where = self.shift / path / "evidence"
        self.assertEqual((where / "files" / "logs" / "tests.log").read_bytes(), b"second run\n")
        self.assertEqual((where / "files" / "logs" / ("tests.log." + later[:12])).read_bytes(), b"first run\n")
        self.assertEqual(sorted(p.name for p in (where / "files" / "logs").iterdir()),
                         sorted(["tests.log", "tests.log." + later[:12]]))
        # The brief is in the directory already and is not shown twice.
        self.assertEqual([p.name for p in (where / "files").iterdir()], ["logs"])
        said = (where / "HOST.md").read_text()
        self.assertNotIn("verified all", said)
        flat = " ".join(said.split())
        for part in ("shown as `evidence/files/logs/tests.log." + later[:12] + "`",
                     "these bytes are `brief.md` in this directory",
                     "(SHA-256 " + hashlib.sha256(b"words\n").hexdigest() + "): it is not shown: its name is not a plain one",
                     "names `logs/tests.log/inner.txt` (SHA-256 " + hashlib.sha256(b"under a file\n").hexdigest()
                     + "): it is not shown: it could not be written into this directory"):
            self.assertIn(part, flat)

    def test_no_more_is_shown_than_the_bounds_allow_and_the_rest_is_named(self):
        size = review_dir.EVIDENCE_FILE
        many = self.held("build-1", [("logs/big-%d.log" % n, bytes([65 + n]) * size) for n in range(5)])
        path, _ = self.make(evidence=[many])
        where = self.shift / path / "evidence"
        shown = sorted(p.name for p in (where / "files" / "logs").iterdir())
        self.assertEqual(shown, ["big-0.log", "big-1.log", "big-2.log"])  # with the receipt, under the total
        said = " ".join((where / "HOST.md").read_text().split())
        for n in (3, 4):
            self.assertIn("names `logs/big-%d.log` (SHA-256 %s): it is not shown: with it more than %d"
                          % (n, hashlib.sha256(bytes([65 + n]) * size).hexdigest(), review_dir.EVIDENCE_TOTAL), said)
        # No more files than may be, the receipts among them, and nothing is read for the rest.
        # An earlier receipt that names many files takes the place of no later receipt, nor
        # of every file a later one names: the receipts come first, then a file of each in turn.
        small = self.held("build-2", [("logs/s-%03d.log" % n, b"%d\n" % n) for n in range(70)])
        path, _ = self.make("request-2", evidence=[small, self.held("build-3", [("logs/current.log", b"current\n")])])
        where = self.shift / path / "evidence"
        self.assertEqual(sorted(p.name for p in (where / "receipts").iterdir()), ["build-2.json", "build-3.json"])
        self.assertEqual((where / "files" / "logs" / "current.log").read_bytes(), b"current\n")
        self.assertEqual(len(list((where / "files" / "logs").iterdir())), review_dir.EVIDENCE_FILES - 2)
        said = " ".join((where / "HOST.md").read_text().split())
        self.assertIn("names `logs/s-069.log` (SHA-256 " + hashlib.sha256(b"69\n").hexdigest() + "): it is not shown: "
                      + review_dir.FULL, said)

    def test_evidence_that_cannot_be_shown_in_its_time_or_at_all_leaves_the_review_what_it_had(self):
        """The export of the files is what a review cannot do without. It is made first, and
        evidence gets a share of the time left after it."""
        item = self.taken()
        # With no time for it nothing of evidence is made, not the directory either.
        with mock.patch.object(review_dir, "EVIDENCE_SHARE", 0):
            path, _ = self.make(evidence=[item])
        where = self.shift / path
        self.assertEqual((where / "tree" / "source.txt").read_text(), "after\n")
        self.assertFalse((where / "evidence").exists())
        with mock.patch.object(review_dir, "show", side_effect=AssertionError("called with no time")):
            path, _ = self.make("request-0", evidence=[item], evidence_seconds=0)
        self.assertFalse((self.shift / path / "evidence").exists())
        seen = []

        def failing(*args, **given):
            seen.append((self.shift / "reviews" / "request-2" / "tree" / "source.txt").exists())
            raise OSError("no space left")
        with mock.patch.object(review_dir, "show", failing):
            path, sha = self.make("request-2", evidence=[item])
        self.assertEqual(seen, [True])  # the files were exported before evidence was tried
        self.assertEqual(sorted(p.name for p in (self.shift / path).iterdir()), ["REVIEW.md", "brief.md", "change.patch", "tree"])
        # A read that is slow stops at the end of the evidence's own time, between two pieces.
        clock = iter([0.0, 0.0, 0.0, 99.0])  # the start, the look before the read, the first piece, the next
        (self.shift / "receipts" / ".kept").mkdir(exist_ok=True)
        with mock.patch.object(review_dir, "moment", lambda: next(clock, 99.0)):
            path, _ = self.make("request-3", evidence=[item])
        said = " ".join((self.shift / path / "evidence" / "HOST.md").read_text().split())
        self.assertIn(review_dir.LATE, said)
        self.assertEqual((self.shift / path / "tree" / "source.txt").read_text(), "after\n")

    def test_evidence_ends_at_the_callers_bound_and_not_at_its_share_of_the_time(self):
        """The caller says by when it needs the directory back, counted from its call. Time
        that the export took is time evidence does not have, and a showing that is slow
        stops at the bound although its share of the call's time is far from used.

        The module's clock is the test's own here, moved on only by the two slow steps, so
        that a machine that stalls cannot change what is shown."""
        many = self.held("build-1", [("logs/n-%d.log" % n, b"%d\n" % n) for n in range(8)])
        export, put = review_dir.export, review_dir.put
        now = [1000.0]
        clock = mock.patch.object(review_dir, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
        clock.start()
        self.addCleanup(clock.stop)

        def slow_export(*args):
            now[0] += 0.3
            return export(*args)
        with mock.patch.object(review_dir, "export", slow_export):
            path, _ = self.make(evidence=[many], evidence_seconds=0.2, seconds=20)
        self.assertTrue((self.shift / path / "tree" / "source.txt").exists())
        self.assertFalse((self.shift / path / "evidence").exists())

        def slow_put(folder, parts, raw):
            if parts[0] == "files":
                now[0] += 0.15
            return put(folder, parts, raw)
        with mock.patch.object(review_dir, "put", slow_put):
            path, _ = self.make("request-2", evidence=[many], evidence_seconds=1.0, seconds=20)
        where = self.shift / path / "evidence"
        shown = len(list((where / "files" / "logs").iterdir()))
        self.assertEqual(shown, 7)  # the eighth would be written at 1.05 seconds, past the bound of 1.0
        self.assertIn("names `logs/n-7.log` (SHA-256 " + hashlib.sha256(b"7\n").hexdigest() + "): it is not shown: "
                      + review_dir.LATE, " ".join((where / "HOST.md").read_text().split()))

    def test_a_plan_review_an_entry_of_an_older_state_and_a_brief_with_a_name_that_is_not_plain(self):
        item = self.taken()
        del item["at"]
        brief = self.artifact("briefs/task.md", "Pad the cell.\n")
        named = self.held("plan-1", [("briefs/my task (1).md", b"Pad the cell.\n")])
        plan = self.artifact("briefs/plan.md", "The plan.\n")
        head = self.candidate  # where the fixture's repository is
        path, _ = self.make(kind="plan", target=dict(kind="plan", artifact=plan), base=head, evidence=[item, named])
        said = " ".join((self.shift / path / "evidence" / "HOST.md").read_text().split())
        for part in ("taken at a time that was not recorded", "The task began at commit " + head + ". The repository "
                     "is still at that commit now.", "names a file (SHA-256 " + brief["sha256"] + "): these bytes are "
                     "`brief.md` in this directory"):
            self.assertIn(part, said)
        self.assertNotIn("my task", said)

    def test_the_instructions_name_the_evidence_and_say_that_missing_proof_is_a_limit_and_not_a_finding(self):
        for kind, target in (("plan", dict(kind="plan", artifact=dict(path="briefs/plan.md", sha256="0" * 64))),
                             ("commit", dict(kind="commit", commit=self.candidate))):
            with self.subTest(kind):
                text = " ".join(review.instructions(kind, target, NONCE).split())
                for part in ("`evidence/`: the builder's receipts for this task that the host has taken",
                             "`evidence/HOST.md`", "Proof that this directory cannot hold",
                             "its absence alone is no new finding",
                             "An earlier finding that rested only on such absence is resolved",
                             "one that named a fault stays unresolved until you can check the correction",
                             "or a shown receipt leaves out is a finding as before"):
                    self.assertIn(part, text)
                again = " ".join(review.instructions(kind, target, NONCE, [dict(id="F1", severity="major")]).split())
                for part in ("`resolved` is true only when you checked that it is, or when the finding rested only on "
                             "proof this directory cannot hold",):
                    self.assertIn(part, again)

    def test_a_directory_is_made_once_and_only_under_reviews(self):
        self.make()
        with self.assertRaisesRegex(Refused, "already exists"):
            self.make()
        for name in ("../escape", "a/b", "", ".hidden", "x" * 200):
            with self.subTest(name=name), self.assertRaises(Refused):
                self.make(name)
        # A link put where the folder goes is not followed.
        os.rename(self.shift / "reviews", self.shift / "reviews-kept")
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.shift / "reviews")
        with self.assertRaises((Refused, OSError)):
            self.make("request-9")
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_brief_or_plan_that_changed_since_it_was_named_is_refused(self):
        brief = self.artifact("briefs/task.md", "Pad the cell.\n")
        (self.shift / "briefs/task.md").write_text("Something else.\n")
        with self.assertRaisesRegex(Refused, "hash mismatch"):
            self.make_with(brief)  # make() would write the brief again

    def make_with(self, brief):
        with receipts.session(self.shift, 30) as view:
            return review_dir.make(view, "request-7", kind="commit", target=dict(kind="commit", commit=self.candidate),
                                   base=self.base, brief=brief, plan=None, prior=[], findings=[], answers=[],
                                   nonce=NONCE, seconds=20)

    def test_an_unknown_commit_and_a_patch_that_is_too_large_are_refused(self):
        with self.assertRaises(Refused):
            self.make(target=dict(kind="commit", commit="0" * 40))
        (self.target / "big.txt").write_text("x" * 4096 + "\n")
        big = self.commit()
        limit = review_dir.PATCH_LIMIT
        review_dir.PATCH_LIMIT = 1024
        try:
            with self.assertRaisesRegex(Refused, "too large"):
                self.make("request-2", target=dict(kind="commit", commit=big))
        finally:
            review_dir.PATCH_LIMIT = limit

    def test_the_snapshot_sees_a_new_commit_a_changed_file_and_a_new_file_and_not_the_shift(self):
        first = review_dir.snapshot(self.target)
        self.assertEqual(first["head"], self.candidate)
        self.make()  # the review directory is under the excluded shift
        (self.shift / "logs" / "note.txt").write_text("host log\n")
        self.assertEqual(review_dir.snapshot(self.target), first)
        (self.target / "source.txt").write_text("edited by a reviewer\n")
        changed = review_dir.snapshot(self.target)
        self.assertNotEqual(changed["status_sha256"], first["status_sha256"])
        self.assertEqual(changed["head"], first["head"])
        subprocess.run(["git", "-C", str(self.target), "checkout", "-q", "--", "source.txt"], check=True, timeout=5)
        self.assertEqual(review_dir.snapshot(self.target), first)
        (self.target / "new.txt").write_text("left behind\n")
        self.assertNotEqual(review_dir.snapshot(self.target), first)
        later = self.commit()
        self.assertEqual(review_dir.snapshot(self.target)["head"], later)

    def test_a_repository_without_a_commit_has_no_head_and_an_empty_tree_to_show(self):
        fresh = self.root / "fresh"
        fresh.mkdir()
        subprocess.run(["git", "init", "-q", str(fresh)], check=True, capture_output=True, timeout=10)
        self.assertIsNone(review_dir.head(fresh))
        self.assertIsNone(review_dir.snapshot(fresh)["head"])
        with self.assertRaises(Refused):
            review_dir.head(self.root / "not-a-repository")

    def test_a_commit_that_holds_no_file_is_exported_as_an_empty_directory(self):
        # `git archive` of such a commit is a header and nothing else, which tarfile refuses to read.
        fresh = self.root / "fresh"
        fresh.mkdir()
        subprocess.run(["git", "init", "-q", str(fresh)], check=True, capture_output=True, timeout=10)
        subprocess.run(["git", "-C", str(fresh), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "base"],
                       check=True, capture_output=True, timeout=10)
        where = self.root / "export"
        self.assertEqual(review_dir.export(fresh, review_dir.head(fresh), str(where), 20), 0)
        self.assertEqual((list(where.iterdir()), (self.root / "export.tar").exists()), ([], False))


# Inherit the fixture, not the receipt suite a second time.
for _name in tuple(vars(test_receipts.ReceiptTests)):
    if _name.startswith("test_"):
        setattr(ReviewDirectoryTests, _name, None)


if __name__ == "__main__":
    import unittest
    unittest.main()
