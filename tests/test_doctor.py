"""`sortie doctor`: what a start on this machine would find, with no model call.

No real agent CLI is started. The programs named claude, codex and agy here are small
scripts of the test's own in a directory given as the search path; each records how it
was called. No key is made: the key file is a synthetic OpenSSH envelope.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import time
from unittest.mock import patch

from sortie_kit import agents, cli, doctor, launcher
from support import LocalTest
from test_setup import BLOB, envelope

CLAUDE_HELP = """Usage: claude [options] [prompt]
  -p, --print                 Print the response and exit
  --model <model>             Model for the session
  --permission-mode <mode>    Permission mode to use
"""
CODEX_HELP = """Usage: codex [OPTIONS] [PROMPT]
Commands:
  exec    Run non-interactively
Options:
  -m, --model <MODEL>
  -C, --cd <DIR>
"""
CODEX_EXEC_HELP = """Usage: codex exec [OPTIONS]
  -c, --config <key=value>
  -m, --model <MODEL>
  -s, --sandbox <SANDBOX_MODE>
  -C, --cd <DIR>
      --add-dir <DIR>
"""
AGY_HELP = """Usage of agy:
  --mode                 Set the agent execution mode
  --model                Model for the session
  --print                Run a single prompt
  --prompt-interactive   Run an initial prompt interactively
  --sandbox              Run in a sandbox
"""


def tree(top):
    """Every name under `top` with its kind, size and time of last change."""
    seen = {}
    for folder, names, files in os.walk(top):
        for name in names + files:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            seen[path] = (info.st_mode, info.st_size, info.st_mtime_ns)
    return seen


class DoctorFixture(LocalTest):
    def setUp(self):
        super().setUp()
        self.target = self.repo()
        self.home = self.root / "home"
        self.config = self.home / ".config" / "sortie-kit"
        self.config.mkdir(parents=True, mode=0o700)
        self.own = agents.user_directory(self.home)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.record = self.root / "calls.txt"
        for name in ("git", "ssh-keygen"):
            self.tool(name)

    def tool(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\nexit 9\n")
        path.chmod(0o755)
        return path

    def program(self, name, helps=None, *, version="made-up 1.0", stream="1", body=None):
        """A stand-in CLI. `helps` maps the words before --help, as one string, to the
        text printed; any other call exits 2 with an error line."""
        lines = ["#!/bin/sh",
                 'echo "$PWD|${SSH_AUTH_SOCK-unset}|${GIT_DIR-unset}|$*" >> ' + shlex.quote(str(self.record))]
        if body:
            lines.append(body)
        lines.append('case "$*" in')
        if version is not None:
            lines.append('  "--version") echo ' + shlex.quote(version) + " >&" + stream + ";;")
        for words, text in (helps or {}).items():
            file = self.root / (name + "-help-" + (words.replace(" ", "-") or "top") + ".txt")
            file.write_text(text)
            lines.append('  "' + (words + " --help").strip() + '") cat ' + shlex.quote(str(file)) + " >&" + stream + ";;")
        lines.append('  *) echo "error: unexpected argument: $*" >&2; exit 2;;')
        lines.append("esac")
        path = self.bin / name
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o755)
        return path

    def three(self):
        self.program("claude", {"": CLAUDE_HELP})
        self.program("codex", {"": CODEX_HELP, "exec": CODEX_EXEC_HELP})
        self.program("agy", {"": AGY_HELP}, stream="2")

    def keys(self, *, trust=True, key=True, public=BLOB):
        if key:
            (self.config / "signing-key").write_bytes(envelope())
            (self.config / "signing-key").chmod(0o600)
        if trust:
            (self.config / "trust.json").write_text(json.dumps({
                "version": 1, "identity": "developer", "public_key": "ssh-ed25519 " + public,
                "ssh_keygen": str(Path(os.path.realpath(self.bin / "ssh-keygen")))}))
            (self.config / "trust.json").chmod(0o600)

    def profile(self, name, **changes):
        value = dict(version=1, name=name, executable=name + "-agent",
                     interactive_argv=["--model", "{model}", "{prompt}"],
                     headless_argv=["--fast", "--model", "{model}", "{prompt}"],
                     models={name + "-model": name + "-family"}, default_model=None)
        value.update(changes)
        self.own.mkdir(parents=True, exist_ok=True)
        (self.own / (name + ".json")).write_text(json.dumps(value))
        return value

    def doctor(self, *names, repo=..., bound=5.0, **more):
        lines, code = doctor.run(names, self.target if repo is ... else repo, home=self.home,
                                 search=str(self.bin), bound=bound, **more)
        self.text = "\n".join(lines)
        self.assertIsNone(re.search(r"\bok\b", self.text, re.IGNORECASE), self.text)
        return lines, code

    def calls(self):
        return self.record.read_text().splitlines() if self.record.exists() else []

    def block(self, name):
        """The lines of one profile's block."""
        lines, taking, found = self.text.splitlines(), False, []
        for line in lines:
            if line.startswith(name + " ("):
                taking = True
            elif taking and not line.startswith("  "):
                break
            if taking:
                found.append(line)
        return "\n".join(found)


