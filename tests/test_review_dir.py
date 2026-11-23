"""The directory a reviewer is started in, and the look that tells a review changed something."""
import hashlib
import json
import os
import subprocess

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
