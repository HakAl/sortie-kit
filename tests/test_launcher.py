import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from sortie_kit import agents, cli, launcher
from sortie_kit.launcher import start
from sortie_kit.layout import Refused, Request
from support import LocalTest, agent_fixture


def plain_home(root):
    """A home whose one agent profile cannot review, and a search path with no agent CLI on
    it: what a machine with no reviewer looks like, whatever the machine running the test has."""
    home, files, search = Path(root) / "plain-home", Path(root) / "plain-files", Path(root) / "no-agents-on-path"
    directory = agents.user_directory(home)
    directory.mkdir(parents=True)
    files.mkdir()
    search.mkdir()
    program = files / "plain-agent"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    (directory / "plain.json").write_text(json.dumps(dict(
        version=1, name="plain", executable=os.path.realpath(program),
        interactive_argv=["--model", "{model}", "{prompt}"],
        headless_argv=["run", "--model", "{model}", "--cd", "{cwd}", "{prompt}"],
        models={"plain-model": "fixture-family"})))
    return home, str(search)


class TwoSeatCliTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.target = self.repo()
        self.args = ["start", "--repo", str(self.target), "--direction",
                     str(self.target / "direction.md"), "--for", "1m"]
        self.own_home()

    def test_two_seat_cli_prepares_exact_draft_and_profile(self):
        with patch("sortie_kit.agents.load", side_effect=AssertionError("no agent lookup")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(self.args + ["--prepare-only"]), 0)
        shift, = (self.target / ".sortie/shifts").iterdir()
        draft = json.loads((shift / "grant.draft.json").read_text())
        self.assertEqual(draft["seats"], {"driver": None, "builder": None})
        self.assertEqual(json.loads((shift / "profile.json").read_text())["roles"],
                         ["driver", "builder"])
        # A file every shift carries, admitted or not, must not call the shift preparation-only.
        self.assertNotIn("status", json.loads((shift / "profile.json").read_text()))
        self.assertFalse((shift / "runtime").exists())

    def test_pane_and_earlier_model_flags_refuse_with_a_pointer_before_preparation(self):
        for old in (["--driver", "w1:p1"], ["--builder", "w1:p2"], ["--runtime", "driver=codex"],
                    ["--builder-model", "m"], ["--builder-family", "gpt"], ["--review-model", "m"],
                    ["--review-family", "gemini"]):
            for mode in ("--prepare-only", "--sign-only"):
                with self.subTest(old=old, mode=mode), contextlib.redirect_stderr(io.StringIO()) as err, \
                        patch("sortie_kit.signing.terminal", side_effect=AssertionError("no terminal")):
                    self.assertEqual(cli.main(self.args + old + [mode]), 3)
                    self.assertIn("--agent ROLE=NAME:MODEL", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(self.args + ["--reviewer", "w1:p3", "--prepare-only"])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse((self.target / ".sortie").exists())

    def test_signing_lists_missing_choices_and_preparation_takes_none(self):
        both = ["--agent", "driver=claude:m", "--agent", "builder=codex:m"]
        listed = [(["--sign-only"], ["--trust FILE", "--key FILE", "--agent driver=NAME:MODEL",
                                     "--agent builder=NAME:MODEL"]),
                  (["--sign-only", "--agent", "driver=claude:m"], ["--agent builder=NAME:MODEL"]),
                  (["--sign-only", "--agent", "builder=claude"] + both[:2], ["--agent builder=claude:MODEL"]),
                  (["--sign-only"] + both, ["--trust FILE", "--key FILE"])]
        for extra, names in listed:
            # Nothing is asked when the trust file and key are not named: the start could not sign anyway.
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()) as err, \
                    patch("sortie_kit.signing.terminal", side_effect=AssertionError("no terminal")), \
                    patch("sortie_kit.cli.start", side_effect=AssertionError("not started")):
                self.assertEqual(cli.main(self.args + extra), 2)
                self.assertIn("sortie: missing: ", err.getvalue())
                for name in names:
                    self.assertIn(name, err.getvalue())
                # Who reviews is never something a start lacks: it is picked.
                self.assertNotIn("review", err.getvalue().lower())
        cases = [(["--sign-only"] + both + ["--agent", "driver=codex:m"], "each once"),
                 (["--sign-only", "--agent", "host=agy:m"] + both, "ROLE one of driver, builder, reviewer"),
                 (["--sign-only", "--agent", "driver"] + both[2:], "ROLE=VALUE"),
                 (["--sign-only", "--family", "host=gpt"] + both, "driver, builder, reviewer"),
                 (["--sign-only", "--family", "reviewer=gpt"] + both, "goes with --agent reviewer=NAME[:MODEL]"),
                 (["--prepare-only"] + both, "only when signing"),
                 (["--prepare-only", "--agent", "reviewer=claude"], "only when signing"),
                 (["--prepare-only", "--review-limit", "5m"], "only when signing"),
                 (["--prepare-only", "--family", "builder=gpt"], "only when signing"),
                 (["--prepare-only", "--allow-same-family-review"], "only when signing"),
                 (["--prepare-only", "--builder-network"], "only when signing")]
        for extra, message in cases:
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()) as err, \
                    patch("sortie_kit.signing.terminal", side_effect=AssertionError("no terminal")):
                self.assertEqual(cli.main(self.args + extra), 3)
                self.assertIn(message, err.getvalue())
        self.assertFalse((self.target / ".sortie").exists())

    def test_the_earlier_review_flags_are_refused_with_the_new_spelling(self):
        both = ["--agent", "driver=claude:m", "--agent", "builder=codex:m", "--trust", "t", "--key", "k"]
        cases = [(["--review", "gemini-4-carbon-high"], "use --agent reviewer=NAME[:MODEL]"),
                 (["--family", "review=gemini"], "--family reviewer=FAMILY, together with --agent reviewer=NAME[:MODEL]")]
        for extra, message in cases:
            for mode in ([], ["--sign-only"]):
                with self.subTest(extra=extra, mode=mode), contextlib.redirect_stderr(io.StringIO()) as err, \
                        patch("sortie_kit.signing.terminal", side_effect=AssertionError("no terminal")), \
                        patch("sortie_kit.cli.start", side_effect=AssertionError("not started")):
                    self.assertEqual(cli.main(self.args + both + mode + extra), 3)
                    self.assertIn("was replaced", err.getvalue())
                    self.assertIn(message, err.getvalue())
        # --review is refused wherever it is given; it is never read as the start of --review-limit.
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(self.args + ["--prepare-only", "--review", "r"]), 3)
        self.assertIn("--review MODEL was replaced", err.getvalue())
        self.assertFalse((self.target / ".sortie").exists())

    def guided(self, extra, typed=b"", terminal=None):
        """Run the start with a fixture profile home; returns (code, what start() got, what was shown)."""
        import socket
        if not hasattr(self, "home"):
            self.home = agent_fixture(self.root, self.target)["home"]
        home = self.home
        near, far = socket.socketpair()
        far.sendall(typed)
        far.shutdown(socket.SHUT_WR)
        seen = {}
        def started(request, **kw):
            seen.update(kw, request=request)
            return Path("shift")
        try:
            with patch.dict(os.environ, HOME=str(home)), patch("sortie_kit.cli.start", side_effect=started), \
                    patch("sortie_kit.signing.terminal", side_effect=terminal or (lambda: os.dup(near.fileno()))), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.main(extra)
            near.shutdown(socket.SHUT_WR)
            shown = b""
            while part := far.recv(65536):
                shown += part
            return code, seen, shown.decode() + err.getvalue()
        finally:
            near.close()
            far.close()

    def test_flags_become_choices_without_assuming_a_model_or_family(self):
        code, seen, _ = self.guided(self.args + [
            "--sign-only", "--trust", "t", "--key", "k", "--agent", "driver=claude:vendor:model@1",
            "--agent", "builder=fixture:new-model", "--agent", "reviewer=fixture:review-new",
            "--family", "reviewer=Gemini", "--family", "builder=gpt", "--review-limit", "5m",
            "--allow-same-family-review"],
            terminal=lambda: self.fail("every choice was given; nothing is asked"))
        self.assertEqual(code, 0)
        self.assertEqual(seen["choices"], {
            "driver": {"agent": "claude", "model": "vendor:model@1", "family": None},
            "builder": {"agent": "fixture", "model": "new-model", "family": "gpt"},
            "reviewer": {"agent": "fixture", "model": "review-new", "family": "gemini"}})
        self.assertEqual(seen["review_seconds"], 300)
        self.assertTrue(seen["same_family"] and seen["sign_only"])
        self.assertEqual(seen["request"].seconds, 60)
        self.assertNotIn("review", seen)

    def test_the_builder_network_flag_reaches_the_start_and_is_off_when_not_given(self):
        base = self.args + ["--sign-only", "--trust", "t", "--key", "k", "--agent", "driver=fixture:fixture-model",
                            "--agent", "builder=fixture:fixture-model"]
        nothing = lambda: self.fail("every choice was given; nothing is asked")
        code, seen, _ = self.guided(base, terminal=nothing)
        self.assertEqual((code, seen["builder_network"]), (0, False))
        # A builder profile without a form with the network on is refused before the start is
        # reached and before anything is asked, also when other choices are still open.
        for args in (base + ["--builder-network"],
                     self.args + ["--sign-only", "--trust", "t", "--key", "k", "--builder-network",
                                  "--agent", "builder=fixture:fixture-model"]):
            code, seen, shown = self.guided(args, terminal=lambda: self.fail("refused before any question"))
            self.assertEqual((code, seen), (3, {}), shown)
            self.assertIn("--builder-network was given, but the builder profile fixture has no form with the network on",
                          shown)
        # With the builder left open, only a profile that has the form is offered, and one without
        # it is refused when typed; the question is asked again and codex, which has it, is taken.
        open_builder = self.args + ["--sign-only", "--trust", "t", "--key", "k", "--builder-network",
                                    "--agent", "driver=fixture:fixture-model"]
        code, seen, shown = self.guided(open_builder, typed=b"fixture\ncodex\nm\n")
        self.assertEqual((code, seen["builder_network"], seen["choices"]["builder"]["agent"]), (0, True, "codex"), shown)
        self.assertIn("  1. codex\n", shown)
        for name in ("claude", "fixture", "agy"):
            self.assertNotIn(". " + name + "\n", shown)
        self.assertIn("the builder profile fixture has no form with the network on", shown)
        # Without the flag the same question offers them all.
        code, seen, shown = self.guided(open_builder[:-3] + open_builder[-2:], typed=b"fixture\nfixture-model\n")
        self.assertEqual((code, seen["builder_network"], seen["choices"]["builder"]["agent"]), (0, False, "fixture"), shown)
        for name in ("claude", "codex", "fixture"):
            self.assertIn(". " + name + "\n", shown)
        # A direct call with someone to ask: a builder named by flag is refused before the driver is asked about.
        from sortie_kit import guided
        with self.assertRaisesRegex(Refused, "builder profile claude has no form with the network on"):
            guided.complete({"agents": {"builder": "claude:m"}, "families": {}, "seconds": 60, "network": True},
                            self.target, ask=lambda *a: self.fail("asked before the refusal"), home=self.home)
        # A profile that has the form takes the flag.
        import json
        from sortie_kit import agents
        path = agents.user_directory(self.home) / "fixture.json"
        value = json.loads(path.read_text())
        value["headless_network_argv"] = ["run", "--net", "--model", "{model}", "--cd", "{cwd}", "{prompt}"]
        path.write_text(json.dumps(value))
        code, seen, _ = self.guided(base + ["--builder-network"], terminal=nothing)
        self.assertEqual((code, seen["builder_network"]), (0, True))
        # Two forms that are one command with this builder's model in its place: refused before
        # any question and before the start, also with the driver still open, and with the model
        # still open once it is typed.
        value["headless_argv"] = ["run", "--model", "{model}", "fixture-model", "{prompt}"]
        value["headless_network_argv"] = ["run", "--model", "fixture-model", "{model}", "{prompt}"]
        path.write_text(json.dumps(value))
        for args in (base + ["--builder-network"],
                     self.args + ["--sign-only", "--trust", "t", "--key", "k", "--builder-network",
                                  "--agent", "builder=fixture:fixture-model"]):
            code, seen, shown = self.guided(args, terminal=lambda: self.fail("refused before any question"))
            self.assertEqual((code, seen), (3, {}), shown)
            self.assertIn("with model fixture-model the builder profile fixture's form with the network on is the "
                          "same command as its form without", shown)
        with self.assertRaisesRegex(Refused, "with model fixture-model .* is the same command as its form without"):
            guided.complete({"agents": {"driver": "fixture:fixture-model", "builder": "fixture"}, "families": {},
                             "seconds": 60, "network": True}, self.target, ask=lambda *a: "fixture-model", home=self.home)
        value["headless_argv"] = ["run", "--model", "{model}", "--cd", "{cwd}", "{prompt}"]
        value["headless_network_argv"] = ["run", "--net", "--model", "{model}", "--cd", "{cwd}", "{prompt}"]
        path.write_text(json.dumps(value))
        # No guided question asks for it: with the seats left open, what is asked never names the network.
        code, seen, shown = self.guided(self.args + ["--sign-only", "--trust", "t", "--key", "k"],
                                        typed=b"fixture\nfixture-model\nfixture\nfixture-model\n")
        self.assertEqual((code, seen["builder_network"]), (0, False), shown)
        self.assertNotIn("network", shown.lower())

    def test_a_live_start_is_given_a_hold_against_idle_sleep_and_no_wake_turns_it_off(self):
        live = self.args + ["--trust", "t", "--key", "k", "--agent", "driver=fixture:fixture-model",
                            "--agent", "builder=fixture:fixture-model"]
        nothing = lambda: self.fail("every choice was given; nothing is asked")
        code, seen, _ = self.guided(live + ["--sign-only"], terminal=nothing)
        self.assertIsNone(seen.get("wake"))  # a signing on its own runs nothing

        def ran(extra):
            """A start that runs the shift: what `start` was given."""
            got = {}

            def started(request, **kw):
                got.update(kw)
                return self.root / "shift", 0, "ended", "stopped by the host", True
            with patch.dict(os.environ, HOME=str(self.home)), patch("sortie_kit.cli.start", side_effect=started), \
                    patch("sortie_kit.signing.terminal", side_effect=nothing), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(live + extra), 0)
            return got
        seen = ran([])
        self.assertTrue(seen["wake"].record()["asked"])
        self.assertFalse(seen["wake"].began)  # built here; taken only once the shift is admitted
        seen = ran(["--no-wake"])
        self.assertEqual((seen["wake"].record()["asked"], seen["wake"].record()["means"]), (False, "off: --no-wake was given"))
        # Nothing runs after a preparation or a signing on its own: the flag is refused there.
        for mode in ("--sign-only", "--prepare-only"):
            with self.subTest(mode=mode):
                code, seen, shown = self.guided(live + [mode, "--no-wake"], terminal=nothing)
                self.assertEqual((code, seen), (3, {}))
                self.assertIn("--no-wake applies only to a start that runs the shift", shown)

    def test_without_a_reviewer_flag_none_is_passed_on_and_the_start_picks(self):
        code, seen, _ = self.guided(self.args + [
            "--sign-only", "--trust", "t", "--key", "k", "--agent", "driver=fixture:fixture-model",
            "--agent", "builder=fixture:fixture-model"],
            terminal=lambda: self.fail("every choice was given; nothing is asked"))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(seen["choices"]), ["builder", "driver"])
        self.assertEqual((seen["review_seconds"], seen["same_family"]), (None, False))
        # A reviewer named without a model takes its profile's review model at the pick.
        code, seen, _ = self.guided(self.args + [
            "--sign-only", "--trust", "t", "--key", "k", "--agent", "driver=fixture:fixture-model",
            "--agent", "builder=fixture:fixture-model", "--agent", "reviewer=fixture"],
            terminal=lambda: self.fail("every choice was given; nothing is asked"))
        self.assertEqual(code, 0)
        self.assertEqual(seen["choices"]["reviewer"], {"agent": "fixture", "model": None, "family": None})

    def test_missing_choices_are_asked_in_the_terminal_before_anything_is_prepared(self):
        base = self.args[:-2] + ["--sign-only", "--trust", "t", "--key", "k"]  # no --for either
        code, seen, shown = self.guided(base + ["--agent", "driver=fixture:fixture-model"],
                                        b"fixture\n1\n45m\n")
        self.assertEqual(code, 0, shown)
        self.assertEqual(seen["choices"]["builder"], {"agent": "fixture", "model": "fixture-model", "family": None})
        self.assertNotIn("reviewer", seen["choices"])
        self.assertEqual((seen["request"].seconds, seen["sign_only"], seen["prepare_only"]), (2700.0, True, False))
        for part in ("Some choices were not given.", "\nBuilder agent (an installed agent profile; a number picks from the list)\n",
                     "  1. fixture-model\n", "How long may the shift run (for example 45m or 2h)\n"):
            self.assertIn(part, shown)
        self.assertNotIn("Driver agent", shown)
        # Who reviews is shown at signing and never asked.
        self.assertNotIn("Review", shown)
        self.assertFalse((self.target / ".sortie").exists())

    def test_without_a_terminal_missing_choices_exit_2_and_are_listed(self):
        def none():
            raise Refused("sign only in the developer's own terminal; redirected streams refused")
        base = self.args[:-2] + ["--trust", "t", "--key", "k"]
        code, seen, shown = self.guided(base + ["--agent", "driver=fixture:fixture-model"], terminal=none)
        self.assertEqual((code, seen), (2, {}))
        for part in ("sortie: missing: --agent builder=NAME:MODEL; --for DURATION",
                     "no terminal to ask in"):
            self.assertIn(part, shown)
        # Unanswered questions end the start; they are never filled in.
        code, seen, shown = self.guided(base + ["--agent", "driver=fixture:fixture-model"], b"fixture\n")
        self.assertEqual((code, seen), (3, {}))
        self.assertIn("no answer for: Builder model", shown)
        self.assertFalse((self.target / ".sortie").exists())

    def test_start_uses_the_trust_file_and_key_where_trust_init_writes_them(self):
        from sortie_kit import trust_init
        home = self.home = agent_fixture(self.root, self.target)["home"]
        trust, key = trust_init.default_trust(home), trust_init.default_key(home)
        full = self.args + ["--sign-only", "--agent", "driver=fixture:fixture-model",
                            "--agent", "builder=fixture:fixture-model"]
        nothing_asked = lambda: self.fail("every choice was given; nothing is asked")
        # Neither file is there: both are listed as missing, as before.
        code, seen, shown = self.guided(full, terminal=nothing_asked)
        self.assertEqual((code, seen), (2, {}))
        self.assertIn("--trust FILE", shown)
        self.assertIn("--key FILE", shown)
        # Only one of the two is there: the other is still missing, and the message says where it was looked for.
        trust.parent.mkdir(parents=True, exist_ok=True)
        trust.write_text("{}")
        code, seen, shown = self.guided(full, terminal=nothing_asked)
        self.assertEqual((code, seen), (2, {}))
        self.assertNotIn("--trust FILE", shown)
        self.assertIn("--key FILE", shown)
        self.assertIn(str(key), shown)
        # Both are there: the start takes them and says which files it took.
        key.write_text("not read here")
        code, seen, shown = self.guided(full, terminal=nothing_asked)
        self.assertEqual(code, 0, shown)
        self.assertEqual((seen["trust_path"], seen["key"]), (trust, key))
        self.assertIn(str(trust), shown)
        self.assertIn(str(key), shown)
        # A flag still decides, each on its own.
        code, seen, shown = self.guided(full + ["--trust", "t"], terminal=nothing_asked)
        self.assertEqual((code, seen["trust_path"], seen["key"]), (0, Path("t"), key))
        code, seen, shown = self.guided(full + ["--trust", "t", "--key", "k"], terminal=nothing_asked)
        self.assertEqual((code, seen["trust_path"], seen["key"]), (0, Path("t"), Path("k")))
        self.assertNotIn(str(trust), shown)
        # A directory or a dangling link of that name is not a file to use.
        key.unlink()
        key.mkdir()
        code, seen, shown = self.guided(full, terminal=nothing_asked)
        self.assertEqual((code, seen), (2, {}))
        self.assertIn("--key FILE", shown)

    def test_no_agent_that_can_review_refuses_before_any_question_is_asked(self):
        self.home, search = plain_home(self.root)
        base = self.args[:-2] + ["--trust", "t", "--key", "k"]
        for mode in ([], ["--sign-only"]):
            with self.subTest(mode=mode), patch.dict(os.environ, PATH=search):
                code, seen, shown = self.guided(base + mode, b"plain\n1\nplain\n1\n45m\n")
                self.assertEqual((code, seen), (3, {}))
                self.assertIn("no installed agent can review", shown)
                self.assertIn("--agent reviewer=NAME", shown)
                self.assertIn("nothing was prepared or signed", shown)
                for asked in ("Some choices were not given", "Driver agent", "How long"):
                    self.assertNotIn(asked, shown)
        self.assertFalse((self.target / ".sortie").exists())
        # With nobody to ask, what is missing is still listed first: that start could not go on anyway.
        def none():
            raise Refused("redirected streams refused")
        with patch.dict(os.environ, PATH=search):
            code, seen, shown = self.guided(base, terminal=none)
        self.assertEqual((code, seen), (2, {}))
        self.assertIn("sortie: missing: --agent driver=NAME:MODEL", shown)

    def test_conflicting_modes_refuse_before_any_question(self):
        for extra in ([], ["--trust", "t", "--key", "k"]):
            code, seen, shown = self.guided(self.args[:-2] + ["--prepare-only", "--sign-only"] + extra,
                                            terminal=lambda: self.fail("nothing is asked"))
            self.assertEqual((code, seen), (3, {}))
            self.assertIn("cannot be combined", shown)

    def test_refusals_are_printed_without_control_characters(self):
        code, seen, shown = self.guided(self.args + ["--sign-only", "--trust", "t", "--key", "k",
                                                     "--agent", "driver=fixture:fixture-model",
                                                     "--agent", "builder=fixture:fixture-model",
                                                     "--agent", "reviewer=fixture:r\x1b[2J"],
                                        terminal=lambda: self.fail("nothing is asked"))
        self.assertEqual((code, seen), (3, {}))
        self.assertIn("invalid model ID", shown)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(self.args + ["--sign-only", "--agent", "driver=x\x1b[2J:m"]), 3)
        self.assertNotIn("\x1b", shown + err.getvalue())

    def test_preparation_never_asks_and_needs_its_duration(self):
        code, seen, shown = self.guided(self.args[:-2] + ["--prepare-only"],
                                        terminal=lambda: self.fail("preparation asks nothing"))
        self.assertEqual((code, seen), (2, {}))
        self.assertIn("sortie: missing: --for DURATION", shown)


class LauncherTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.target = self.repo()
        self.request = Request(self.target, self.target / "direction.md", 30)
        self.own_home()

    def exclude(self):
        return self.target / ".git/info/exclude"

    def test_offline_preparation_leaves_gitignore_alone_and_packages_templates(self):
        (self.target / ".gitignore").write_text("existing-rule")
        with patch("sortie_kit.agents.load", side_effect=AssertionError("no agent lookup")):
            shift = start(self.request, prepare_only=True)
        # The repository's own untracked exclude file hides .sortie; no file a commit could carry changes.
        self.assertEqual((self.target / ".gitignore").read_text(), "existing-rule")
        self.assertEqual(self.exclude().read_text().splitlines().count("/.sortie/"), 1)
        draft = json.loads((shift / "grant.draft.json").read_text())
        self.assertEqual(draft["allowed_actions"], [])
        self.assertEqual(draft["status"], "UNSIGNED_PREPARATION_ONLY")
        self.assertFalse((shift / "grant.json").exists())
        self.assertEqual((shift / "direction.md").read_bytes(), self.request.direction.read_bytes())
        for name in ("state.md", "completion.md", "profile.json", "sources.json"):
            self.assertTrue((shift / name).is_file(), name)
        # Preparation writes no journal row; nothing reads one.
        self.assertFalse((shift / "events.jsonl").exists())
        self.assertNotIn("pane", (shift / "state.md").read_text())
        self.assertNotIn("events.jsonl", (shift / "state.md").read_text())
        # Seats read this file after admission: it must not state a status that is no longer true.
        self.assertNotIn("Status: prepared", (shift / "state.md").read_text())
        self.assertIn("written once, when the shift is prepared", (shift / "state.md").read_text())
        self.assertNotIn("`sortie ", (shift / "state.md").read_text())
        tracked = subprocess.run(["git", "-C", str(self.target), "status", "--short", "--untracked-files=all"],
                                 check=True, capture_output=True, text=True, timeout=5).stdout
        self.assertNotIn(".sortie/", tracked)
        self.assertEqual(tracked.split(), ["??", ".gitignore", "??", "direction.md"])
        second = start(self.request, prepare_only=True)
        self.assertNotEqual(shift, second)
        self.assertEqual(self.exclude().read_text().splitlines().count("/.sortie/"), 1)

    def test_the_pane_transport_and_the_runtime_starters_are_gone(self):
        """Nothing of the pane era is importable, packaged or offered."""
        import contextlib
        import importlib
        import io
        from importlib.resources import files
        from sortie_kit import cli
        for name in ("send", "wait", "tick", "runtime", "journal"):
            with self.subTest(module=name), self.assertRaises(ModuleNotFoundError):
                importlib.import_module("sortie_kit." + name)
        self.assertFalse(files("sortie_kit").joinpath("data", "runtime").is_dir())
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as refused:
            cli.main(["runtime", "example", "--runtime", "codex", "--role", "driver", "--part", "config"])
        self.assertEqual(refused.exception.code, 2)
        self.assertIn("invalid choice", err.getvalue())
        importlib.import_module("sortie_kit.tick_supervise")  # kept: the seats' process ownership

    def test_a_repository_that_already_ignores_sortie_is_not_written_to(self):
        (self.target / ".gitignore").write_text("/.sortie/\n")
        before = self.exclude().read_bytes() if self.exclude().exists() else None
        start(self.request, prepare_only=True)
        self.assertEqual((self.target / ".gitignore").read_text(), "/.sortie/\n")
        self.assertEqual(self.exclude().read_bytes() if self.exclude().exists() else None, before)

    def test_exclude_file_is_created_when_missing_and_keeps_existing_lines(self):
        self.exclude().unlink(missing_ok=True)
        start(self.request, prepare_only=True)
        self.assertEqual(self.exclude().read_text(), "/.sortie/\n")
        self.exclude().write_text("mine")
        start(self.request, prepare_only=True)
        self.assertEqual(self.exclude().read_text(), "mine\n/.sortie/\n")

    def test_live_start_refuses_without_trust_key_or_choices_before_anything_is_written(self):
        seat = {"agent": "claude", "model": "m"}
        both = {"driver": seat, "builder": seat}
        cases = [(dict(), "starting a shift requires --trust and --key"),
                 (dict(trust_path=self.root / "t", key=self.root / "k"), "name an agent"),
                 (dict(trust_path=self.root / "t", key=self.root / "k", choices=both, review_seconds=5),
                  "review limit")]
        for kwargs, message in cases:
            with self.subTest(message=message), \
                    patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
                    patch("subprocess.Popen", side_effect=AssertionError("no agent started")), \
                    patch("sortie_kit.signing.terminal", side_effect=AssertionError("no terminal")), \
                    self.assertRaisesRegex(Refused, message):
                start(self.request, **kwargs)
        self.assertFalse((self.target / ".sortie").exists())

    def test_live_start_refuses_outside_a_terminal_before_preparation(self):
        seat = {"agent": "claude", "model": "m"}
        with patch("os.isatty", return_value=False), \
                patch("subprocess.Popen", side_effect=AssertionError("no agent started")), \
                self.assertRaisesRegex(Refused, "terminal"):
            start(self.request, trust_path=self.root / "t", key=self.root / "k",
                  choices={"driver": seat, "builder": seat})
        self.assertFalse((self.target / ".sortie").exists())

    def test_live_start_refuses_a_terminal_the_driver_cannot_be_given_before_anything_is_signed(self):
        import os
        seat = {"agent": "claude", "model": "m"}
        kwargs = dict(trust_path=self.root / "t", key=self.root / "k",
                      choices={"driver": seat, "builder": seat})
        refusal = Refused("the terminal's own device name could not be obtained")
        with patch("sortie_kit.signing.terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                patch("sortie_kit.tick_supervise.own_terminal", side_effect=refusal), \
                patch("sortie_kit.signing.sign", side_effect=AssertionError("nothing may be signed")), \
                patch("subprocess.Popen", side_effect=AssertionError("no agent started")), \
                self.assertRaisesRegex(Refused, "device name"):
            start(self.request, **kwargs)
        self.assertFalse((self.target / ".sortie").exists())
        # Signing alone starts no driver, so it asks nothing of the driver's terminal.
        with patch("sortie_kit.signing.terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                patch("sortie_kit.tick_supervise.own_terminal", side_effect=AssertionError("not for sign-only")), \
                self.assertRaisesRegex((Refused, OSError), "^(?!.*not for sign-only)"):
            start(self.request, sign_only=True, **kwargs)

    def test_start_refuses_before_preparing_or_signing_when_no_installed_agent_can_review(self):
        import os
        home, search = plain_home(self.root)
        seat = {"agent": "plain", "model": "plain-model"}
        kwargs = dict(trust_path=self.root / "t", key=self.root / "k", choices={"driver": seat, "builder": seat})
        for mode in (dict(sign_only=True), dict()):
            with self.subTest(mode=mode), \
                    patch("sortie_kit.signing.terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                    patch("sortie_kit.tick_supervise.own_terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                    patch("sortie_kit.grants.Trust"), \
                    patch("sortie_kit.launcher.prepare", side_effect=AssertionError("nothing may be prepared")), \
                    patch("sortie_kit.signing.sign", side_effect=AssertionError("nothing may be signed")), \
                    patch("sortie_kit.launcher.run", side_effect=AssertionError("no shift is run")), \
                    self.assertRaises(Refused) as raised:
                start(self.request, home=home, search=search, **mode, **kwargs)
            message = str(raised.exception)
            self.assertIn("no installed agent can review", message)
            # The names it looked at, and what to do, in the one sentence.
            for part in ("claude, codex, agy, plain", "--agent reviewer=NAME", "nothing was prepared or signed"):
                self.assertIn(part, message)
            self.assertNotIn("\n", message)
            self.assertNotIn("skill", message)
        self.assertFalse((self.target / ".sortie").exists())
        # One installed agent that can review is enough; the start then goes on to prepare.
        fixture = agent_fixture(self.root, self.target)
        home = fixture["home"]
        kwargs["choices"] = {role: fixture["choices"][role] for role in ("driver", "builder")}
        with patch("sortie_kit.signing.terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                patch("sortie_kit.grants.Trust"), \
                patch("sortie_kit.launcher.prepare", side_effect=Refused("preparation was reached")), \
                self.assertRaisesRegex(Refused, "preparation was reached"):
            start(self.request, sign_only=True, home=home, search=search, **kwargs)
        seats, choice = launcher.all_seats(self.target, kwargs["choices"], home=home, search=search)
        self.assertEqual((seats["reviewer"]["agent"], seats["reviewer"]["model"], seats["reviewer"]["mode"], choice),
                         ("fixture", "fixture-review-model", "review", "picked-other-family"))
        self.assertEqual(seats["reviewer"]["executable"]["path"], str(fixture["program"]))
        # Preparation alone signs nothing and runs no review, so it needs no reviewer.
        bare = self.root / "bare-home"
        bare.mkdir()
        self.assertTrue(start(self.request, prepare_only=True, home=bare, search=search).is_dir())

    def test_builder_network_is_signed_into_the_builder_argv_or_refused_before_anything_is_prepared(self):
        import json
        import os
        from sortie_kit import agents
        fixture = agent_fixture(self.root, self.target)
        home = fixture["home"]
        # The fixture profile has no form with the network on: refused, in both modes, with nothing made.
        kwargs = dict(trust_path=self.root / "t", key=self.root / "k", choices=fixture["choices"], home=home)
        for mode in (dict(sign_only=True), dict()):
            with self.subTest(mode=mode), \
                    patch("sortie_kit.signing.terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                    patch("sortie_kit.tick_supervise.own_terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                    patch("sortie_kit.grants.Trust"), \
                    patch("sortie_kit.launcher.prepare", side_effect=AssertionError("nothing may be prepared")), \
                    patch("sortie_kit.signing.sign", side_effect=AssertionError("nothing may be signed")), \
                    patch("sortie_kit.launcher.run", side_effect=AssertionError("no shift is run")), \
                    self.assertRaises(Refused) as raised:
                start(self.request, builder_network=True, **mode, **kwargs)
            message = str(raised.exception)
            for part in ("--builder-network was given", "builder profile fixture has no form with the network on",
                         "nothing was prepared or signed"):
                self.assertIn(part, message)
            self.assertNotIn("\n", message)
        self.assertFalse((self.target / ".sortie").exists())
        # A profile that has the form: the flag puts that whole form into the builder's seat and no other.
        path = agents.user_directory(home) / "fixture.json"
        value = json.loads(path.read_text())
        value["headless_network_argv"] = ["run", "--net", "--model", "{model}", "--cd", "{cwd}", "{prompt}"]
        path.write_text(json.dumps(value))
        made = {}
        off, _ = launcher.all_seats(self.target, fixture["choices"], home=home)
        on, _ = launcher.all_seats(self.target, fixture["choices"], home=home, builder_network=True)
        self.assertEqual(off["builder"]["argv"], value["headless_argv"])
        with patch("sortie_kit.signing.terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                patch("sortie_kit.grants.Trust"), \
                patch("sortie_kit.grants.make_grant", side_effect=lambda shift, trust, seats, seconds, **kw: made.update(plain=seats) or b"{}"), \
                patch("sortie_kit.signing.sign"):
            start(self.request, sign_only=True, **kwargs)
        self.assertEqual(made["plain"]["builder"]["argv"], value["headless_argv"])
        self.assertEqual(on["builder"]["argv"], value["headless_network_argv"])
        self.assertEqual((on["driver"], on["reviewer"]), (off["driver"], off["reviewer"]))
        # And the start hands those seats to the grant that is signed.
        with patch("sortie_kit.signing.terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                patch("sortie_kit.grants.Trust"), \
                patch("sortie_kit.grants.make_grant", side_effect=lambda shift, trust, seats, seconds, **kw: made.update(seats=seats) or b"{}"), \
                patch("sortie_kit.signing.sign"):
            start(self.request, sign_only=True, builder_network=True, **kwargs)
        self.assertEqual(made["seats"]["builder"]["argv"], value["headless_network_argv"])

    def test_a_network_form_that_is_the_same_command_for_this_start_is_refused_before_any_program_is_looked_for(self):
        import json
        from sortie_kit import agents
        fixture = agent_fixture(self.root, self.target)
        home = fixture["home"]
        path = agents.user_directory(home) / "fixture.json"
        value = json.loads(path.read_text())
        model = fixture["choices"]["builder"]["model"]
        where = str(self.target)
        # With the builder's model, and then with the repository's path, in its place.
        for plain, wide in ((["run", "--model", "{model}", model, "{prompt}"], ["run", "--model", model, "{model}", "{prompt}"]),
                            (["run", "--model", "{model}", "--at", "{cwd}", where, "{prompt}"],
                             ["run", "--model", "{model}", "--at", where, "{cwd}", "{prompt}"])):
            with self.subTest(wide=wide):
                value["headless_argv"], value["headless_network_argv"] = plain, wide
                path.write_text(json.dumps(value))
                with patch("sortie_kit.agents.pin_executable", side_effect=AssertionError("no program may be looked for")), \
                        self.assertRaisesRegex(Refused, "is the same command as its form without; nothing was prepared or signed"):
                    launcher.all_seats(self.target, fixture["choices"], home=home, builder_network=True)
                off, _ = launcher.all_seats(self.target, fixture["choices"], home=home)  # without the flag it starts
                self.assertEqual(off["builder"]["argv"], plain)

    def test_builder_network_names_only_the_builder_and_is_refused_for_a_preparation(self):
        # codex in every seat, with a stand-in program: only the builder's command changes.
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "codex").write_text("#!/bin/sh\nexit 0\n")
        (tools / "codex").chmod(0o755)
        home = self.root / "empty-home"
        home.mkdir()
        seat = {"agent": "codex", "model": "m"}
        choices = {"driver": seat, "builder": seat, "reviewer": seat}
        option = "sandbox_workspace_write.network_access=true"
        off, _ = launcher.all_seats(self.target, choices, same_family=False, home=home, search=str(tools))
        on, _ = launcher.all_seats(self.target, choices, builder_network=True, home=home, search=str(tools))
        rest = ["--model", "{model}", "--cd", "{cwd}", "{prompt}"]
        self.assertEqual(off["builder"]["argv"], ["exec", "--sandbox", "workspace-write", "--add-dir", ".git"] + rest)
        self.assertEqual(on["builder"]["argv"],
                         ["exec", "--sandbox", "workspace-write", "--add-dir", ".git", "-c", option] + rest)
        self.assertEqual((on["driver"], on["reviewer"]), (off["driver"], off["reviewer"]))
        for role in ("driver", "reviewer"):
            self.assertNotIn(option, on[role]["argv"])
        # A builder profile without the form is refused before any seat's program is looked for:
        # here neither the driver's nor the builder's is installed.
        empty = self.root / "no-tools"
        empty.mkdir()
        with self.assertRaisesRegex(Refused, "builder profile claude has no form with the network on"):
            launcher.signed_seats(self.target, {"driver": seat, "builder": {"agent": "claude", "model": "m"}},
                                  builder_network=True, home=home, search=str(empty))
        with self.assertRaisesRegex(Refused, "builder profile agy has no form with the network on"):
            launcher.signed_seats(self.target, {"driver": seat, "builder": {"agent": "agy", "model": "gemini-4-neon-high"}},
                                  builder_network=True, home=home, search=str(empty))
        # A profile with no headless start at all is refused for that, with the flag as without it.
        agents.user_directory(home).mkdir(parents=True)
        (agents.user_directory(home) / "no-builder.json").write_text(json.dumps(dict(
            version=1, name="no-builder", executable="no-builder-agent", headless_argv=None,
            interactive_argv=["--model", "{model}", "{prompt}"], models={"fixture-model": "fixture-family"})))
        with self.assertRaisesRegex(Refused, "no-builder has no headless start and cannot be the builder"):
            launcher.signed_seats(self.target, {"driver": seat, "builder": {"agent": "no-builder", "model": "fixture-model"}},
                                  builder_network=True, home=home, search=str(empty))
        # A preparation signs nothing and names no seat: the request is refused, not dropped.
        with self.assertRaisesRegex(Refused, "--builder-network applies only when signing"):
            start(self.request, prepare_only=True, builder_network=True)
        self.assertFalse((self.target / ".sortie").exists())

    def test_the_review_limit_is_a_whole_number_of_seconds_from_thirty_to_an_hour(self):
        seat = {"agent": "claude", "model": "m"}
        kwargs = dict(sign_only=True, trust_path=self.root / "t", key=self.root / "k",
                      choices={"driver": seat, "builder": seat})
        for seconds in (cli.finite("29s"), cli.finite("2h"), cli.finite("45.5s"), 0, -30, True, "600"):
            with self.subTest(seconds=seconds), \
                    patch("sortie_kit.signing.terminal", side_effect=AssertionError("no terminal")), \
                    self.assertRaisesRegex(Refused, "review limit must be a whole number of seconds from 30 to 3600"):
                start(self.request, review_seconds=seconds, **kwargs)
        # A limit that is taken lets the start go on to the terminal; none given is ten minutes.
        for seconds in (cli.finite("30s"), cli.finite("1h"), cli.finite("0.5m"), None):
            with self.subTest(seconds=seconds), \
                    patch("sortie_kit.signing.terminal", side_effect=Refused("the terminal was reached")), \
                    self.assertRaisesRegex(Refused, "the terminal was reached"):
                start(self.request, review_seconds=seconds, **kwargs)
        from sortie_kit import grants
        self.assertEqual((grants.REVIEW_LIMIT, grants.review_limit(600.0)), (600, 600))
        self.assertFalse((self.target / ".sortie").exists())

    def test_start_refuses_a_seat_whose_program_is_not_there_before_anything_is_prepared(self):
        import os
        fixture = agent_fixture(self.root, self.target)
        fixture["program"].unlink()
        with patch("sortie_kit.signing.terminal", side_effect=lambda: os.open(os.devnull, os.O_RDONLY)), \
                patch("sortie_kit.grants.Trust"), \
                patch("sortie_kit.signing.sign", side_effect=AssertionError("nothing may be signed")), \
                self.assertRaises((Refused, OSError)) as raised:
            start(self.request, sign_only=True, home=fixture["home"], trust_path=self.root / "t",
                  key=self.root / "k", choices=fixture["choices"])
        self.assertIn("fixture-agent", str(raised.exception))
        self.assertFalse((self.target / ".sortie").exists())

    def test_no_path_under_a_skills_directory_is_looked_up(self):
        for name in ("review_script", "SKILL_ROOTS", "reviewer"):
            self.assertFalse(hasattr(launcher, name), name)
        fixture = agent_fixture(self.root, self.target)
        self.assertFalse((fixture["home"] / ".agents").exists())
        seats, choice = launcher.all_seats(self.target, fixture["choices"], home=fixture["home"])
        self.assertEqual((seats["reviewer"]["agent"], seats["reviewer"]["mode"], choice), ("fixture", "review", "given"))

    def test_cli_live_start_names_what_is_missing(self):
        args = ["start", "--repo", str(self.target), "--direction", str(self.request.direction), "--for", "1m"]
        both = ["--agent", "driver=claude:m", "--agent", "builder=codex:m"]
        for extra, message in (([], "--agent driver=NAME:MODEL"), (both, "missing: --trust FILE")):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()) as err, \
                    patch("sortie_kit.signing.terminal", side_effect=AssertionError("no terminal")):
                self.assertEqual(cli.main(args + extra), 2)
                self.assertIn(message, err.getvalue())
        self.assertFalse((self.target / ".sortie").exists())

    def test_cli_says_so_when_the_shift_ended_without_a_report(self):
        import hashlib
        shift = self.root / "shift"; shift.mkdir()
        args = ["start", "--repo", str(self.target), "--direction", str(self.request.direction), "--for", "1m",
                "--agent", "driver=claude:m", "--agent", "builder=codex:m", "--trust", "t", "--key", "k"]
        def said(published=True):
            ended = (shift, 3, "duration elapsed", "stopped by the host", published)
            with patch("sortie_kit.cli.start", return_value=ended), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(args), 3)
            return out.getvalue()
        def pair(cleanup, digest=None):
            raw = json.dumps({"actual_window": {"processes": cleanup}}).encode()
            (shift / "report.json").write_bytes(raw)
            (shift / "report.md").write_text("# Report\n\nSHA256 of report.json: "
                                             + (digest or hashlib.sha256(raw).hexdigest()) + "\n")
        self.assertIn("No report was written", said())
        self.assertNotIn("Report: ", said())
        final = "\nReport: " + str(shift / "report.md")
        pair({"cleanup": "done"})
        self.assertIn(final, said())
        pair({"cleanup": "incomplete: builder output drain failed"})
        self.assertIn(final, said())
        # The host says its final report was not written: whatever is on disk is an earlier one.
        self.assertNotIn(final, said(published=False))
        self.assertIn("Preliminary report", said(published=False))
        # A report from before the host's cleanup, an unreadable summary, or Markdown that
        # does not belong to the summary beside it, is not presented as the final one.
        for name, make in (("early", lambda: pair({"cleanup": "not yet performed"})),
                           ("no processes", lambda: pair(3)),
                           ("other summary", lambda: pair({"cleanup": "done"}, digest="0" * 64)),
                           ("torn summary", lambda: (shift / "report.json").write_text("{")),
                           ("no summary", lambda: (shift / "report.json").unlink())):
            with self.subTest(name=name):
                make()
                text = said()
                self.assertNotIn(final, text)
                self.assertIn("Preliminary report", text)
                self.assertIn("sortie report --shift " + str(shift), text)

    def test_cli_last_lines_say_the_tasks_and_the_shutdown_apart(self):
        import hashlib
        from sortie_kit import report
        shift = self.root / "shift"; shift.mkdir()
        args = ["start", "--repo", str(self.target), "--direction", str(self.request.direction), "--for", "1m",
                "--agent", "driver=claude:m", "--agent", "builder=codex:m", "--trust", "t", "--key", "k"]
        tasks = [dict(id="version", acceptance=report.ACCEPTED, candidate=dict(kind="commit", commit="aed7b35a60" * 4)),
                 dict(id="other", acceptance="not accepted", candidate=None)]
        def said(code, seen, shutdown, published=True, known=None, left=None):
            raw = json.dumps(dict(tasks=tasks, not_started=left, actual_window=dict(processes=dict(
                cleanup="done", driver=seen, **({} if shutdown is None else {"shutdown": shutdown}))))).encode()
            (shift / "report.json").write_bytes(raw)
            (shift / "report.md").write_text("# Report\n\nSHA256 of report.json: " + hashlib.sha256(raw).hexdigest() + "\n")
            ended = (shift, code, "Task version accepted and closed.", seen, published) + (
                () if known is None else (known,))
            with patch("sortie_kit.cli.start", return_value=ended), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(args), code)
            return out.getvalue()
        head = ("\nShift ended: Task version accepted and closed.\nTasks: 1 accepted locally of 2 briefed (version at "
                "aed7b35); whether the direction held more is not known to the host.\n")
        tail = "Report: " + str(shift / "report.md") + "\n"
        self.assertEqual(said(0, "stopped by the host", dict(confirmed=True, groups=[], problems=[])),
                         head + "Shutdown: confirmed.\nDriver process: stopped by the host\n" + tail)
        # What the driver stated about the rest of the direction ends the Tasks line.
        for left, words in (([dict(name="pad\x1b[31m", reason="no time")], "the driver names 1 more as not started"),
                            ([], "the driver says the direction held no more"),
                            ([dict(name="pad")], "whether the direction held more is not known to the host")):
            self.assertIn("\nTasks: 1 accepted locally of 2 briefed (version at aed7b35); " + words + ".\n",
                          said(0, "stopped by the host", dict(confirmed=True, groups=[], problems=[]), left=left))
        seen = "stop signaled; exit not observed; its process group was not observed gone"
        problem = "the driver process was not seen to stop (" + seen + ")"
        self.assertEqual(said(3, seen, dict(confirmed=False, groups=[4242], problems=[problem])),
                         head + "Shutdown: NOT CONFIRMED. The driver process was not seen to stop (" + seen + "). "
                         "Check for leftover processes; no line printed means none are left: "
                         "ps -A -o pid,pgid,stat,command | awk '$2 == 4242'\n"
                         "Exit status 3 is about this shutdown, not about the tasks.\n"
                         "Driver process: " + seen + "\n" + tail)
        # A report from an earlier toolkit has no shutdown entry: the driver line decides.
        self.assertIn("\nShutdown: NOT CONFIRMED. The driver process was not seen to stop", said(3, seen, None))
        # Without this host's final report nothing kept says how the shutdown went.
        text = said(3, seen, dict(confirmed=True, groups=[], problems=[]), published=False)
        self.assertIn("\nShutdown: not known; the final report was not written.\nDriver process: " + seen + "\n", text)
        self.assertNotIn("Tasks:", text)
        self.assertNotIn("Shutdown: confirmed", text)
        # What the host itself saw reaches the terminal whether or not a report was written,
        # and it is the host's word, not the kept file's, that the Shutdown line repeats.
        known = dict(confirmed=False, groups=[77], problems=["a builder turn was not seen to stop"])
        for published in (True, False):
            text = said(3, "stopped by the host", dict(confirmed=True, groups=[], problems=[]), published, known)
            self.assertIn("\nShutdown: NOT CONFIRMED. A builder turn was not seen to stop. Check for leftover "
                          "processes; no line printed means none are left: ps -A -o pid,pgid,stat,command | "
                          "awk '$2 == 77'\n", text)
            self.assertEqual("\nTasks: " in text, published)
        # A summary whose tasks cannot be read costs the Tasks line and nothing else.
        for broken in (7, [7, {"id": 5, "acceptance": [], "candidate": 3}], {"a": 1}):
            tasks[:] = [broken] if not isinstance(broken, list) else broken
            raw = json.dumps(dict(tasks=broken, actual_window=dict(processes=dict(cleanup="done")))).encode()
            (shift / "report.json").write_bytes(raw)
            (shift / "report.md").write_text("# Report\n\nSHA256 of report.json: " + hashlib.sha256(raw).hexdigest() + "\n")
            ended = (shift, 3, "over", "stopped by the host", True, known)
            with patch("sortie_kit.cli.start", return_value=ended), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(args), 3)
            self.assertIn("\nShutdown: NOT CONFIRMED. A builder turn was not seen to stop. Check for leftover",
                          out.getvalue())
            self.assertNotIn("Shutdown: not known", out.getvalue())

    def test_cli_last_lines_show_the_reason_and_the_driver_line_as_inert_text(self):
        shift = self.root / "shift"; shift.mkdir()
        args = ["start", "--repo", str(self.target), "--direction", str(self.request.direction), "--for", "1m",
                "--agent", "driver=claude:m", "--agent", "builder=codex:m", "--trust", "t", "--key", "k"]
        ended = (shift, 0, "done\nShutdown: confirmed.\x1b[2J", "stopped\r\x1b[1A", True,
                 dict(confirmed=False, groups=[], problems=["a builder turn was not seen to stop"]))
        with patch("sortie_kit.cli.start", return_value=ended), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(args)
        text = out.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)
        self.assertIn("\nShift ended: done\\nShutdown: confirmed.\\x1b[2J\n", text)
        self.assertIn("\nDriver process: stopped\\r\\x1b[1A\n", text)
        self.assertEqual(text.count("\nShutdown: "), 1)

    def test_driver_prompt_is_host_text_with_the_shift_and_the_wait_command(self):
        shift = self.target / ".sortie/shifts/a shift"
        prompt = launcher.driver_prompt(shift, {"direction_sha256": "d" * 64, "until": "2030-01-01T00:00:00+00:00"})
        self.assertIn("\nshift: " + str(shift) + "\n", prompt)
        self.assertIn("entry.py wait --shift '" + str(shift) + "'\n", prompt)
        for part in ("driver.md", "d" * 64, "2030-01-01T00:00:00+00:00", "start no agent yourself"):
            self.assertIn(part, prompt)
        # The driver kept the line in a shell variable, which zsh runs as one file name.
        for part in ("exactly as written", "never from a shell variable", "before you publish the next"):
            self.assertIn(part, prompt)
        seat = dict(model="m", executable=dict(path="/x/agent"), argv=["{prompt}"])
        self.assertEqual(agents.render_argv(seat, prompt=prompt, cwd="/repo"), ["/x/agent", prompt])

    def test_nonroot_repo_and_empty_direction(self):
        child = self.target / "subdir"
        child.mkdir()
        with self.assertRaisesRegex(Refused, "root"):
            start(replace(self.request, repo=child), prepare_only=True)
        self.request.direction.write_text("")
        with self.assertRaisesRegex(Refused, "nonempty"):
            start(self.request, prepare_only=True)

    def test_a_direction_file_or_repository_that_is_not_there_is_refused_in_plain_words(self):
        absent = self.target / "no such direction.md"
        with self.assertRaisesRegex(Refused, "^there is no direction file at ") as raised:
            start(Request(self.target, absent, 30), prepare_only=True)
        self.assertIn(str(absent), str(raised.exception))
        gone = self.root / "no such repo"
        with self.assertRaisesRegex(Refused, "^there is no directory at ") as raised:
            start(Request(gone, self.target / "direction.md", 30), prepare_only=True)
        self.assertIn(str(gone), str(raised.exception))
        for args in (["--repo", str(self.target), "--direction", str(absent)],
                     ["--repo", str(gone), "--direction", str(self.target / "direction.md")]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["start", "--for", "1m", "--prepare-only"] + args), 3)
                self.assertIn("sortie: refused: there is no ", err.getvalue())
                self.assertNotIn("Errno", err.getvalue())
        self.assertFalse((self.target / ".sortie").exists())

    def test_tracked_sortie_refused(self):
        artifact = self.target / ".sortie"
        artifact.mkdir()
        (artifact / "old.json").write_text("{}")
        subprocess.run(["git", "-C", str(self.target), "add", "-f", ".sortie"], check=True, timeout=5)
        with self.assertRaisesRegex(Refused, "tracked"):
            start(self.request, prepare_only=True)

    def test_symlink_roots_and_exclude_refused_without_external_write(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.target / ".sortie"
        link.symlink_to(outside)
        with self.assertRaisesRegex(Refused, "symlink"):
            start(self.request, prepare_only=True)
        link.unlink()
        elsewhere = self.root / "elsewhere-exclude"
        elsewhere.write_text("kept\n")
        self.exclude().unlink(missing_ok=True)
        self.exclude().symlink_to(elsewhere)
        with self.assertRaisesRegex(Refused, "exclude file"):
            start(self.request, prepare_only=True)
        self.assertEqual(elsewhere.read_text(), "kept\n")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.target / ".sortie").exists())

    def test_a_linked_info_directory_or_git_directory_is_refused_and_nothing_outside_is_written(self):
        import shutil
        info, outside = self.target / ".git/info", self.root / "outside-info"
        outside.mkdir()
        shutil.rmtree(info)
        info.symlink_to(outside)
        with self.assertRaisesRegex(Refused, "exclude file"):
            start(self.request, prepare_only=True)
        self.assertEqual(list(outside.iterdir()), [])
        info.unlink()
        moved = self.root / "moved-git"
        (self.target / ".git").rename(moved)
        (self.target / ".git").symlink_to(moved)
        with self.assertRaisesRegex(Refused, "exclude file"):
            start(self.request, prepare_only=True)
        self.assertFalse((moved / "info").exists())
        self.assertFalse((self.target / ".sortie").exists())

    def test_exclude_file_that_is_not_a_small_regular_file_is_refused(self):
        self.exclude().write_bytes(b"x\n" * (1024 * 1024))
        with self.assertRaisesRegex(Refused, "exclude file"):
            start(self.request, prepare_only=True)
        self.exclude().unlink()
        self.exclude().mkdir()
        with self.assertRaisesRegex(Refused, "exclude file|Git preflight"):
            start(self.request, prepare_only=True)
        self.assertFalse((self.target / ".sortie").exists())

    def test_exclude_file_that_shares_its_content_with_another_name_is_refused(self):
        # A hard link is no symlink: appending in place would change the other name's file too.
        outside = self.root / "outside-file"
        outside.write_text("kept\n")
        self.exclude().unlink(missing_ok=True)
        os.link(outside, self.exclude())
        with self.assertRaisesRegex(Refused, "exclude file"):
            start(self.request, prepare_only=True)
        self.assertEqual(outside.read_text(), "kept\n")
        self.assertFalse((self.target / ".sortie").exists())

    def test_a_linked_worktree_uses_the_shared_exclude_file(self):
        git = ["git", "-C", str(self.target), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "base"], check=True, capture_output=True, timeout=10)
        tree = self.root / "linked tree"
        subprocess.run(git + ["worktree", "add", "-q", "--detach", str(tree)], check=True, capture_output=True, timeout=10)
        (tree / "direction.md").write_text("Implement the selected task.\n")
        shift = start(Request(tree, tree / "direction.md", 30), prepare_only=True)
        self.assertTrue(shift.is_relative_to(tree.resolve()))
        self.assertEqual(self.exclude().read_text().splitlines().count("/.sortie/"), 1)
        shown = subprocess.run(["git", "-C", str(tree), "status", "--short", "--untracked-files=all"],
                               check=True, capture_output=True, text=True, timeout=5).stdout
        self.assertNotIn(".sortie", shown)

    def test_partial_preparation_retains_explicit_failure(self):
        # The packaged templates are the last thing preparation copies; fail there.
        with patch("sortie_kit.layout.files", side_effect=OSError("disk fault")):
            with self.assertRaisesRegex(OSError, "disk fault"):
                start(self.request, prepare_only=True)
        shifts = list((self.target / ".sortie/shifts").iterdir())
        self.assertEqual(len(shifts), 1)
        self.assertIn("disk fault", (shifts[0] / "preparation-error.txt").read_text())
        self.assertFalse((shifts[0] / "grant.json").exists())

    def test_signing_refuses_missing_choices_before_terminal_or_preparation(self):
        seat = {"agent": "claude", "model": "m"}
        cases = [(dict(choices=None), "name an agent"),
                 (dict(choices={"driver": seat}), "name an agent"),
                 (dict(choices={"driver": seat, "builder": seat, "host": seat}), "name an agent"),
                 (dict(choices={"driver": seat, "builder": {"agent": ""}}), "name an agent"),
                 (dict(choices={"driver": seat, "builder": seat, "reviewer": {"agent": ""}}), "name an agent"),
                 (dict(choices={"driver": seat, "builder": seat, "reviewer": "claude"}), "name an agent"),
                 (dict(choices={"driver": seat, "builder": seat}, prepare_only=True), "without --prepare-only")]
        for kwargs, message in cases:
            with self.subTest(message=message, kwargs=kwargs), \
                    patch("sortie_kit.signing.terminal", side_effect=AssertionError("no terminal")), \
                    self.assertRaisesRegex(Refused, message):
                start(self.request, sign_only=True, trust_path=self.root / "unused-trust",
                      key=self.root / "unused-key", **kwargs)
        self.assertFalse((self.target / ".sortie").exists())

    def test_choices_become_pinned_seats_and_no_permission_file_is_written(self):
        fixture = agent_fixture(self.root, self.target)
        shift = start(self.request, prepare_only=True)
        before = sorted(p.relative_to(shift) for p in shift.rglob("*"))
        with patch("subprocess.Popen", side_effect=AssertionError("no agent started")), \
                patch("subprocess.run", side_effect=AssertionError("no agent started")):
            seats = launcher.signed_seats(self.target, fixture["choices"], home=fixture["home"])
        self.assertEqual({r: (s["agent"], s["model"], s["family"], s["mode"]) for r, s in seats.items()},
                         {"driver": ("fixture", "fixture-model", "fixture-family", "interactive"),
                          "builder": ("fixture", "fixture-model", "fixture-family", "headless")})
        for role in ("driver", "builder"):
            seat = agents.seat_checked(role, seats[role])
            self.assertEqual(seat["executable"]["path"], str(fixture["program"]))
            self.assertNotIn("config", seat)
        self.assertEqual(sorted(p.relative_to(shift) for p in shift.rglob("*")), before)
        self.assertFalse((shift / "runtime").exists())

    def test_seat_config_flag_is_gone_and_built_in_seats_pass_no_settings_file(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stop:
            cli.main(["start", "--repo", str(self.target), "--direction", str(self.target / "direction.md"),
                      "--for", "1m", "--prepare-only", "--seat-config", "builder=/x"])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("--seat-config", err.getvalue())
        for name in ("claude", "codex", "agy"):
            value = agents.load(name, self.target, home=self.root / "no-home")["value"]
            self.assertNotIn("config", value)
            for key in ("interactive_argv", "headless_argv"):
                self.assertFalse({"--settings", "{config}", "--profile"} & set(value[key]), (name, key))

    def test_seat_choices_refuse_missing_model_or_family(self):
        fixture = agent_fixture(self.root, self.target)
        def changed(role, **values):
            choices = {r: dict(c) for r, c in fixture["choices"].items()}
            choices[role].update(values)
            return choices
        cases = [(changed("builder", model=None), "none is assumed"),
                 (changed("builder", model="unlisted"), "state its family"),
                 (changed("builder", family="gpt"), "contradicts"),
                 (changed("driver", agent="absent"), "no agent profile"),
                 (changed("driver", agent="no-driver"), "no-driver has no interactive start and cannot be the driver")]
        # A profile made here with no interactive form, on the fixture's own program: the case
        # does not depend on which agent CLIs this machine has installed. No built-in profile is such a one.
        directory = agents.user_directory(fixture["home"])
        (directory / "no-driver.json").write_text(json.dumps(dict(
            json.loads((directory / "fixture.json").read_text()), name="no-driver", interactive_argv=None)))
        empty = self.root / "no-tools"
        empty.mkdir()
        for choices, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(Refused, message):
                launcher.signed_seats(self.target, choices, home=fixture["home"], search=str(empty))
        self.assertEqual(launcher.signed_seats(self.target, changed("builder", model="unlisted", family="GPT"),
                                               home=fixture["home"])["builder"]["family"], "gpt")

    def test_cli_requires_positive_finite_duration(self):
        args = ["start", "--repo", str(self.target), "--direction", str(self.request.direction),
                "--prepare-only"]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(args), 2)  # preparation asks nothing; the duration is its one requirement
        self.assertIn("missing: --for DURATION", err.getvalue())
        for suffix in (["--for", "0"], ["--for", "nan"], ["--for", "inf"],
                       ["--for", "-2"], ["--for", "9" * 400]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main(args + suffix)
            self.assertEqual(raised.exception.code, 2)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args + ["--for", "1m"]), 0)
        self.assertIn("unsigned", out.getvalue())