class FlagTests(DoctorFixture):
    def test_every_flag_of_the_built_in_profiles_is_listed_and_nothing_is_left_to_do(self):
        self.three()
        self.keys()
        lines, code = self.doctor()
        self.assertEqual(code, 0, self.text)
        self.assertIn("driver form (interactive): --model listed", self.block("claude"))
        self.assertIn("reviewer form (review): --print listed, --permission-mode listed, --model listed",
                      self.block("claude"))
        self.assertIn("builder form (headless): subcommand exec listed, --sandbox listed, --add-dir listed, "
                      "--model listed, --cd listed", self.block("codex"))
        self.assertIn("builder form with the network on: subcommand exec listed, --sandbox listed, --add-dir listed, "
                      "-c listed, --model listed, --cd listed", self.block("codex"))
        self.assertIn("reviewer form (review): --print listed, --mode listed, --sandbox listed, --model listed",
                      self.block("agy"))
        self.assertIn("Version it prints: made-up 1.0", self.block("claude"))
        self.assertIn("Nothing found to do. This does not show that a shift will run", self.text)
        self.assertIn('"listed" means that the flag\'s name appears in the CLI\'s own help text', self.text)
        self.assertNotIn("not found in the help", self.text)

    def test_a_flag_absent_from_the_help_is_said_and_is_a_thing_to_do(self):
        self.three()
        self.keys()
        self.program("codex", {"": CODEX_HELP, "exec": CODEX_EXEC_HELP.replace("      --add-dir <DIR>\n", "")})
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("--add-dir not found in the help", self.block("codex"))
        todo = self.text.split("To do before a shift:")[1]
        self.assertIn("codex: --add-dir is not found in the help of codex exec", todo)

    def test_a_flag_that_is_only_part_of_a_longer_one_is_not_listed(self):
        self.three()
        self.keys()
        self.program("claude", {"": CLAUDE_HELP.replace("--model <model>", "--model-list <x>")})
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("driver form (interactive): --model not found in the help", self.block("claude"))

    def test_the_subcommand_word_missing_from_the_top_help_is_said(self):
        self.three()
        self.keys()
        self.program("codex", {"": CODEX_HELP.replace("  exec    Run non-interactively\n", ""),
                               "exec": CODEX_EXEC_HELP})
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("subcommand exec not found in the help", self.block("codex"))

    def test_help_on_standard_error_is_read_and_the_calls_are_the_expected_ones(self):
        self.three()
        self.keys()
        self.doctor()
        said = [line.split("|")[3] for line in self.calls()]
        self.assertEqual(sorted(said), sorted(["--version", "--help"] * 3 + ["exec --help"]))

    def test_calls_run_in_the_root_directory_without_the_variables_a_seat_does_not_get(self):
        self.three()
        self.keys()
        with patch.dict(os.environ, SSH_AUTH_SOCK="/nowhere/agent", GIT_DIR="/nowhere/git"):
            self.doctor()
        self.assertTrue(self.calls())
        for line in self.calls():
            self.assertEqual(line.split("|")[:3], ["/", "unset", "unset"], line)

    def test_what_a_flag_is(self):
        self.assertEqual(doctor.flags(["exec", "--a", "v", "--b=1", "-c=2", "-", "-5", "{model}", "--a", "--", "--z"]),
                         ["--a", "--b", "-c"])
        self.assertEqual(doctor.words(["exec", "sub", "--a", "later"]), ["exec", "sub"])
        self.assertEqual(doctor.words(["{prompt}", "exec"]), [])
        self.assertTrue(doctor.listed("-c", "  -c, --config <k=v>"))
        self.assertTrue(doctor.listed("--cd", "use --cd=DIR"))
        self.assertFalse(doctor.listed("-c", "  --no-c  -cd  x-c"))


