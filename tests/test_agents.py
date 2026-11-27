import hashlib
import json
import os
from pathlib import Path

from sortie_kit import agents
from sortie_kit.layout import Refused
from support import LocalTest


def profile(**changes):
    value = dict(version=1, name="fixture", executable="fixture-agent",
                 interactive_argv=["--model", "{model}", "{prompt}"],
                 headless_argv=["run", "--model", "{model}", "--cd", "{cwd}", "{prompt}"],
                 models={"fixture-model": "fixture-family"}, default_model=None)
    value.update(changes)
    return value


class AgentTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.target = self.repo()
        self.home = self.root / "home"
        self.directory = agents.user_directory(self.home)
        self.directory.mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.program = self.outside / "fixture-agent"
        self.program.write_text("#!/bin/sh\nexit 0\n")
        self.program.chmod(0o755)

    def install(self, value, name=None):
        path = self.directory / ((name or value["name"]) + ".json")
        path.write_text(json.dumps(value))
        return path

    def loaded(self, **changes):
        self.install(profile(**changes))
        return agents.load("fixture", self.target, home=self.home)

    def refused(self, pattern, call, *args, **kwargs):
        with self.assertRaisesRegex(Refused, pattern):
            call(*args, **kwargs)

    def test_a_pinned_file_that_is_a_script_is_told_from_one_that_is_not_without_running_it(self):
        pin = lambda path: {"path": str(path), "sha256": "0" * 64, "bytes": 1}
        native = self.outside / "native-agent"
        native.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 64)
        short = self.outside / "short"
        short.write_bytes(b"#")
        link = self.outside / "link"
        link.symlink_to(self.program)
        self.program.write_text("#!/bin/sh\ntouch " + str(self.root / "ran") + "\n")
        self.assertIs(agents.launcher(pin(self.program)), True)
        for other in (native, short, link, self.outside / "absent", self.outside):
            self.assertIs(agents.launcher(pin(other)), False, other)
        # A path is text, as a signed pin has it: nothing else is opened.
        for odd in ({}, {"path": None}, None, {"path": self.program}, {"path": os.fsencode(self.program)}, {"path": 0}):
            self.assertIs(agents.launcher(odd), False, odd)
        self.assertFalse((self.root / "ran").exists())

    def test_the_signer_is_told_which_seats_have_a_script_pinned_and_what_that_pin_does_not_cover(self):
        native = self.outside / "native-agent"
        native.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 64)
        seat = lambda path: {"executable": {"path": str(path), "sha256": "0" * 64, "bytes": 1}}
        head = lambda who, path: ("Pinned script: the pinned file of " + who + ", " + str(path) + ", is a script. Its pin, "
                                  "as every pin, covers that one file and not its interpreter or any program the "
                                  "script starts. ")
        again = lambda who: ("An update of the CLI that writes this file again ends the work of " + who + " in this "
                             "shift: the host refuses the next start. ")
        once = "The driver is started once, when the shift begins: a rewrite after that is not looked at for it. "
        unseen = "An update that leaves this file as it is goes unseen by the host."
        every = ("driver", "builder", "reviewer")
        line, = agents.launcher_words({"driver": seat(self.program), "builder": seat(self.program), "reviewer": seat(native)})
        self.assertEqual(line, head("the driver and the builder", self.program) + again("the builder") + once + unseen)
        other = self.outside / "other-agent"
        other.write_text("#!/usr/bin/env node\n")
        first, second = agents.launcher_words({"driver": seat(native), "builder": seat(other), "reviewer": seat(self.program)})
        self.assertEqual(first, head("the builder", other) + again("the builder") + unseen)
        self.assertEqual(second, head("the reviewer", self.program) + again("the reviewer") + unseen)
        one, = agents.launcher_words({role: seat(self.program) for role in every})
        self.assertEqual(one, head("the driver, the builder and the reviewer", self.program)
                         + again("the builder and the reviewer") + once + unseen)
        # A driver alone is started once: no next start is promised for it.
        alone, = agents.launcher_words({"driver": seat(self.program), "builder": seat(native), "reviewer": seat(native)})
        self.assertEqual(alone, head("the driver", self.program) + once + unseen)
        self.assertEqual(agents.launcher_words({role: seat(native) for role in every}), [])
        # Seats of another shape give no line and raise nothing: the lines are a note, and the grant is checked elsewhere.
        for odd in (None, [], {}, {"driver": None}, {"driver": {}, "builder": {"executable": None}, "reviewer": seat(native)},
                    {role: {"executable": {"path": self.program}} for role in every}):
            self.assertEqual(agents.launcher_words(odd), [], odd)
        self.assertEqual(len(agents.launcher_words({"builder": seat(self.program)})), 1)

    def test_packaged_profiles_are_valid_and_each_can_take_any_seat(self):
        self.assertEqual(agents.available(self.target, home=self.home), ["agy", "claude", "codex"])
        pin = {"path": "/x/program", "sha256": "0" * 64, "bytes": 1}
        for name in ("claude", "codex", "agy"):
            found = agents.load(name, self.target, home=self.home)
            self.assertEqual((found["value"]["name"], found["source"]), (name, "built-in"))
            self.assertIsNone(found["value"]["default_model"])  # no model is ever assumed
            for role, mode in agents.MODES.items():
                made = agents.seat(role, found, "some-model", "some-family", pin)
                self.assertEqual((made["mode"], made["argv"]), (mode, found["value"][mode + "_argv"]), (name, role))
        agy = agents.load("agy", self.target, home=self.home)
        self.assertEqual(agents.choose_model(agy, "gemini-4-carbon-high"), ("gemini-4-carbon-high", "gemini"))
        # A profile with no interactive form cannot drive, and one with no headless form cannot build.
        self.refused("fixture has no interactive start and cannot be the driver", agents.seat, "driver",
                     self.loaded(interactive_argv=None), "fixture-model", "fixture-family", pin)
        self.refused("fixture has no headless start and cannot be the builder", agents.seat, "builder",
                     self.loaded(headless_argv=None), "fixture-model", "fixture-family", pin)

    def test_built_in_agy_starts_each_seat_as_its_help_gives_the_options(self):
        agy = agents.load("agy", self.target, home=self.home)["value"]
        # --prompt-interactive: run an initial prompt interactively and continue the session.
        self.assertEqual(agy["interactive_argv"], ["--prompt-interactive", "{prompt}", "--model", "{model}"])
        # --print: run a single prompt non-interactively and print the response.
        self.assertEqual(agy["headless_argv"], ["--print", "{prompt}", "--model", "{model}"])
        self.assertEqual(agy["review_argv"], ["--print", "{prompt}", "--mode", "plan", "--sandbox", "--model", "{model}"])
        self.assertEqual(agy["review_model"], "gemini-4-hydrogen-high")
        # The builder's form asks for no mode and no sandbox, and nothing that turns asking off.
        for key in ("interactive_argv", "headless_argv"):
            self.assertFalse({"--mode", "--sandbox", "--dangerously-skip-permissions", "--settings"} & set(agy[key]), key)

    def test_built_in_agy_places_each_listed_model_in_its_makers_family(self):
        agy = agents.load("agy", self.target, home=self.home)
        models = agy["value"]["models"]
        for model in ("gemini-4-neon-high", "gemini-4-neon-medium", "gemini-4-hydrogen-high", "gemini-4-carbon-high"):
            self.assertEqual(agents.choose_model(agy, model), (model, "gemini"))
        # agy serves models of another maker too. Each one listed is in that maker's family,
        # and a stated family cannot move it out.
        served = [model for model in models if model.startswith("claude-")]
        self.assertEqual(sorted(served), ["claude-opus-5-5-high", "claude-opus-5-5-low", "claude-opus-5-5-medium",
                                          "claude-sonnet-5-5-high", "claude-sonnet-5-5-low", "claude-sonnet-5-5-medium"])
        for model in served:
            self.assertEqual(agents.choose_model(agy, model), (model, "claude"))
            self.refused("contradicts", agents.choose_model, agy, model, "gemini")
        self.assertEqual(agents.choose_model(agy, "gpt-oss-120b-medium"), ("gpt-oss-120b-medium", "gpt"))
        for model, kind in models.items():
            self.assertEqual(kind, {"claude": "claude", "gpt": "gpt"}.get(model.split("-")[0], "gemini"), model)
            self.assertTrue(model.startswith(("claude-", "gpt-", "gemini-")), model)
        # None is assumed for a model it does not list.
        self.assertNotIn("unlisted_family", agy["value"])
        self.refused("state its family", agents.choose_model, agy, "claude-some-later-model")

    def test_built_in_codex_builder_asks_for_repo_writes_and_no_profile_turns_a_sandbox_off(self):
        codex = agents.load("codex", self.target, home=self.home)["value"]
        # `codex exec` is read-only unless told otherwise: a builder must write the
        # shift's receipts, edit and commit (.git). The host runs the reviews, so the
        # builder is given no network unless the start asks for the second form.
        self.assertEqual(codex["headless_argv"], [
            "exec", "--sandbox", "workspace-write", "--add-dir", ".git",
            "--model", "{model}", "--cd", "{cwd}", "{prompt}"])
        # The form with the network on is what the one headless form was before, element for element.
        self.assertEqual(codex["headless_network_argv"], [
            "exec", "--sandbox", "workspace-write", "--add-dir", ".git",
            "-c", "sandbox_workspace_write.network_access=true",
            "--model", "{model}", "--cd", "{cwd}", "{prompt}"])
        for name in ("claude", "agy"):
            self.assertNotIn("headless_network_argv", agents.load(name, self.target, home=self.home)["value"])
        self.assertEqual(codex["interactive_argv"], ["--model", "{model}", "--cd", "{cwd}", "{prompt}"])
        for name in ("claude", "codex", "agy"):
            value = agents.load(name, self.target, home=self.home)["value"]
            for key in ("interactive_argv", "headless_argv", "headless_network_argv"):
                text = " ".join(value.get(key) or []).lower()
                for off in ("danger", "bypass", "skip-permissions", "full-access", "approve"):
                    self.assertNotIn(off, text, (name, key))

    def test_the_network_form_is_taken_only_by_a_builder_whose_profile_has_one(self):
        pin = {"path": "/opt/tools/x", "sha256": "0" * 64, "bytes": 1}
        codex = agents.load("codex", self.target, home=self.home)
        plain = agents.seat("builder", codex, "m", "gpt", pin)
        wide = agents.seat("builder", codex, "m", "gpt", pin, network=True)
        option = ["-c", "sandbox_workspace_write.network_access=true"]
        self.assertEqual(plain["argv"], codex["value"]["headless_argv"])
        self.assertEqual(wide["argv"], codex["value"]["headless_network_argv"])
        self.assertEqual([a for a in wide["argv"] if a not in option], plain["argv"])
        self.assertFalse(set(option) & set(plain["argv"]))
        # The seat record is the same seven fields either way: the signed argv is what says it.
        self.assertEqual(set(wide), set(plain))
        self.assertEqual({k: v for k, v in wide.items() if k != "argv"}, {k: v for k, v in plain.items() if k != "argv"})
        # Asked of a profile that has no such form, it is refused and never dropped.
        claude, agy = (agents.load(name, self.target, home=self.home) for name in ("claude", "agy"))
        for profile_ in (claude, agy, self.loaded()):
            name = profile_["value"]["name"]
            self.refused("--builder-network was given, but the builder profile " + name + " has no form with the network on",
                         agents.seat, "builder", profile_, "fixture-model", "f", pin, network=True)
        # Only the builder: no other seat has such a form.
        for role in ("driver", "reviewer"):
            self.refused("only the builder", agents.seat, role, codex, "m", "gpt", pin, network=True)
        # A profile of one's own may have the form; it is a whole template and is checked as one.
        own = self.loaded(headless_network_argv=["run", "--net", "--model", "{model}", "{prompt}"])
        self.assertEqual(agents.seat("builder", own, "fixture-model", "f", pin, network=True)["argv"],
                         ["run", "--net", "--model", "{model}", "{prompt}"])
        self.assertEqual(agents.seat("builder", own, "fixture-model", "f", pin)["argv"], profile()["headless_argv"])
        # A profile with no headless start at all: that is the reason given, not the missing network form.
        self.refused("has no headless start and cannot be the builder", agents.seat, "builder",
                     self.loaded(headless_argv=None), "fixture-model", "f", pin, network=True)
        for bad in (dict(headless_network_argv=["--net", "{prompt}"]), dict(headless_network_argv="--net"),
                    dict(headless_network_argv=profile()["headless_argv"]),  # the same command: the flag would do nothing
                    dict(headless_network_argv=["--model={model}", "{prompt}"]),
                    dict(headless_network_argv=["{model}", "{prompt}"], headless_argv=None)):
            with self.subTest(bad), self.assertRaises(Refused):
                agents.profile_checked(profile(**bad))
        agents.profile_checked(profile(headless_network_argv=None))  # absent and null both mean no such form

    def test_a_network_form_that_fills_to_the_same_command_is_refused_for_that_start(self):
        # The two templates differ, and with this model in its place they are one command:
        # a start that asked for the network would sign what a start without it signs.
        pin = {"path": "/opt/tools/x", "sha256": "0" * 64, "bytes": 1}
        own = self.loaded(headless_argv=["run", "--model", "{model}", "fixture-model", "{prompt}"],
                          headless_network_argv=["run", "--model", "fixture-model", "{model}", "{prompt}"],
                          models={"fixture-model": "f", "other-model": "f"})
        self.refused("--builder-network was given, but with model fixture-model the builder profile fixture's form "
                     "with the network on is the same command as its form without; nothing was prepared or signed",
                     agents.seat, "builder", own, "fixture-model", "f", pin, network=True)
        self.assertEqual(agents.seat("builder", own, "other-model", "f", pin, network=True)["argv"],
                         ["run", "--model", "fixture-model", "{model}", "{prompt}"])
        self.assertEqual(agents.seat("builder", own, "fixture-model", "f", pin)["argv"],
                         ["run", "--model", "{model}", "fixture-model", "{prompt}"])
        # The same with the repository's path in the place of {cwd}.
        own = self.loaded(headless_argv=["run", "--model", "{model}", "--at", "{cwd}", "/work/repo", "{prompt}"],
                          headless_network_argv=["run", "--model", "{model}", "--at", "/work/repo", "{cwd}", "{prompt}"])
        self.refused("is the same command as its form without", agents.network_argv, own, "fixture-model", "/work/repo")
        self.assertEqual(agents.network_argv(own, "fixture-model", "/work/other"),
                         ["run", "--model", "{model}", "--at", "/work/repo", "{cwd}", "{prompt}"])
        self.assertEqual(agents.network_argv(own), agents.network_argv(own, "fixture-model"))  # nothing to fill: as written

    def test_custom_profile_loads_and_cannot_shadow_or_come_from_unsafe_files(self):
        found = self.loaded()
        self.assertEqual(found["sha256"], hashlib.sha256((self.directory / "fixture.json").read_bytes()).hexdigest())
        self.assertIn("fixture", agents.available(self.target, home=self.home))
        self.install(profile(name="claude"))
        self.refused("built-in name", agents.load, "claude", self.target, home=self.home)
        self.install(profile(name="other"), name="renamed")
        self.refused("differs from its file name", agents.load, "renamed", self.target, home=self.home)
        self.refused("no agent profile", agents.load, "absent", self.target, home=self.home)
        self.refused("lower-case token", agents.load, "../fixture", self.target, home=self.home)
        (self.directory / "fixture.json").chmod(0o664)
        self.refused("group or world writable", agents.load, "fixture", self.target, home=self.home)
        (self.directory / "fixture.json").unlink()
        real = self.install(profile(), name="real")
        (self.directory / "fixture.json").symlink_to(real)
        self.refused("symlink", agents.load, "fixture", self.target, home=self.home)
        inside = agents.user_directory(self.target)
        inside.mkdir(parents=True)
        (inside / "fixture.json").write_text(json.dumps(profile()))
        self.refused("outside the target repo", agents.load, "fixture", self.target, home=self.target)

    def test_profile_shape_and_argv_templates_fail_closed(self):
        bad = [
            dict(version=2), dict(name="Fixture"), dict(executable="-x"), dict(executable="bin/agent"),
            dict(config="none"), dict(config=["settings"]),  # no permission file is taken or declared
            dict(models={"m": "Family"}), dict(models={"-m": "family"}), dict(default_model="unlisted"),
            dict(headless_argv=["{prompt}"]),  # no model
            dict(headless_argv=["{model}", "{model}", "{prompt}"]),
            dict(headless_argv=["{model}", "{prompt}", "{prompt}"]),
            dict(headless_argv=["--model={model}", "{prompt}"]),  # not a whole argument
            dict(headless_argv=["{model}", "{prompt}", "{cwd}", "{cwd}"]),
            dict(headless_argv=["{model}", "{prompt}", ""]),
            dict(headless_argv="{model} {prompt}"),
        ]
        for changes in bad:
            with self.subTest(changes), self.assertRaises(Refused):
                agents.profile_checked(profile(**changes))
        extra = profile()
        extra["shell"] = True
        self.refused("fields", agents.profile_checked, extra)
        agents.profile_checked(profile(interactive_argv=None, headless_argv=["{model}", "{prompt}"]))

    def test_a_profile_may_name_the_family_of_models_it_does_not_list(self):
        found = self.loaded(unlisted_family="fixture-family")
        self.assertEqual(agents.choose_model(found, "new-model"), ("new-model", "fixture-family"))
        # A stated family never decides against the profile: not for a model it lists, and not
        # for one it does not list when the profile names the family of all such models.
        # Stating the profile's own family, in any spelling of it, changes nothing.
        self.refused("contradicts", agents.choose_model, found, "fixture-model", "gpt")
        self.refused("contradicts the fixture profile, which places every model it does not list in the family "
                     "fixture-family", agents.choose_model, found, "new-model", "GPT")
        self.assertEqual(agents.choose_model(found, "new-model", "Fixture-Family"), ("new-model", "fixture-family"))
        # Only a profile that names no such family leaves an unlisted model to what is stated.
        open_ = self.loaded(name="fixture", unlisted_family=None)
        self.assertEqual(agents.choose_model(open_, "new-model", "GPT"), ("new-model", "gpt"))
        # So no statement makes a claude model on the built-in claude profile, an alias of one
        # included, anything but claude, nor a model on the built-in codex profile anything but gpt.
        claude, codex = (agents.load(name, self.target, home=self.root / "no-home") for name in ("claude", "codex"))
        for model in ("sonnet", "opus", "claude-some-later-model"):
            self.refused("contradicts", agents.choose_model, claude, model, "gemini")
            self.assertEqual(agents.choose_model(claude, model, "Anthropic"), (model, "claude"))
        self.refused("contradicts", agents.choose_model, codex, "some-model", "claude")
        self.assertEqual(agents.choose_model(codex, "some-model", "OpenAI"), ("some-model", "gpt"))
        self.refused("none is assumed", agents.choose_model, found)
        for bad in ("Family", "", 3, ["gpt"]):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                agents.profile_checked(profile(unlisted_family=bad))
        agents.profile_checked(profile(unlisted_family=None))
        built_in = {name: agents.load(name, self.target, home=self.root / "no-home")["value"]
                    for name in ("claude", "codex", "agy")}
        # codex lists no model, so without this a first start had to invent a family name.
        self.assertEqual((built_in["codex"]["models"], built_in["codex"]["unlisted_family"]), ({}, "gpt"))
        self.assertEqual(built_in["claude"]["unlisted_family"], "claude")
        self.assertIsNone(built_in["agy"].get("unlisted_family"))  # agy runs models of several families

    def test_one_family_has_one_name_however_it_is_reached(self):
        from sortie_kit import grants
        # The maker's name and the model line's name are one family; otherwise a codex builder (gpt)
        # and a reviewer stated as openai would pass as independent.
        for stated, canonical in (("OpenAI", "gpt"), ("gpt", "gpt"), ("anthropic", "claude"), ("Google", "gemini"),
                                  ("other", "other")):
            self.assertEqual(grants.family(stated), canonical)
        found = self.loaded(models={"fixture-model": "openai"}, unlisted_family="anthropic")
        self.assertEqual(agents.choose_model(found, "fixture-model"), ("fixture-model", "gpt"))
        self.assertEqual(agents.choose_model(found, "fixture-model", "GPT"), ("fixture-model", "gpt"))
        self.assertEqual(agents.choose_model(found, "new-model"), ("new-model", "claude"))
        self.assertEqual(agents.choose_model(found, "new-model", "Anthropic"), ("new-model", "claude"))
        self.refused("contradicts", agents.choose_model, found, "new-model", "openai")
        codex = agents.load("codex", self.target, home=self.root / "no-home")
        builder = agents.choose_model(codex, "gpt-next")
        for review_family in ("openai", "OpenAI", "gpt"):
            with self.subTest(review_family=review_family):
                # A maker's name cannot pass as another family: it is not a pick of another family.
                with self.assertRaisesRegex(Refused, "how the reviewer was picked"):
                    grants.review_policy(*builder, "gpt-next", review_family, "picked-other-family")
                policy = grants.review_policy(*builder, "gpt-next", review_family, "given")
                self.assertEqual((policy["builder_family"], policy["review_family"], policy["same_family_review"]),
                                 ("gpt", "gpt", True))
        self.assertFalse(grants.review_policy(*builder, "gemini-4-carbon-high", "google",
                                              "picked-other-family")["same_family_review"])

    def test_model_is_never_assumed_and_family_cannot_contradict_the_profile(self):
        found = self.loaded()
        self.refused("none is assumed", agents.choose_model, found)
        self.assertEqual(agents.choose_model(found, "fixture-model"), ("fixture-model", "fixture-family"))
        self.refused("state its family", agents.choose_model, found, "new-model")
        self.assertEqual(agents.choose_model(found, "new-model", " GPT "), ("new-model", "gpt"))
        self.refused("contradicts", agents.choose_model, found, "fixture-model", "gpt")
        self.refused("invalid model", agents.choose_model, found, "-model", "gpt")
        omitted = profile()
        del omitted["default_model"]
        self.install(omitted)
        unset = agents.load("fixture", self.target, home=self.home)
        self.refused("none is assumed", agents.choose_model, unset)
        self.assertEqual(agents.choose_model(unset, "fixture-model"), ("fixture-model", "fixture-family"))
        chosen = self.loaded(default_model="fixture-model")
        self.assertEqual(agents.choose_model(chosen), ("fixture-model", "fixture-family"))

    def test_executable_is_resolved_pinned_and_rechecked_without_running_it(self):
        found = self.loaded()
        self.program.write_text("#!/bin/sh\necho ran > '" + str(self.outside / "ran") + "'\n")
        link = self.outside / "bin"
        link.mkdir()
        (link / "fixture-agent").symlink_to(self.program)
        pin, seen = agents.pin_executable(found, self.target, search=str(link))
        raw = self.program.read_bytes()
        self.assertEqual(pin, {"path": os.path.realpath(self.program), "sha256": hashlib.sha256(raw).hexdigest(),
                               "bytes": len(raw)})
        self.assertEqual(agents.check_executable(pin, self.target), seen)
        self.assertEqual(agents.observe(pin["path"]), seen)
        self.assertFalse((self.outside / "ran").exists())
        self.refused("not found", agents.pin_executable, found, self.target, search=str(self.root / "empty"))
        # A symlink left at the pinned path is not followed.
        self.refused("fully resolved", agents.check_executable, dict(pin, path=str(link / "fixture-agent")), self.target)
        self.program.chmod(0o775)
        self.refused("group or world writable", agents.check_executable, pin, self.target)
        self.program.chmod(0o644)
        self.refused("must be executable", agents.check_executable, pin, self.target)
        self.program.chmod(0o641)  # an execute bit that does not apply to its owner
        self.refused("must be executable", agents.check_executable, pin, self.target)
        self.program.chmod(0o755)
        self.program.write_bytes(raw + b"# changed\n")
        self.refused("differs from the signed pin", agents.check_executable, pin, self.target)
        self.assertNotEqual(agents.observe(pin["path"]), seen)
        inside = self.target / "fixture-agent"
        inside.write_bytes(raw)
        inside.chmod(0o755)
        self.refused("outside the target repo", agents.pin_executable, found, self.target, search=str(self.target))

    def test_same_bytes_at_a_replaced_inode_change_the_spawn_observation(self):
        found = self.loaded(executable=str(self.program))
        pin, seen = agents.pin_executable(found, self.target)
        raw = self.program.read_bytes()
        self.program.unlink()
        self.program.write_bytes(raw)
        self.program.chmod(0o755)
        self.assertEqual(agents.hash_executable(pin["path"], self.target)[0], pin)
        self.assertNotEqual(agents.observe(pin["path"]), seen)

    def seat(self, role="builder", **changes):
        found = self.loaded(**changes)
        pin, _ = agents.pin_executable(found, self.target, search=str(self.outside))
        return agents.seat(role, found, "fixture-model", "fixture-family", pin)

    def test_seat_scope_binds_mode_template_and_pins(self):
        builder, driver = self.seat(), self.seat("driver")
        self.assertEqual((builder["mode"], driver["mode"]), ("headless", "interactive"))
        self.assertEqual(builder["argv"], profile()["headless_argv"])
        self.assertEqual(driver["argv"], profile()["interactive_argv"])
        self.assertEqual(builder["profile_sha256"], hashlib.sha256((self.directory / "fixture.json").read_bytes()).hexdigest())
        with self.assertRaisesRegex(Refused, "cannot be the builder"):
            self.seat(headless_argv=None)
        with self.assertRaisesRegex(Refused, "cannot be the driver"):
            self.seat("driver", interactive_argv=None)
        bad = [dict(mode="interactive"), dict(model="-m"), dict(family="Fixture"), dict(agent="x/y"),
               dict(profile_sha256="0"), dict(argv=["{model}"]), dict(config=[]),
               dict(executable=dict(builder["executable"], path="relative/agent")),
               dict(executable=dict(builder["executable"], bytes=True))]
        for changes in bad:
            with self.subTest(changes), self.assertRaises(Refused):
                agents.seat_checked("builder", dict(builder, **changes))
        for role, value in (("builder", builder), ("driver", driver)):
            self.refused("invalid seat", agents.seat_checked, role, dict(value, argv=None))
        self.refused("invalid seat", agents.seat_checked, "reviewer", builder)
        self.refused("invalid seat", agents.seat_checked, "builder", dict(builder, pane="w1:p1"))

    def test_argv_is_filled_as_whole_arguments_from_the_signed_seat(self):
        builder = self.seat()
        prompt = "Read builder.md; $(touch x) `id` --model other\n'quoted' {cwd} {model}"
        argv = agents.render_argv(builder, prompt=prompt, cwd=self.target)
        self.assertEqual(argv, [builder["executable"]["path"], "run", "--model", "fixture-model",
                                "--cd", str(self.target), prompt])
        literal = dict(builder, argv=["--settings", "{config}", "{model}", "{prompt}"])
        self.assertEqual(agents.render_argv(literal, prompt="go", cwd=self.target)[1:3],
                         ["--settings", "{config}"])  # not a placeholder any more: passed as written
        self.refused("cannot be read as an option", agents.render_argv, builder, prompt="--dangerous", cwd=self.target)
        self.refused("cannot be read as an option", agents.render_argv, builder, prompt="", cwd=self.target)
        self.refused("cannot be read as an option", agents.render_argv, builder, prompt="a\x00b", cwd=self.target)
        self.refused("working directory", agents.render_argv, builder, prompt="go", cwd="relative")

