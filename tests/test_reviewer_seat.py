"""The reviewer seat: profiles that can review, and how the reviewer is picked when none is named."""
import json
import os
from pathlib import Path

from sortie_kit import agents, launcher
from sortie_kit.layout import Refused
from support import LocalTest

READ_ONLY = {
    "claude": ["--print", "--permission-mode", "plan", "--model", "{model}", "{prompt}"],
    "codex": ["exec", "--sandbox", "read-only", "--model", "{model}", "--cd", "{cwd}", "{prompt}"],
    "agy": ["--print", "{prompt}", "--mode", "plan", "--sandbox", "--model", "{model}"],
}
FAMILIES = {"claude": "claude", "codex": "gpt", "agy": "gemini"}


def profile(name="fixture", **changes):
    value = dict(version=1, name=name, executable=name + "-agent",
                 interactive_argv=["--model", "{model}", "{prompt}"],
                 headless_argv=["run", "--model", "{model}", "--cd", "{cwd}", "{prompt}"],
                 review_argv=["review", "--model", "{model}", "--cd", "{cwd}", "{prompt}"],
                 review_model=name + "-review-model",
                 models={name + "-model": name + "-family", name + "-review-model": name + "-family"}, default_model=None)
    value.update(changes)
    return {key: item for key, item in value.items() if item is not ...}