class FailingHelpTests(DoctorFixture):
    def test_a_help_that_exits_with_an_error_is_not_known_and_its_text_is_not_read_as_help(self):
        self.three()
        self.keys()
        self.program("claude", body='if [ "$*" = "--help" ]; then echo "error: --model and --print unknown" >&2; exit 2; fi')
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        block = self.block("claude")
        self.assertIn("help not known (claude --help ended with exit 2: error: --model and --print unknown)", block)
        self.assertNotIn("listed", block)
        self.assertNotIn("not found in the help", block)
        self.assertIn("claude: its help could not be read", self.text.split("To do before a shift:")[1])

    def test_a_cli_that_hangs_on_help_is_stopped_named_and_passed_over(self):
        self.three()
        self.keys()
        self.program("claude", body='if [ "$*" = "--help" ]; then sleep 30 & wait; fi')
        began = time.monotonic()
        lines, code = self.doctor(bound=0.5)
        self.assertLess(time.monotonic() - began, 15)
        self.assertEqual(code, 1)
        self.assertIn("claude --help did not end within 0.5s and was stopped; its flags are not known",
                      self.block("claude"))
        self.assertIn("Version it prints: made-up 1.0", self.block("claude"))
        self.assertIn("--model listed", self.block("agy"))  # the next profile is still reported

    def test_a_cli_that_hangs_on_version_is_said_and_its_help_is_still_asked(self):
        self.three()
        self.keys()
        self.program("claude", {"": CLAUDE_HELP}, body='if [ "$*" = "--version" ]; then sleep 30 & wait; fi')
        lines, code = self.doctor(bound=0.5)
        self.assertIn("Version it prints: claude --version did not end within 0.5s and was stopped",
                      self.block("claude"))
        self.assertIn("driver form (interactive): --model listed", self.block("claude"))
        self.assertEqual(code, 0, self.text)  # a version that was not printed stops no shift

    def test_output_beyond_what_is_kept_is_cut_and_the_call_still_ends(self):
        self.three()
        self.keys()
        self.program("claude", {"": CLAUDE_HELP},
                     body='if [ "$*" = "--help" ]; then head -c 3000000 /dev/zero | tr "\\0" "x"; fi')
        lines, code = self.doctor()
        self.assertEqual(code, 1)  # the flags lie beyond what was kept
        self.assertIn("--model not known", self.block("claude"))
        self.assertLess(len(self.text), 100_000)

    def test_a_version_line_with_control_characters_is_cut_and_shown_inert_by_the_command(self):
        self.three()
        self.keys()
        self.program("claude", {"": CLAUDE_HELP}, version="\n\x1b[31mred " + "v" * 400)
        lines, code = self.doctor()
        line = next(l for l in lines if "Version it prints" in l and "red" in l)
        self.assertLessEqual(len(line), 260)
        self.assertNotIn("\x1b", cli.safe(line))

    def test_a_version_that_is_not_printed_is_said(self):
        self.three()
        self.keys()
        self.program("claude", {"": CLAUDE_HELP}, version=None)
        lines, code = self.doctor()
        self.assertIn("Version it prints: did not print one (claude --version ended with exit 2)", self.block("claude"))
        self.assertEqual(code, 0, self.text)


class ProgramTests(DoctorFixture):
    def test_a_built_in_cli_that_is_not_installed_is_said_and_is_nothing_to_do_by_itself(self):
        self.program("claude", {"": CLAUDE_HELP})
        self.program("codex", {"": CODEX_HELP, "exec": CODEX_EXEC_HELP})
        self.keys()
        lines, code = self.doctor()
        self.assertEqual(code, 0, self.text)
        self.assertEqual(self.block("agy"), "agy (built-in profile)\n  Program: agy, not found on your PATH")

    def test_no_cli_at_all_is_one_line_to_do(self):
        self.keys()
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        todo = self.text.split("To do before a shift:")[1]
        self.assertEqual(len([l for l in todo.splitlines() if l.startswith("- ")]), 1, todo)
        self.assertIn("Install and sign in to an agent CLI", todo)
        self.assertIn("No agent CLI was found, so nobody would build or review.", self.text)
        self.assertEqual(self.calls(), [])

    def test_a_script_and_a_file_that_is_no_script_are_told_apart(self):
        self.three()
        self.keys()
        self.doctor()
        self.assertIn("a script: the pin covers that one file and not its interpreter or any program it starts",
                      self.block("codex"))
        self.assertIn("File that would be pinned: " + os.path.realpath(self.bin / "codex"), self.block("codex"))
        self.assertRegex(self.block("codex"), r"sha256 [0-9a-f]{12}\.\.\., \d+ bytes")
        self.assertTrue(doctor.kind({"path": "/bin/ls"}).startswith("not a script"))

    def test_a_program_that_start_would_refuse_is_said_and_never_run(self):
        self.three()
        self.keys()
        (self.bin / "claude").chmod(0o775)
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("sortie start would refuse this program: agent executable must be executable by you and not "
                      "group or world writable", self.block("claude"))
        self.assertNotIn("Version", self.block("claude"))
        self.assertFalse([l for l in self.calls() if "claude" in l], self.calls())
        self.assertEqual(len([l for l in self.calls() if l.endswith("|--version")]), 2)

    def test_a_program_inside_the_repository_is_refused_and_never_run(self):
        self.keys()
        inside = self.target / "tools"
        inside.mkdir()
        for name in ("git", "ssh-keygen"):
            (inside / name).write_text("#!/bin/sh\nexit 9\n")
            (inside / name).chmod(0o755)
        self.bin = inside
        self.three()
        lines, code = doctor.run((), self.target, home=self.home, search=str(inside), bound=5.0)
        text = "\n".join(lines)
        self.assertEqual(code, 1)
        self.assertEqual(text.count("sortie start would refuse this program: agent executable must be outside the "
                                    "target repo"), 3)
        self.assertEqual(self.calls(), [])
        # Without a repository named, nothing is held against one, and the output says so.
        lines, code = doctor.run((), None, home=self.home, search=str(inside), bound=5.0)
        self.assertIn("No repository was named with --repo", "\n".join(lines))
        self.assertTrue(self.calls())


