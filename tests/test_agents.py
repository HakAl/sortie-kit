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

    def test_packaged_profiles_are_valid_and_agy_starts_no_seat(self):
        self.assertEqual(agents.available(self.target, home=self.home), ["agy", "claude", "codex"])
        for name in ("claude", "codex", "agy"):
            found = agents.load(name, self.target, home=self.home)
            self.assertEqual((found["value"]["name"], found["source"]), (name, "built-in"))
            self.assertIsNone(found["value"]["default_model"])  # no model is ever assumed
        pin = {"path": "/x/agy", "sha256": "0" * 64, "bytes": 1}
        agy = agents.load("agy", self.target, home=self.home)
        for role in ("driver", "builder"):
            self.refused("cannot be the " + role, agents.seat, role, agy, "gemini-4-carbon-high", "gemini", pin)
        self.assertEqual(agents.choose_model(agy, "gemini-4-carbon-high"), ("gemini-4-carbon-high", "gemini"))

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
        for name in ("claude", "codex"):
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
        claude = agents.load("claude", self.target, home=self.home)
        for profile_ in (claude, self.loaded()):
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
        # agy has no headless start at all: that is the reason given, not the missing network form.
        self.refused("has no headless start and cannot be the builder", agents.seat, "builder",
                     agents.load("agy", self.target, home=self.home), "m", "gemini", pin, network=True)
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
        # A stated family still decides for a model the profile does not list, and never for one it does.
        self.assertEqual(agents.choose_model(found, "new-model", "GPT"), ("new-model", "gpt"))
        self.refused("contradicts", agents.choose_model, found, "fixture-model", "gpt")
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
        self.assertEqual(agents.choose_model(found, "new-model", "openai"), ("new-model", "gpt"))
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
        # A path that is shown with escapes is not the path, so no ready command is offered for it.
        odd = signing.summary(self.value(repo="/work/r\u00e9po"))
        self.assertIn("Shift:      /work/r\\u00e9po/.sortie/shifts/20300101T000000Z-abc\n", odd)
        self.assertNotIn("sortie stop --shift", odd)
        self.assertIn("sortie stop with this shift's directory", odd)
        self.assertEqual([signing.span(s) for s in (0.4, 45, 600, 5400, 14400, 3661)],
                         ["0s", "45s", "10m", "1h 30m", "4h", "1h 1m"])

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
        self.assertEqual(len(agents.reach("driver", value["seats"]["driver"])), 1)
        self.assertNotIn("has not inspected", " ".join(agents.reach("driver", value["seats"]["driver"])))
        other = dict(builder, agent="mine", executable=dict(builder["executable"], path="/opt/tools/mine"))
        self.assertEqual(agents.reach("builder", other),
                         ["a profile of your own: sortie-kit has not inspected what these arguments allow",
                          "anything your own mine configuration allows on top still applies; "
                          "sortie-kit does not read or limit it"])
