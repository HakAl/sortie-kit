"""Guided start: what is asked, in which order, and what is listed when nobody can answer.

No terminal, grant or agent is involved: answers are fed to the question logic.
"""
import json

from sortie_kit import agents, guided
from sortie_kit.layout import Refused
from support import LocalTest, agent_fixture


class Answers:
    """Stands in for the person: one reply per question, checked once."""
    def __init__(self, *replies):
        self.replies, self.asked = list(replies), []

    def __call__(self, label, options, check, hint=""):
        self.asked.append((label, list(options)))
        return check(self.replies.pop(0))


def given(agents=None, families=None, seconds=None):
    return dict(agents=dict(agents or {}), families=dict(families or {}), seconds=seconds)


class GuidedTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.repo_path = self.repo()
        self.home = agent_fixture(self.root, self.repo_path)["home"]

    def complete(self, value, ask=None):
        return guided.complete(value, self.repo_path, ask=ask, home=self.home)

    def test_every_choice_given_by_flags_asks_nothing(self):
        ask = Answers()
        choices, seconds = self.complete(given(
            {"driver": "fixture:fixture-model", "builder": "fixture:fixture-model"}, seconds=1800.0), ask)
        self.assertEqual(ask.asked, [])
        self.assertEqual(choices, {role: {"agent": "fixture", "model": "fixture-model", "family": None}
                                   for role in ("driver", "builder")})  # no reviewer: the start picks one
        self.assertEqual(seconds, 1800.0)

    def test_missing_choices_are_asked_in_order_from_installed_profiles_and_the_reviewer_never_is(self):
        ask = Answers("fixture", "fixture-model", "fixture", "fixture-model", "45m")
        choices, seconds = self.complete(given(), ask)
        self.assertEqual([label for label, _ in ask.asked],
                         ["Driver agent", "Driver model", "Builder agent", "Builder model",
                          "How long may the shift run"])
        self.assertFalse([label for label, _ in ask.asked if "review" in label.casefold()])
        offered = dict(ask.asked)
        # agy has no start of its own, so it is offered for neither seat.
        self.assertEqual(offered["Driver agent"], ["claude", "codex", "fixture"])
        self.assertEqual(offered["Builder agent"], ["claude", "codex", "fixture"])
        self.assertEqual(offered["Builder model"], ["fixture-model", "fixture-review-model"])
        self.assertEqual(choices["builder"], {"agent": "fixture", "model": "fixture-model", "family": None})
        self.assertEqual((sorted(choices), seconds), (["builder", "driver"], 2700.0))

    def test_a_reviewer_named_by_flag_is_checked_and_passed_on_without_a_question(self):
        both = {"driver": "fixture:fixture-model", "builder": "fixture:fixture-model"}
        ask = Answers()
        choices, _ = self.complete(given(dict(both, reviewer="fixture"), seconds=60.0), ask)
        self.assertEqual(choices["reviewer"], {"agent": "fixture", "model": None, "family": None})
        choices, _ = self.complete(given(dict(both, reviewer="agy:gemini-4-carbon-high"), seconds=60.0), ask)
        self.assertEqual(choices["reviewer"], {"agent": "agy", "model": "gemini-4-carbon-high", "family": None})
        choices, _ = self.complete(given(dict(both, reviewer="fixture:new-model"), {"reviewer": "OpenAI"}, 60.0), ask)
        self.assertEqual(choices["reviewer"], {"agent": "fixture", "model": "new-model", "family": "gpt"})
        self.assertEqual(ask.asked, [])
        path = agents.user_directory(self.home) / "plain.json"
        value = json.loads((agents.user_directory(self.home) / "fixture.json").read_text())
        path.write_text(json.dumps({k: v for k, v in dict(value, name="plain").items()
                                    if k not in ("review_argv", "review_model")}))
        for named, families, message in (
                (dict(both, reviewer="plain"), {}, "has no review start and cannot be the reviewer"),
                (dict(both, reviewer="nosuch"), {}, "no agent profile named nosuch"),
                (dict(both, reviewer="fixture:new-model"), {}, "--family reviewer=FAMILY"),
                (dict(both, reviewer="fixture:bad\x1b[2Jmodel"), {}, "invalid model ID for the reviewer"),
                (both, {"reviewer": "gemini"}, "goes with --agent reviewer=")):
            with self.subTest(named=named, families=families), self.assertRaisesRegex(Refused, message) as caught:
                self.complete(given(named, families, 60.0), ask)
            self.assertNotIsInstance(caught.exception, guided.Missing)
            self.assertNotIn("\x1b", str(caught.exception))
        self.assertEqual(ask.asked, [])

    def test_a_profile_without_a_model_list_says_what_to_type_and_gives_an_example(self):
        hints = {}
        replies = ["codex", "gpt-next", "fixture", "fixture-model", "30m"]
        def ask(label, options, check, hint=""):
            hints[label] = (list(options), hint)
            return check(replies.pop(0))
        self.complete(given(), ask)
        options, hint = hints["Driver model"]
        self.assertEqual(options, [])
        self.assertIn("lists no models", hint)
        self.assertIn("type the model ID", hint)
        self.assertRegex(hint, r"for example gpt-[\w.-]+$")
        self.assertLessEqual(len(hint), 150)  # shown whole: the terminal line is cut at 200
        # A profile that lists models offers them and needs no example.
        self.assertEqual(hints["Builder model"][0], ["fixture-model", "fixture-review-model"])
        self.assertNotIn("for example", hints["Builder model"][1])

    def test_only_what_is_missing_is_asked(self):
        ask = Answers("fixture-model", "2h")
        choices, seconds = self.complete(given({"driver": "fixture:fixture-model", "builder": "fixture"}), ask)
        self.assertEqual([label for label, _ in ask.asked], ["Builder model", "How long may the shift run"])
        self.assertEqual((choices["builder"]["model"], seconds), ("fixture-model", 7200.0))

    def test_an_unlisted_model_asks_for_its_family_unless_the_profile_names_one(self):
        ask = Answers("fixture", "new-model", "Other", "codex", "gpt-next", "30m")
        choices, _ = self.complete(given(), ask)
        self.assertEqual([label for label, _ in ask.asked],
                         ["Driver agent", "Driver model", "Driver model family", "Builder agent", "Builder model",
                          "How long may the shift run"])
        self.assertEqual(choices["driver"], {"agent": "fixture", "model": "new-model", "family": "other"})
        # The codex profile lists no model and says which family an unlisted one belongs to.
        self.assertEqual(choices["builder"], {"agent": "codex", "model": "gpt-next", "family": None})

    def test_a_profile_default_model_is_used_without_asking(self):
        path = agents.user_directory(self.home) / "fixture.json"
        value = json.loads(path.read_text())
        path.write_text(json.dumps(dict(value, default_model="fixture-model")))
        ask = Answers()
        choices, _ = self.complete(given({"driver": "fixture", "builder": "fixture"}, seconds=60.0), ask)
        self.assertEqual(ask.asked, [])
        self.assertEqual(choices["driver"]["model"], "fixture-model")

    def test_with_nobody_to_ask_everything_missing_is_listed_and_nothing_is_guessed(self):
        with self.assertRaises(guided.Missing) as caught:
            self.complete(given({"builder": "fixture", "driver": "fixture:new-model"}))
        self.assertEqual(caught.exception.names, [
            "--family driver=FAMILY (model new-model is not in the fixture profile)",
            "--agent builder=fixture:MODEL (no model named)",
            "--for DURATION"])  # nothing about a reviewer: it is not the developer's to supply
        with self.assertRaises(guided.Missing) as caught:
            self.complete(given(seconds=60.0))
        self.assertEqual(caught.exception.names, ["--agent driver=NAME:MODEL", "--agent builder=NAME:MODEL"])

    def test_a_model_given_by_flag_is_checked_before_it_is_repeated_in_any_message(self):
        for value in (given({"driver": "fixture:bad\x1b[2Jmodel"}), given({"builder": "fixture:-m"}),
                      given({"reviewer": "fixture:bad\x1b[2Jreview"}), given({"reviewer": "fixture:has space"})):
            with self.subTest(value=value), self.assertRaisesRegex(Refused, "invalid model ID") as caught:
                self.complete(value)
            self.assertNotIsInstance(caught.exception, guided.Missing)
            self.assertNotIn("\x1b", str(caught.exception))

    def test_a_named_agent_that_cannot_take_the_seat_refuses_as_before(self):
        for value in (given({"driver": "agy:gemini-4-carbon-high"}), given({"driver": "nosuch:model"})):
            with self.assertRaises(Refused) as caught:
                self.complete(value, Answers())
            self.assertNotIsInstance(caught.exception, guided.Missing)