class OwnProfileTests(DoctorFixture):
    def test_a_profile_of_ones_own_is_looked_at_only_when_named_and_never_called_with_a_leading_word(self):
        self.three()
        self.keys()
        self.profile("mine", headless_argv=["run", "--fast", "--deep", "--model", "{model}", "{prompt}"])
        self.program("mine-agent", {"": "usage: mine-agent run [--fast] [--model M]\n"})
        self.doctor()
        self.assertFalse([l for l in self.calls() if l.endswith("|run --help")])
        self.assertEqual(self.block("mine"), "")
        self.assertEqual(len(self.calls()), 7)  # the three built-in ones only: this one was not run
        lines, code = self.doctor("mine")
        block = self.block("mine")
        self.assertIn("mine (your own profile, " + str(self.own / "mine.json") + ")", block)
        self.assertIn("This is your own program; sortie-kit runs it with --version and with --help and cannot know "
                      "what it does with them.", block)
        self.assertIn("driver form (interactive): --model listed", block)
        self.assertIn('builder form (headless): --fast listed, --deep not known from the top help (the help of "run" '
                      "was not asked for), --model listed", block)
        self.assertEqual(code, 0, self.text)
        mine = [l.split("|")[3] for l in self.calls()][-2:]
        self.assertEqual(sorted(mine), ["--help", "--version"])

    def test_an_own_form_without_a_leading_word_can_be_not_found(self):
        self.three()
        self.keys()
        self.profile("mine")
        self.program("mine-agent", {"": "usage: mine-agent [--model M]\n"})
        lines, code = self.doctor("mine")
        self.assertEqual(code, 1)
        self.assertIn("builder form (headless): --fast not found in the help, --model listed", self.block("mine"))

    def test_a_named_profile_that_does_not_load_is_a_thing_to_do(self):
        self.three()
        self.keys()
        lines, code = self.doctor("nope")
        self.assertEqual(code, 1)
        self.assertIn("nope: no agent profile named nope", self.text)
        self.assertIn("claude (built-in profile)", self.text)

    def test_a_file_of_ones_own_under_a_built_in_name_is_said_and_the_report_is_complete(self):
        self.three()
        self.keys()
        self.profile("claude")
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("claude: sortie start would refuse this profile: custom agent profile reuses a built-in name: "
                      "claude", self.text)
        self.assertIn("--model listed", self.block("agy"))
        self.assertIn("To do before a shift:", self.text)


class ReviewerTests(DoctorFixture):
    def test_the_lines_say_what_the_start_would_pick(self):
        self.three()
        self.keys()
        self.doctor()
        for name, family in (("claude", "claude"), ("codex", "gpt"), ("agy", "gemini"), ("agy", "claude"), ("agy", "gpt")):
            seat, choice = launcher.pick_reviewer(self.target, {"agent": name, "family": family}, home=self.home,
                                                  search=str(self.bin))
            self.assertEqual(choice, "picked-other-family")
            self.assertIn(name + " as the builder (family " + family + ") would be reviewed by " + seat["agent"] + ", "
                          + seat["model"] + " (family " + seat["family"] + "): another family.", self.text)

    def test_one_cli_alone_reviews_its_own_family_and_that_is_said(self):
        self.program("claude", {"": CLAUDE_HELP})
        self.keys()
        lines, code = self.doctor()
        self.assertEqual(code, 0, self.text)
        self.assertIn("claude as the builder (family claude) would be reviewed by claude, claude-sonnet-5-5 (family "
                      "claude): the same family, since no installed agent of another family can review.", self.text)

    def test_nobody_to_review_is_said_once_in_the_words_of_the_start(self):
        self.keys()
        self.profile("mine")
        self.program("mine-agent", {"": "usage: mine-agent [--fast] [--model M]\n"})
        lines, code = self.doctor("mine")
        self.assertEqual(code, 1)
        self.assertEqual(self.text.count("no installed agent can review: none of"), 1)
        self.assertIn("Nobody would review", self.text.split("To do before a shift:")[1])

    def test_a_picked_reviewer_that_cannot_be_pinned_is_said_and_the_report_is_complete(self):
        self.three()
        self.keys()
        (self.bin / "claude").chmod(0o775)  # the first in the order of the pick, for a codex builder
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("codex as the builder (family gpt): sortie start would refuse: reviewer claude: agent executable must "
                      "be executable by you", self.text)
        self.assertIn("To do before a shift:", self.text)

    def test_families_are_canonical_and_each_is_said_once(self):
        self.three()
        self.keys()
        self.profile("mine", models={"a": "openai", "b": "gpt"})
        self.program("mine-agent", {"": "usage: mine-agent [--fast] [--model M]\n"})
        self.doctor("mine")
        self.assertEqual(self.text.count("mine as the builder (family gpt)"), 1)
        self.assertNotIn("family openai", self.text)

    def test_a_builder_profile_that_names_no_family_is_said(self):
        self.three()
        self.keys()
        self.profile("mine", models={})
        self.program("mine-agent", {"": "usage: mine-agent [--fast] [--model M]\n"})
        self.doctor("mine")
        self.assertIn("mine as the builder: its family is whatever you state with --family; who would review it is not "
                      "shown here.", self.text)

    def test_the_file_of_a_reviewer_is_not_read_again_for_each_line(self):
        self.three()
        self.keys()
        real, seen = agents.hash_executable, []
        def counted(path, repo):
            seen.append(str(path))
            return real(path, repo)
        with patch.object(agents, "hash_executable", counted):
            self.doctor()
        self.assertEqual(sorted(seen), sorted(os.path.realpath(self.bin / n) for n in ("claude", "codex", "agy")))