NO_NETWORK = ("no network option is passed: unless your own codex configuration turns it on, the builder "
              "cannot use the network in the commands it runs (codex itself still reaches its model). "
              "That is how the CLI runs this sandbox; sortie-kit does not enforce it")
NETWORK = ("-c sandbox_workspace_write.network_access=true: the builder can use the network in the commands it runs. "
           "That is what the option asks of the CLI; sortie-kit has not checked it")


class ReadableScopeTests(LocalTest):
    """What the developer reads before signing is built from the grant alone."""
    def value(self, reviewer="agy", family="gemini", choice="picked-other-family", network=False, **changes):
        from sortie_kit import grants
        seats = {}
        for role, name in (("driver", "claude"), ("builder", "codex"), ("reviewer", reviewer)):
            profile = agents.load(name, self.root, home=self.root / "no-home")
            seats[role] = agents.seat(role, profile, "model-" + role, family if role == "reviewer" else "family-" + role,
                                      {"path": "/opt/tools/" + name, "sha256": "0" * 64, "bytes": 1},
                                      **(dict(network=True) if network and role == "builder" else {}))
        value = dict(repo="/work/target repo", shift_id="20300101T000000Z-abc", direction_sha256="d" * 64, seats=seats,
                     start="2030-01-01T00:00:00+00:00", until="2030-01-01T00:45:00+00:00", review_limit_seconds=600,
                     review_policy=grants.review_policy("model-builder", "family-builder", "model-reviewer", family,
                                                        choice))
        value.update(changes)
        return value

    def test_summary_names_repo_window_seats_commands_and_builder_reach(self):
        from sortie_kit import signing
        text = signing.summary(self.value())
        for expected in (
                "Repository: /work/target repo\n", "SHA256 " + "d" * 64,
                "Window:     45m, until 2030-01-01 00:45:00 UTC.",
                "              New work can start for at most 30m, less what signing and startup take.\n"
                "              In the last 15m nothing new starts; a builder turn already running may finish, and a review starts only when its whole limit still fits.\n"
                "              10s before the deadline the host begins to stop whatever still runs.\n",
                "Shift:      /work/target repo/.sortie/shifts/20300101T000000Z-abc\n"
                "              To end it early, from another terminal:\n"
                "              sortie stop --shift '/work/target repo/.sortie/shifts/20300101T000000Z-abc'\n",
                "Driver:     claude, model model-driver (family family-driver), in this terminal\n"
                "              /opt/tools/claude --model model-driver PROMPT\n",
                "Builder:    codex, model model-builder (family family-builder), headless, one turn at a time\n"
                "              /opt/tools/codex exec --sandbox workspace-write --add-dir .git "
                "--model model-builder --cd '/work/target repo' PROMPT\n",
                "Reviewer:   agy, model model-reviewer (family gemini), headless, started by the host for each review\n"
                "              picked: the first installed agent of another family than the builder\n"
                "              /opt/tools/agy --print PROMPT --mode plan --sandbox --model model-reviewer\n"
                "              REVIEW stands for the directory the host makes for each run of a review.\n"
                "              One run may take at most 10m; a run that gives nothing usable is\n"
                "              made once more, and a second failure blocks that task.\n",
                "  What the reviewer can reach:\n"
                "    - --mode plan --sandbox: the CLI's plan mode, with its terminal restrictions on. That is what "
                "the option asks of the CLI; sortie-kit has not checked what it stops\n"
                "    - it runs as you, with your sign-in for agy, in a directory under the shift",
                "    - sortie-kit does not confine it: it can read whatever your user can read and use the network "
                "as its CLI allows\n"
                "    - a change to the repository or to the shift's receipts/ during a review fails that review\n",
                "    - --sandbox workspace-write: write anywhere in the repository\n",
                "    - --add-dir .git: write in .git too",
                "    - " + NO_NETWORK + "\n",
                "it can read whatever your user can read",
                "Authority:  the grant's own actions are local work and starting these three seats. It has no\n"
                "              push or publish action. What the agents can reach is what their command lines\n"
                "              and your configuration allow; sortie-kit inspects neither.\n"):
            self.assertIn(expected, text)
        self.assertNotIn("Everything is stopped", text)
        self.assertNotIn("same-family review", text)
        self.assertNotIn("agy-review", text)
        self.assertNotIn("network_access", text)
        self.assertNotIn("can use the network", text)

    def test_summary_of_a_start_with_the_builder_network_shows_the_option_and_says_it_in_plain_words(self):
        from sortie_kit import signing
        text = signing.summary(self.value(network=True))
        self.assertIn("              /opt/tools/codex exec --sandbox workspace-write --add-dir .git -c "
                      "sandbox_workspace_write.network_access=true --model model-builder --cd '/work/target repo' PROMPT\n",
                      text)
        self.assertIn("    - " + NETWORK + "\n", text)
        self.assertNotIn("cannot use the network", text)
        for kept in ("    - --sandbox workspace-write: write anywhere in the repository\n", "    - --add-dir .git: write in .git too"):
            self.assertIn(kept, text)

    def test_summary_says_how_the_reviewer_was_chosen_and_says_same_family_plainly_with_why(self):
        from sortie_kit import signing
        # Picked, with no agent of another family installed: the builder's own CLI reviews.
        text = signing.summary(self.value(reviewer="codex", family="family-builder", choice="picked-same-family"))
        self.assertIn("Reviewer:   codex, model model-reviewer (family family-builder), headless, started by the host "
                      "for each review\n"
                      "              picked: no agent of another family is installed\n"
                      "              same-family review: the reviewer is of the builder's own family (family-builder), "
                      "because no agent of another family is installed\n"
                      "              /opt/tools/codex exec --sandbox read-only --model model-reviewer --cd "
                      "'/work/target repo/.sortie/shifts/20300101T000000Z-abc/reviews/REVIEW' PROMPT\n", text)
        self.assertIn("    - --sandbox read-only: commands it runs cannot write files.", text)
        # Named by flag, of the builder's family.
        text = signing.summary(self.value(reviewer="claude", family="family-builder", choice="given"))
        self.assertIn("              given with --agent reviewer=\n"
                      "              same-family review: the reviewer is of the builder's own family (family-builder), "
                      "as you named it with --agent reviewer=\n"
                      "              /opt/tools/claude --print --permission-mode plan --model model-reviewer PROMPT\n", text)
        # Named by flag, of another family: said as given, and not as same-family.
        text = signing.summary(self.value(reviewer="claude", family="claude", choice="given"))
        self.assertIn("              given with --agent reviewer=\n              /opt/tools/claude ", text)
        self.assertNotIn("same-family", text)
        self.assertIn("One run may take at most 2m 30s;", signing.summary(self.value(review_limit_seconds=150)))

    def test_summary_says_when_no_review_can_start_in_the_last_part_of_the_window(self):
        """A review started in wind-down must fit whole in its working time. Where the limit
        does not, no review starts there, and the signer is told before signing."""
        from sortie_kit import signing
        import datetime as dt
        until = lambda seconds: (dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
                                 + dt.timedelta(seconds=seconds)).isoformat()
        said = lambda seconds, **changes: signing.summary(self.value(until=until(seconds), **changes))
        # Six minutes, as the shift in which no review could start: 600 s against 120 s less 10.
        self.assertIn(
            "              One run may take at most 10m; a run that gives nothing usable is\n"
            "              made once more, and a second failure blocks that task.\n"
            "              No review starts in the last 2m of this shift: a review must fit whole,\n"
            "              the limit is 10m and only 1m 50s of working time is left then.\n"
            "              A plan or commit handed in then stays not reviewed, and its task is not accepted.\n"
            "              A shorter --review-limit or a longer --for changes that.\n"
            "  What the builder may do:\n", said(360))
        self.assertIn("No review starts in the last 10m of this shift: a review must fit whole,\n"
                      "              the limit is 10m and only 9m 50s of working time is left then.\n", said(1800))
        # The limit fits: 610 s of wind-down less 10, and any longer window.
        for seconds, changes in ((1830, {}), (2700, {}), (360, dict(review_limit_seconds=60)),
                                 (360, dict(review_limit_seconds=110)), (7200, dict(review_limit_seconds=1190))):
            self.assertNotIn("No review starts", said(seconds, **changes), (seconds, changes))
        self.assertIn("No review starts", said(360, review_limit_seconds=111))
        # A wind-down is never longer than twenty minutes: above its working time no longer window helps.
        text = said(7200, review_limit_seconds=1800)
        self.assertIn("No review starts in the last 20m of this shift: a review must fit whole,\n"
                      "              the limit is 30m and only 19m 50s of working time is left then.\n", text)
        self.assertIn("              Only a --review-limit of 19m 50s or less changes that.\n", text)
        self.assertNotIn("longer --for", text)
        # Finding 5 of the agy read. Such a limit in a shorter shift: that much less is not enough there.
        text = said(1800, review_limit_seconds=1800)
        self.assertIn("the limit is 30m and only 9m 50s of working time is left then.\n", text)
        self.assertIn("              A --review-limit of 9m 50s or less changes that, or one of 19m 50s or less\n"
                      "              with a longer --for.\n", text)
        # And a shift so short that no limit a person can sign fits: the shortest is 30 seconds.
        text = said(60)
        self.assertIn("No review starts in the last 20s of this shift: a review must fit whole,\n"
                      "              the limit is 10m and only 10s of working time is left then.\n", text)
        self.assertIn("              Only a longer --for changes that: no --review-limit is shorter than 30s.\n", text)
        self.assertIn("Only a longer --for changes that", said(60, review_limit_seconds=30))
        self.assertIn("Only a longer --for changes that", said(60, review_limit_seconds=1190))
        # Second Minor of the merge gate: with a limit above what any wind-down holds, a longer shift alone does not.
        for limit in (1191, 3600):
            text = said(60, review_limit_seconds=limit)
            self.assertIn("              Only a longer --for together with a --review-limit of 19m 50s or less changes that.\n", text)
            self.assertNotIn("Only a longer --for changes that", text)
        self.assertIn("A shorter --review-limit or a longer --for changes that.", said(120))  # 30 s of working time
        # A path that is shown with escapes is not the path, so no ready command is offered for it.
        odd = signing.summary(self.value(repo="/work/r\u00e9po"))
        self.assertIn("Shift:      /work/r\\u00e9po/.sortie/shifts/20300101T000000Z-abc\n", odd)
        self.assertNotIn("sortie stop --shift", odd)
        self.assertIn("sortie stop with this shift's directory", odd)
        self.assertEqual([signing.span(s) for s in (0.4, 45, 600, 5400, 14400, 3661)],
                         ["0s", "45s", "10m", "1h 30m", "4h", "1h 1m"])

    def test_summary_of_a_shift_with_agy_driving_and_building_shows_both_commands_and_what_the_builder_may_do(self):
        from sortie_kit import grants, signing
        agy = agents.load("agy", self.root, home=self.root / "no-home")
        pin = {"path": "/opt/tools/agy", "sha256": "0" * 64, "bytes": 1}
        seats = dict(self.value(reviewer="codex", family="gpt")["seats"],
                     driver=agents.seat("driver", agy, "gemini-4-neon-high", "gemini", pin),
                     builder=agents.seat("builder", agy, "claude-sonnet-5-5-high", "claude", pin))
        text = signing.summary(self.value(reviewer="codex", family="gpt", seats=seats, review_policy=grants.review_policy(
            "claude-sonnet-5-5-high", "claude", "model-reviewer", "gpt", "picked-other-family")))
        for expected in (
                "Driver:     agy, model gemini-4-neon-high (family gemini), in this terminal\n"
                "              /opt/tools/agy --prompt-interactive PROMPT --model gemini-4-neon-high\n",
                "Builder:    agy, model claude-sonnet-5-5-high (family claude), headless, one turn at a time\n"
                "              /opt/tools/agy --print PROMPT --model claude-sonnet-5-5-high\n",
                "  What the builder may do:\n"
                "    - no permission, mode or sandbox option is passed: it can do whatever your own agy configuration "
                "allows without asking\n"
                "    - anything your own agy configuration allows on top still applies; sortie-kit does not read or "
                "limit it\n"
                "  What the reviewer can reach:\n"):
            self.assertIn(expected, text)
        self.assertNotIn("same-family", text)

    def test_summary_cannot_carry_terminal_control_or_non_ascii_text(self):
        from sortie_kit import signing
        text = signing.summary(self.value(repo="/work/\x1b[2J\x07\u202e\nrepo"))
        self.assertTrue(text.isascii())
        self.assertFalse(set(text) & set(map(chr, range(32))) - {"\n"})
        self.assertIn("Repository: /work/\\u001b[2J\\u0007\\u202e\\nrepo\n", text)

    def test_reach_says_of_the_built_in_codex_builder_whether_it_can_use_the_network(self):
        plain, wide = (self.value(network=n)["seats"]["builder"] for n in (False, True))
        self.assertIn(NO_NETWORK, agents.reach("builder", plain))
        self.assertIn(NETWORK, agents.reach("builder", wide))
        for seat, absent in ((plain, "can use the network"), (wide, "cannot use the network")):
            self.assertNotIn(absent, " ".join(agents.reach("builder", seat)))
        # Neither sentence says sortie-kit stops or allows anything itself.
        self.assertIn("sortie-kit does not enforce it", NO_NETWORK)
        self.assertIn("what the option asks of the CLI; sortie-kit has not checked it", NETWORK)
        # A built-in codex seat without the workspace-write sandbox option says nothing of the network.
        for argv in (["exec", "--model", "{model}", "{prompt}"], ["exec", "--add-dir", ".git", "--model", "{model}", "{prompt}"]):
            self.assertNotIn("network", " ".join(agents.reach("builder", dict(plain, argv=argv))))
        # A grant signed by an earlier version has the option in the one headless form it had: said the same way.
        old = dict(plain, profile_sha256="1" * 64, argv=list(wide["argv"]))
        self.assertIn(NETWORK, agents.reach("builder", old))
        # A profile of one's own that runs codex: only the options found are said, as before, and
        # nothing is said of a network it does not name.
        own = dict(wide, agent="mine")
        lines = agents.reach("builder", own)
        self.assertIn("-c sandbox_workspace_write.network_access=true: use the network", lines)
        self.assertNotIn(NETWORK, lines)
        self.assertNotIn("network", " ".join(agents.reach("builder", dict(plain, agent="mine"))))
        # The claude builder is passed no such option, and its words are what they were.
        claude = agents.seat("builder", agents.load("claude", self.root, home=self.root / "no-home"), "m", "claude",
                             {"path": "/opt/tools/claude", "sha256": "0" * 64, "bytes": 1})
        self.assertNotIn("network", " ".join(agents.reach("builder", claude)))

    def test_reach_describes_only_the_signed_arguments(self):
        value = self.value()
        builder = value["seats"]["builder"]
        stripped = dict(builder, argv=["exec", "--model", "{model}", "{prompt}"])
        self.assertEqual(agents.reach("builder", stripped)[0],
                         "no sandbox option is passed: what it can write is whatever your own codex configuration says")
        headless = agents.seat("builder", agents.load("claude", self.root, home=self.root / "no-home"), "m", "claude",
                               {"path": "/opt/tools/claude", "sha256": "0" * 64, "bytes": 1})
        self.assertIn("only what your Claude settings allow without asking", agents.reach("builder", headless)[0])
        # The built-in agy builder: what its form does not pass is known and said, and no more.
        agy = agents.load("agy", self.root, home=self.root / "no-home")
        pin = {"path": "/opt/tools/agy", "sha256": "0" * 64, "bytes": 1}
        self.assertEqual(agents.reach("builder", agents.seat("builder", agy, "gemini-4-neon-high", "gemini", pin)),
                         ["no permission, mode or sandbox option is passed: it can do whatever your own agy "
                          "configuration allows without asking",
                          "anything your own agy configuration allows on top still applies; "
                          "sortie-kit does not read or limit it"])
        # Nothing is said of a driver beyond the one line every seat gets, agy as any other.
        self.assertEqual(agents.reach("driver", agents.seat("driver", agy, "gemini-4-neon-high", "gemini", pin)),
                         ["anything your own agy configuration allows on top still applies; "
                          "sortie-kit does not read or limit it"])
        # The line is said of the form it was written for and of no other. A seat named agy whose
        # signed arguments are not that form, as a later version's might be, is not described by it.
        for other in (["--print", "{prompt}", "--model", "{model}", "--dangerously-skip-permissions"],
                      ["--print", "{prompt}", "--mode", "plan", "--sandbox", "--model", "{model}"],
                      ["--print", "--model", "{model}", "{prompt}"]):
            changed = dict(agents.seat("builder", agy, "gemini-4-neon-high", "gemini", pin), argv=other)
            self.assertEqual(agents.reach("builder", changed),
                             ["sortie-kit has not inspected what these arguments allow",
                              "anything your own agy configuration allows on top still applies; "
                              "sortie-kit does not read or limit it"], other)
        # A profile of one's own on the agy program is not the built-in one: its arguments were not read.
        mine = dict(agents.seat("builder", agy, "gemini-4-neon-high", "gemini", pin), agent="my-agy")
        self.assertEqual(agents.reach("builder", mine)[0],
                         "a profile of your own: sortie-kit has not inspected what these arguments allow")
        self.assertEqual(len(agents.reach("driver", value["seats"]["driver"])), 1)
        self.assertNotIn("has not inspected", " ".join(agents.reach("driver", value["seats"]["driver"])))
        other = dict(builder, agent="mine", executable=dict(builder["executable"], path="/opt/tools/mine"))
        self.assertEqual(agents.reach("builder", other),
                         ["a profile of your own: sortie-kit has not inspected what these arguments allow",
                          "anything your own mine configuration allows on top still applies; "
                          "sortie-kit does not read or limit it"])