class ReviewerFixture(LocalTest):
    def setUp(self):
        super().setUp()
        self.target = self.repo()
        self.home = self.root / "home"
        self.directory = agents.user_directory(self.home)
        self.directory.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def program(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return Path(os.path.realpath(path))

    def install(self, value):
        (self.directory / (value["name"] + ".json")).write_text(json.dumps(value))
        self.program(value["executable"])
        return value

    def builder(self, agent="codex", family="gpt"):
        return {"agent": agent, "model": agent + "-builder-model", "family": family}

    def pick(self, builder, given=None, **kw):
        return launcher.pick_reviewer(self.target, builder, given, home=self.home, search=str(self.bin), **kw)

    def refused(self, pattern, call, *args, **kwargs):
        with self.assertRaisesRegex(Refused, pattern):
            call(*args, **kwargs)


class ProfileTests(ReviewerFixture):
    def test_each_built_in_profile_can_review_in_its_read_only_form_with_a_named_model(self):
        for name, argv in READ_ONLY.items():
            value = agents.load(name, self.target, home=self.home)["value"]
            self.assertEqual(value["review_argv"], argv, name)
            model, family = agents.choose_model(agents.load(name, self.target, home=self.home), value["review_model"])
            self.assertEqual(family, FAMILIES[name], name)
            for off in ("--dangerously-bypass-approvals-and-sandbox", "--dangerously-skip-permissions",
                        "danger-full-access", "bypassPermissions", "workspace-write", "acceptEdits", "accept-edits"):
                self.assertNotIn(off, value["review_argv"], name)

    def test_a_review_form_and_its_model_go_together_and_the_model_has_a_family(self):
        self.install(profile())
        self.assertTrue(agents.can_review(agents.load("fixture", self.target, home=self.home)))
        for name, changes, pattern in (
                ("argv without a model", dict(review_model=...), "go together"),
                ("a model without argv", dict(review_argv=...), "go together"),
                ("a null argv with a model", dict(review_argv=None), "go together"),
                ("a model no family covers", dict(review_model="other-model"), "review_model"),
                ("a model that is not an ID", dict(review_model="two words"), "review_model"),
                ("argv without the prompt", dict(review_argv=["review", "--model", "{model}"]), "prompt"),
                ("a placeholder inside an argument", dict(review_argv=["--model={model}", "{prompt}"]), "whole argument")):
            with self.subTest(name=name):
                self.install(profile(**changes))
                self.refused(pattern, agents.load, "fixture", self.target, home=self.home)
        # A profile from before the reviewer seat has neither field and still loads; it cannot review.
        self.install(profile(review_argv=..., review_model=...))
        old = agents.load("fixture", self.target, home=self.home)
        self.assertFalse(agents.can_review(old))
        pin = {"path": "/x/fixture", "sha256": "0" * 64, "bytes": 1}
        self.refused("no review start and cannot be the reviewer", agents.seat, "reviewer", old, "fixture-model",
                     "fixture-family", pin)
        # A model of unlisted_family is covered.
        self.install(profile(review_model="any-model", unlisted_family="fixture-family"))
        self.assertTrue(agents.can_review(agents.load("fixture", self.target, home=self.home)))

    def test_the_reviewer_seat_is_signed_like_the_others_from_the_review_form(self):
        self.install(profile())
        loaded = agents.load("fixture", self.target, home=self.home)
        pin, _ = agents.pin_executable(loaded, self.target, search=str(self.bin))
        seat = agents.seat("reviewer", loaded, "fixture-review-model", "fixture-family", pin)
        self.assertEqual((seat["mode"], seat["argv"]), ("review", loaded["value"]["review_argv"]))
        self.assertEqual(agents.seat_checked("reviewer", seat), seat)
        self.refused("invalid seat scope", agents.seat_checked, "builder", seat)
        where = self.root / "review directory"
        argv = agents.render_argv(seat, prompt="Read REVIEW.md", cwd=where)
        self.assertEqual(argv, [pin["path"], "review", "--model", "fixture-review-model", "--cd", str(where), "Read REVIEW.md"])

    def test_reach_says_what_the_reviewer_command_line_asks_for_and_that_nothing_confines_it(self):
        pin = {"path": "/x/agent", "sha256": "0" * 64, "bytes": 1}
        said = {}
        for name in READ_ONLY:
            loaded = agents.load(name, self.target, home=self.home)
            model, family = agents.choose_model(loaded, loaded["value"]["review_model"])
            said[name] = " | ".join(agents.reach("reviewer", agents.seat("reviewer", loaded, model, family, pin)))
        self.assertIn("--sandbox read-only", said["codex"])
        self.assertIn("--permission-mode plan", said["claude"])
        self.assertIn("--mode plan --sandbox", said["agy"])
        self.install(profile())
        loaded = agents.load("fixture", self.target, home=self.home)
        custom = " | ".join(agents.reach("reviewer", agents.seat("reviewer", loaded, "fixture-review-model", "fixture-family", pin)))
        self.assertIn("has not inspected", custom)
        self.assertIn("may be able to write", custom)
        for text in list(said.values()) + [custom]:
            self.assertIn("does not confine it", text)
            self.assertIn("can read whatever your user can read", text)
            self.assertIn("fails that review", text)
        # A built-in profile whose read-only option was edited out is not described as having it.
        loaded = agents.load("codex", self.target, home=self.home)
        seat = agents.seat("reviewer", loaded, "m", "gpt", pin)
        seat["argv"] = [a for a in seat["argv"] if a not in ("--sandbox", "read-only")]
        self.assertNotIn("read-only", " | ".join(agents.reach("reviewer", seat)))


class PickTests(ReviewerFixture):
    def installed(self, *names):
        for name in names:
            self.program(name)

    def test_the_first_installed_agent_of_another_family_is_picked_in_the_stated_order(self):
        self.installed("claude", "codex", "agy")
        seat, choice = self.pick(self.builder("codex", "gpt"))
        self.assertEqual((seat["agent"], seat["model"], seat["family"], seat["mode"], choice),
                         ("claude", "claude-sonnet-5-5", "claude", "review", "picked-other-family"))
        self.assertEqual(seat["argv"], READ_ONLY["claude"])
        self.assertEqual(seat["executable"]["path"], os.path.realpath(self.bin / "claude"))
        # The builder's own family is passed over: claude builds, so codex is next in the order.
        seat, choice = self.pick(self.builder("claude", "claude"))
        self.assertEqual((seat["agent"], seat["family"], choice), ("codex", "gpt", "picked-other-family"))
        # One that is not installed is passed over too.
        (self.bin / "codex").unlink()
        seat, choice = self.pick(self.builder("claude", "claude"))
        self.assertEqual((seat["agent"], seat["family"], choice), ("agy", "gemini", "picked-other-family"))

    def test_the_users_own_profiles_come_after_the_built_in_ones_by_name(self):
        self.install(profile("zeta"))
        self.install(profile("alpha"))
        self.install(profile("mute", review_argv=..., review_model=...))  # cannot review: never picked
        seat, choice = self.pick(self.builder("codex", "gpt"))
        self.assertEqual((seat["agent"], choice), ("alpha", "picked-other-family"))
        self.installed("agy")
        self.assertEqual(self.pick(self.builder("codex", "gpt"))[0]["agent"], "agy")
        self.assertEqual(launcher.review_order(self.target, home=self.home), ["claude", "codex", "agy", "alpha", "mute", "zeta"])

    def test_with_no_other_family_installed_the_builders_own_cli_reviews_and_it_is_said(self):
        self.installed("codex")
        seat, choice = self.pick(self.builder("codex", "gpt"))
        self.assertEqual((seat["agent"], seat["family"], choice), ("codex", "gpt", "picked-same-family"))
        self.assertEqual(seat["model"], agents.load("codex", self.target, home=self.home)["value"]["review_model"])
        # No flag was needed for that. Another profile of the same family does not change it.
        self.install(profile("other", models={"other-review-model": "gpt"}))
        seat, choice = self.pick(self.builder("codex", "gpt"))
        self.assertEqual((seat["agent"], choice), ("codex", "picked-same-family"))
        # A builder whose own profile cannot review: the first that can, still same-family.
        self.install(profile("mute", review_argv=..., review_model=..., models={"mute-model": "gpt"}))
        (self.bin / "codex").unlink()
        seat, choice = self.pick(self.builder("mute", "gpt"))
        self.assertEqual((seat["agent"], seat["family"], choice), ("other", "gpt", "picked-same-family"))

    def test_no_agent_that_can_review_is_refused_in_one_sentence(self):
        self.install(profile("mute", review_argv=..., review_model=...))
        with self.assertRaises(Refused) as caught:
            self.pick(self.builder("mute", "mute-family"))
        text = str(caught.exception)
        self.assertIn("no installed agent can review", text)
        self.assertEqual(text.count(". "), 0, text)
        self.assertNotIn("\n", text)
        for name in ("claude", "codex", "agy"):
            self.assertIn(name, text)

    def test_a_named_reviewer_overrides_the_pick(self):
        self.installed("claude", "codex", "agy")
        seat, choice = self.pick(self.builder("codex", "gpt"), dict(agent="agy", model=None, family=None))
        self.assertEqual((seat["agent"], seat["model"], seat["family"], choice),
                         ("agy", "gemini-4-hydrogen-high", "gemini", "given"))
        seat, choice = self.pick(self.builder("codex", "gpt"), dict(agent="agy", model="gemini-4-carbon-high", family=None))
        self.assertEqual((seat["model"], choice), ("gemini-4-carbon-high", "given"))
        self.install(profile("mute", review_argv=..., review_model=...))
        self.refused("cannot be the reviewer", self.pick, self.builder("codex", "gpt"), dict(agent="mute", model="m", family="x"))
        self.refused("no agent profile named", self.pick, self.builder("codex", "gpt"), dict(agent="zeta-missing", model=None, family=None))
        self.install(profile("lost"))
        (self.bin / "lost-agent").unlink()
        self.refused("not found", self.pick, self.builder("codex", "gpt"), dict(agent="lost", model=None, family=None))

    def test_a_same_family_reviewer_by_flag_needs_the_explicit_flag_while_another_family_is_installed(self):
        self.installed("claude", "codex")
        given = dict(agent="codex", model=None, family=None)
        self.refused("another family is installed.*--allow-same-family-review", self.pick, self.builder("codex", "gpt"), given)
        seat, choice = self.pick(self.builder("codex", "gpt"), given, same_family=True)
        self.assertEqual((seat["agent"], seat["family"], choice), ("codex", "gpt", "given"))
        # With no other family installed the flag is not needed for it.
        (self.bin / "claude").unlink()
        seat, choice = self.pick(self.builder("codex", "gpt"), given)
        self.assertEqual((seat["agent"], choice), ("codex", "given"))
        # The flag says same-family; it does not fit a reviewer of another family.
        self.installed("claude")
        self.refused("families differ", self.pick, self.builder("codex", "gpt"),
                     dict(agent="claude", model=None, family=None), same_family=True)
        # Nor a pick: with another family installed the pick is never same-family.
        self.refused("families differ", self.pick, self.builder("codex", "gpt"), same_family=True)

    def test_a_stated_family_decides_for_a_model_the_profile_does_not_place(self):
        self.installed("codex", "agy")
        # codex lists no models and gives gpt for all of them.
        seat, _ = self.pick(self.builder("claude", "claude"), dict(agent="codex", model="some-model", family=None))
        self.assertEqual((seat["model"], seat["family"]), ("some-model", "gpt"))
        # agy lists its models and gives no family for others: one must be stated.
        self.refused("state its family", self.pick, self.builder("claude", "claude"),
                     dict(agent="agy", model="other-model", family=None))
        seat, _ = self.pick(self.builder("claude", "claude"), dict(agent="agy", model="other-model", family="Google"))
        self.assertEqual((seat["model"], seat["family"]), ("other-model", "gemini"))
        self.refused("contradicts", self.pick, self.builder("claude", "claude"),
                     dict(agent="agy", model="gemini-4-carbon-high", family="gpt"))


if __name__ == "__main__":
    import unittest
    unittest.main()