class KeyTests(DoctorFixture):
    def test_a_first_run_before_setup_has_exactly_one_thing_to_do(self):
        self.program("claude", {"": CLAUDE_HELP})
        self.program("codex", {"": CODEX_HELP, "exec": CODEX_EXEC_HELP})
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("Signing key and trust file: none at " + str(self.config / "signing-key") + " and "
                      + str(self.config / "trust.json"), self.text)
        todo = [l for l in self.text.split("To do before a shift:")[1].splitlines() if l.startswith("- ")]
        self.assertEqual(todo, ["- Run sortie setup once in your own terminal: it makes the signing key and the trust "
                                "file."])

    def test_a_key_and_a_trust_file_are_found_without_the_public_key_file(self):
        self.three()
        self.keys()
        self.assertFalse((self.config / "signing-key.pub").exists())
        lines, code = self.doctor()
        self.assertEqual(code, 0, self.text)
        self.assertIn("Signing key and trust file: found at " + str(self.config / "signing-key") + " and "
                      + str(self.config / "trust.json") + ", and the trust file names this key.", self.text)
        self.assertIn("Not shown here: that you know the key's passphrase, and that each CLI is signed in.", self.text)

    def test_a_trust_file_that_names_another_key_is_what_the_start_would_refuse(self):
        import base64
        self.three()
        self.keys(public=base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(32)).decode())
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("sortie start would refuse them: private key does not match trusted public key", self.text)

    def test_one_of_the_two_alone_is_said_as_that(self):
        self.three()
        self.keys(trust=False)
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("Signing key: found at " + str(self.config / "signing-key") + ". Trust file: none at "
                      + str(self.config / "trust.json") + ".", self.text)
        (self.config / "signing-key").unlink()
        self.keys(key=False)
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("Trust file: found at " + str(self.config / "trust.json") + ". Signing key: none at "
                      + str(self.config / "signing-key") + ".", self.text)

    def test_a_link_that_leads_nowhere_is_not_absent(self):
        self.three()
        self.keys(key=False)
        os.symlink(self.root / "gone", self.config / "signing-key")
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("sortie start would refuse them:", self.text)
        self.assertNotIn("none at", self.text)

    def test_a_home_inside_the_repository_is_refused_as_the_start_refuses_it(self):
        self.three()
        self.keys()
        lines, code = self.doctor(repo=self.root)
        self.assertEqual(code, 1)
        self.assertIn("sortie start would refuse them: key and trust must be outside the target repo", self.text)

    def test_a_tool_that_is_not_found_is_a_thing_to_do(self):
        self.three()
        self.keys()
        (self.bin / "git").unlink()
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("git: not found on your PATH", self.text)


class WholeTests(DoctorFixture):
    def test_nothing_is_written(self):
        self.three()
        self.keys()
        self.profile("mine")
        self.program("mine-agent", {"": "usage: mine-agent [--fast] [--model M]\n"})
        self.record = self.root / "elsewhere" / "calls.txt"  # the stand-ins' own record is not the doctor's doing
        self.record.parent.mkdir()
        self.three()
        self.program("mine-agent", {"": "usage: mine-agent [--fast] [--model M]\n"})
        before = tree(self.home), tree(self.target), tree(self.bin)
        lines, code = self.doctor("mine")
        self.assertEqual(code, 0, self.text)
        self.assertEqual((tree(self.home), tree(self.target), tree(self.bin)), before)

    def test_the_first_lines_say_what_runs_and_which_sortie_the_path_has(self):
        self.three()
        self.keys()
        import sortie_kit
        lines, code = self.doctor()
        self.assertTrue(lines[0].startswith("sortie-kit " + sortie_kit.__version__ + ", Python "), lines[0])
        self.assertIn("running from " + os.path.dirname(os.path.abspath(sortie_kit.__file__)), lines[0])
        self.assertEqual(lines[1], "The sortie first on your PATH: none.")
        script = self.tool("sortie")
        scripts = self.root / "scripts"
        scripts.mkdir()
        os.symlink(script, scripts / "sortie")  # a link to this installation's script is this installation
        lines, code = self.doctor(scripts=[scripts])
        self.assertEqual(lines[1], "The sortie first on your PATH: " + str(script) + " (this installation).")
        lines, code = self.doctor(scripts=[self.root / "other"])
        self.assertIn(str(script) + " (another file than this installation's; `type sortie` shows what your shell "
                      "runs).", lines[1])
        self.assertEqual(code, 0, self.text)