class AskerTests(LocalTest):
    def asker(self, *lines):
        self.lines, self.out = list(lines), []
        return guided.asker(lambda: self.lines.pop(0) if self.lines else None, self.out.append)

    def test_a_number_picks_a_listed_option_and_a_typed_value_is_checked(self):
        ask = self.asker("2\n", " typed \n")
        self.assertEqual(ask("Driver agent", ["one", "two"], str), "two")
        self.assertEqual(ask("Driver model", ["one"], str), "typed")
        shown = "".join(self.out)
        self.assertIn("Driver agent (a number picks from the list)\n  1. one\n  2. two\n  Driver agent: ", shown)
        self.assertIn("\nDriver model (a number picks from the list)\n", shown)
        self.assertTrue(shown.endswith(": "))

    def test_an_unusable_answer_is_asked_again_three_times_at_most(self):
        def check(text):
            if text != "good":
                raise ValueError("not usable: " + text)
            return text
        self.assertEqual(self.asker("bad\n", "\n", "good\n")("Review model", [], check), "good")
        self.assertIn("not usable: bad", "".join(self.out))
        with self.assertRaisesRegex(Refused, "no usable answer"):
            self.asker("bad\n", "bad\n", "bad\n", "good\n")("Review model", [], check)
        self.assertEqual(self.lines, ["good\n"])

    def test_a_digit_that_is_no_decimal_number_is_one_unusable_answer_not_a_crash(self):
        def check(text):
            if text != "one":
                raise ValueError("not offered")
            return text
        self.assertEqual(self.asker("\u00b2\n", "\u0663\n", "1\n")("Driver agent", ["one", "two"], check), "one")

    def test_the_end_of_input_refuses_and_typed_text_is_never_printed_raw(self):
        with self.assertRaisesRegex(Refused, "no answer"):
            self.asker()("Driver agent", ["one"], str)

        def check(text):
            raise ValueError("refused " + text)
        with self.assertRaises(Refused):
            self.asker("\x1b[2Jx\n", "a\n", "b\n")("Driver agent", [], check)
        self.assertNotIn("\x1b", "".join(self.out))