class CommandTests(DoctorFixture):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + ":/bin:/usr/bin"):
            try:
                code = cli.main(["doctor", *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_the_command_prints_the_report_and_gives_its_exit_code(self):
        self.three()
        code, out, err = self.main("--repo", str(self.target))
        self.assertEqual(code, 1)
        self.assertIn("claude (built-in profile)", out)
        self.assertIn("- Run sortie setup once in your own terminal", out)
        self.keys()
        code, out, err = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("No repository was named with --repo", out)

    def test_wrong_usage_is_exit_2_and_nothing_is_looked_at(self):
        self.three()
        code, out, err = self.main("--agent", "builder=codex:some-model")
        self.assertEqual(code, 2)
        self.assertIn("sortie doctor --agent takes the NAME of a profile alone", err)
        code, out, err = self.main("--repo", str(self.root / "no-such-directory"))
        self.assertEqual(code, 2)
        self.assertIn("is not a directory", err)
        self.assertEqual(self.calls(), [])


def gone(pid, wait=3.0):
    """Whether the process has ended, looked at for a short while."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


class AfterReviewTests(DoctorFixture):
    """What the read of the change found."""

    def test_what_a_hung_cli_started_in_its_group_is_stopped_with_it(self):
        self.three()
        self.keys()
        mark = self.root / "child.pid"
        self.program("claude", body='if [ "$*" = "--help" ]; then sleep 60 & echo $! > ' + shlex.quote(str(mark))
                     + "; wait; fi")
        self.doctor(bound=0.5)
        self.assertIn("claude --help did not end within 0.5s and was stopped", self.block("claude"))
        self.assertTrue(gone(int(mark.read_text())))

    def test_a_helper_that_left_the_group_and_holds_the_pipes_does_not_hold_the_call(self):
        import signal
        import sys
        self.three()
        self.keys()
        mark = self.root / "helper.pid"
        helper = shlex.quote(sys.executable) + " -c 'import os,time; os.setsid(); time.sleep(40)'"
        self.program("claude", {"": CLAUDE_HELP},
                     body='if [ "$*" = "--help" ]; then ' + helper + " & echo $! > " + shlex.quote(str(mark)) + "; fi")
        began = time.monotonic()
        try:
            lines, code = self.doctor(bound=3.0)
            spent = time.monotonic() - began
        finally:
            try:
                os.kill(int(mark.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.assertLess(spent, 12)
        self.assertIn("driver form (interactive): --model listed", self.block("claude"))
        self.assertEqual(code, 0, self.text)

    def test_a_named_profile_whose_program_is_missing_is_a_thing_to_do(self):
        self.three()
        self.keys()
        self.profile("mine")
        lines, code = self.doctor("mine")
        self.assertEqual(code, 1)
        self.assertIn("  Program: mine-agent, not found on your PATH", self.block("mine"))
        self.assertIn("- mine: the program of the profile you named, mine-agent, is not found",
                      self.text.split("To do before a shift:")[1])

    def test_nobody_to_review_is_found_out_when_no_builder_names_a_family(self):
        self.keys()
        self.profile("mine", models={})
        self.program("mine-agent", {"": "usage: mine-agent [--fast] [--model M]\n"})
        lines, code = self.doctor("mine")
        self.assertEqual(code, 1)
        self.assertEqual(self.text.count("no installed agent can review: none of"), 1)
        self.assertIn("Nobody would review", self.text.split("To do before a shift:")[1])

    def test_nobody_to_review_is_found_out_when_nothing_could_build(self):
        self.keys()
        self.profile("mine", headless_argv=None)
        self.program("mine-agent", {"": "usage: mine-agent [--model M]\n"})
        lines, code = self.doctor("mine")
        todo = self.text.split("To do before a shift:")[1]
        self.assertIn("so nothing could be the builder", todo)
        self.assertIn("Nobody would review", todo)

    def test_deeply_nested_json_is_a_refusal_and_the_report_is_complete(self):
        self.three()
        self.keys()
        (self.config / "trust.json").write_text("[" * 100000 + "]" * 100000)
        (self.config / "trust.json").chmod(0o600)
        self.profile("mine")
        (self.own / "mine.json").write_text("[" * 20000 + "]" * 20000)
        lines, code = self.doctor("mine")
        self.assertEqual(code, 1)
        self.assertIn("but sortie start would refuse them:", self.text)
        self.assertIn("To do before a shift:", self.text)
        self.assertIn("claude as the builder", self.text)

    def too_deep(self, mark):
        """`json.loads` as Python 3.11 and 3.13 answer JSON that nests too deep, for the
        one text `mark`: Python 3.14 reads on where those two raise."""
        real = json.loads

        def loads(raw, *args, **more):
            if (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).strip() == mark:
                raise RecursionError("maximum recursion depth exceeded")
            return real(raw, *args, **more)
        return patch.object(json, "loads", side_effect=loads)

    def test_json_that_nests_too_deep_is_refused_as_other_bad_json_whatever_the_python(self):
        from sortie_kit import grants
        from sortie_kit.layout import Refused
        with self.too_deep("[7]"), self.assertRaisesRegex(Refused, "invalid grant/trust JSON"):
            grants.decoded(b"[7]")

    def test_a_profile_that_nests_too_deep_is_left_out_of_the_pick_whatever_the_python(self):
        self.three()
        self.keys()
        self.profile("mine")
        (self.own / "mine.json").write_text("[7]")
        with self.too_deep("[7]"):
            lines, code = self.doctor("mine")
            able = [item["name"] for item in launcher.reviewers(self.target, home=self.home, search=str(self.bin))]
        self.assertEqual(able, ["claude", "codex", "agy"])
        self.assertEqual(code, 1)
        self.assertIn("claude as the builder (family claude) would be reviewed by codex", self.text)
        self.assertNotIn("Nobody would review", self.text)
        self.assertEqual(self.text.split("To do before a shift:")[1].strip().splitlines(),
                         ["- mine: the profile you named does not load (invalid grant/trust JSON). Profiles of "
                          "your own are in " + str(self.own) + "."])

    def test_one_file_alone_is_held_to_what_a_start_requires_of_it(self):
        self.three()
        (self.config / "signing-key").mkdir()
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("Signing key: at " + str(self.config / "signing-key") + ", but sortie start would refuse it:",
                      self.text)
        self.assertNotIn("Signing key: found", self.text)
        (self.config / "signing-key").rmdir()
        (self.config / "trust.json").write_text("{}")
        (self.config / "trust.json").chmod(0o600)
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("Trust file: at " + str(self.config / "trust.json") + ", but sortie start would refuse it: "
                      "unsupported trust configuration", self.text)
        self.assertNotIn("Trust file: found", self.text)

    def test_an_empty_name_before_an_equals_sign_is_no_flag(self):
        self.assertEqual(doctor.flags(["-=x", "--=y", "--real"]), ["--real"])

    def test_a_help_that_prints_nothing_is_not_known(self):
        self.three()
        self.keys()
        self.program("claude", body='if [ "$*" = "--help" ]; then exit 0; fi')
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("help not known (claude --help printed nothing)", self.block("claude"))
        self.assertNotIn("listed", self.block("claude"))

    def test_the_help_of_a_subcommand_that_is_not_listed_is_not_asked_for(self):
        self.three()
        self.keys()
        self.program("codex", {"": CODEX_HELP.replace("  exec    Run non-interactively\n", ""),
                               "exec": CODEX_EXEC_HELP})
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("builder form (headless): subcommand exec not found in the help; the flags after it were not "
                      "looked for", self.block("codex"))
        self.assertFalse([l for l in self.calls() if l.endswith("|exec --help")], self.calls())

    def test_each_thing_to_do_is_said_once_and_a_profile_named_twice_is_looked_at_once(self):
        self.three()
        self.keys()
        self.program("codex", {"": CODEX_HELP, "exec": CODEX_EXEC_HELP.replace("      --add-dir <DIR>\n", "")})
        self.profile("mine")
        self.program("mine-agent", {"": "usage: mine-agent [--fast] [--model M]\n"})
        lines, code = self.doctor("mine", "mine")
        todo = [l for l in self.text.split("To do before a shift:")[1].splitlines() if l.startswith("- ")]
        self.assertEqual(len(todo), len(set(todo)), todo)
        self.assertEqual(len([l for l in todo if "--add-dir" in l]), 1, todo)
        self.assertEqual(self.text.count("mine (your own profile"), 1)
        self.assertEqual(len([l for l in self.calls() if l.endswith("|--version")]), 4)


class OwnedGroupTests(DoctorFixture):
    """What the merge gate found: a signal goes to the number of a process group only
    while that number is held by a child of this process that has not been reaped. Once
    the child is reaped the number is free, and another process could have it and lead
    another group."""

    def script(self, body):
        path = self.bin / "made-up"
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return str(path)

    def watched(self):
        """`os.killpg` with a record, for each signal, of whether the number was still
        that of a child of this process that is alive and has not been reaped: a look
        without waiting answers (0, 0) for such a child and for no other number. A
        signal to any other number is recorded and not sent."""
        real, seen = os.killpg, []

        def killpg(pid, sig):
            try:
                held = os.waitpid(pid, os.WNOHANG) == (0, 0)
            except ChildProcessError:
                held = False
            seen.append((int(sig), held))
            if held:
                real(pid, sig)
        return patch.object(doctor.os, "killpg", side_effect=killpg), seen

    def call(self, body, bound=5.0):
        import signal
        patched, seen = self.watched()
        with patched:
            result = doctor.call(self.script(body), ["--help"], bound)
        self.assertEqual([item for item in seen if not item[1]], [], seen)
        self.assertIn((int(signal.SIGKILL), True), seen)  # what is left of the group is still stopped
        return result

    def test_a_call_that_ends_by_itself_signals_only_a_group_it_still_holds(self):
        result = self.call('echo "usage: made-up [--model M]"; echo "note" >&2; exit 3')
        self.assertEqual((result["out"], result["err"], result["code"], result["stopped"], result["error"]),
                         ("usage: made-up [--model M]\n", "note\n", 3, False, ""))

    def test_a_call_stopped_at_the_bound_signals_only_a_group_it_still_holds(self):
        mark = self.root / "child.pid"
        began = time.monotonic()
        result = self.call("sleep 60 & echo $! > " + shlex.quote(str(mark)) + "; echo before; wait", bound=0.5)
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual((result["stopped"], result["code"], result["out"]), (True, None, "before\n"))
        self.assertTrue(gone(int(mark.read_text())))

    def test_what_a_program_left_running_in_its_group_is_stopped_while_the_group_is_held(self):
        mark = self.root / "child.pid"
        result = self.call("sleep 60 > /dev/null 2>&1 & echo $! > " + shlex.quote(str(mark)) + "; echo usage")
        self.assertEqual((result["out"], result["code"], result["stopped"]), ("usage\n", 0, False))
        self.assertTrue(gone(int(mark.read_text())))

    def test_a_helper_that_left_the_group_and_writes_on_does_not_open_a_time_with_the_group_let_go(self):
        import signal
        import sys
        mark = self.root / "helper.pid"
        helper = (shlex.quote(sys.executable) + " -c 'import os,time; os.setsid()\nwhile True:\n"
                  " os.write(1, b\"x\"); time.sleep(0.02)'")
        began = time.monotonic()
        try:
            result = self.call("echo usage; " + helper + " & echo $! > " + shlex.quote(str(mark)), bound=1.5)
            spent = time.monotonic() - began
        finally:
            try:
                os.kill(int(mark.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.assertLess(spent, 8)
        self.assertEqual((result["code"], result["stopped"]), (0, False))
        self.assertTrue(result["out"].startswith("usage\n"))

    def test_a_program_that_cannot_be_started_is_said_and_nothing_is_signalled_that_is_not_held(self):
        patched, seen = self.watched()
        with patched:
            result = doctor.call(str(self.bin / "not-there"), ["--help"], 5.0)
        self.assertEqual([item for item in seen if not item[1]], [], seen)
        self.assertTrue(result["error"])
        self.assertEqual((result["code"], result["stopped"], result["out"]), (None, False, ""))

    def test_the_program_is_given_a_null_standard_input_and_not_the_keepers(self):
        began = time.monotonic()
        result = self.call("cat; echo usage")
        self.assertLess(time.monotonic() - began, 4)
        self.assertEqual((result["out"], result["code"], result["stopped"]), ("usage\n", 0, False))

    def test_a_keeper_that_was_ended_by_another_is_said_and_never_as_an_exit_status(self):
        self.three()
        self.keys()
        self.program("claude", {"": CLAUDE_HELP}, body="kill -9 $PPID; sleep 0.3")
        began = time.monotonic()
        lines, code = self.doctor()
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(code, 1)
        block = self.block("claude")
        self.assertNotIn("None", self.text)
        self.assertIn("Version it prints: claude --version ended, and how was not seen", block)
        self.assertIn("help not known (claude --help ended, and how was not seen)", block)
        self.assertNotIn("listed", block)

    def test_a_subcommand_beyond_what_is_kept_of_a_help_is_not_known_and_its_help_is_not_asked_for(self):
        self.three()
        self.keys()
        self.program("codex", {"exec": CODEX_EXEC_HELP}, body='if [ "$*" = "--help" ]; then echo "Usage: codex --model M '
                     '--cd DIR"; head -c 3000000 /dev/zero | tr "\\0" "x"; exit 0; fi')
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        block = self.block("codex")
        self.assertNotIn("not found in the help", block)
        self.assertIn("builder form (headless): subcommand exec not known (the help is longer than the 256 KiB that "
                      "were read, and it is not in that part); the flags after it were not looked for", block)
        self.assertIn("driver form (interactive): --model listed, --cd listed", block)
        self.assertFalse([l for l in self.calls() if l.endswith("|exec --help")], self.calls())
        todo = [l for l in self.text.split("To do before a shift:")[1].splitlines() if "codex" in l]
        self.assertEqual(len(todo), 1, todo)

    def test_a_flag_beyond_what_is_kept_of_a_help_is_not_known_and_never_not_found(self):
        self.three()
        self.keys()
        self.program("claude", {"": CLAUDE_HELP},
                     body='if [ "$*" = "--help" ]; then head -c 3000000 /dev/zero | tr "\\0" "x"; fi')
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        block = self.block("claude")
        self.assertNotIn("not found in the help", block)
        self.assertNotIn("listed", block)
        self.assertIn("driver form (interactive): --model not known (the help is longer than the 256 KiB that were "
                      "read, and it is not in that part)", block)
        todo = [l for l in self.text.split("To do before a shift:")[1].splitlines() if "claude" in l]
        self.assertEqual(len(todo), 1, todo)
        self.assertIn("claude: its help is longer than the 256 KiB that were read", todo[0])

    def test_a_flag_within_what_is_kept_of_a_help_that_was_cut_is_listed(self):
        self.three()
        self.keys()
        kept = self.root / "claude-own-help.txt"
        kept.write_text(CLAUDE_HELP.replace("  --permission-mode <mode>    Permission mode to use\n", ""))
        self.program("claude", body='if [ "$*" = "--help" ]; then cat ' + shlex.quote(str(kept))
                     + '; head -c 3000000 /dev/zero | tr "\\0" "x"; exit 0; fi')
        lines, code = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("reviewer form (review): --print listed, --permission-mode not known (the help is longer than "
                      "the 256 KiB that were read, and it is not in that part), --model listed", self.block("claude"))
        self.assertIn("builder form (headless): --print listed, --model listed", self.block("claude"))


class AfterReviewCommandTests(CommandTests):
    def test_a_repository_path_that_cannot_be_looked_at_is_wrong_usage(self):
        self.three()
        code, out, err = self.main("--repo", "bad\x00path")
        self.assertEqual(code, 2)
        self.assertIn("--repo", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.calls(), [])
