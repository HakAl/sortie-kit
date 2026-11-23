"""`sortie trust init`: the trust file written from a public key, never over an existing one.

No key is generated and nothing is signed: the public key is synthetic text.
"""
import base64
import contextlib
import io
import json
import os
import stat

from sortie_kit import cli, grants, trust_init
from sortie_kit.layout import Refused
from support import LocalTest

BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))).decode()


class TrustInitTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.config = self.root / "config"
        self.config.mkdir(mode=0o700)
        self.key = self.config / "signing-key"
        self.public = self.config / "signing-key.pub"
        self.public.write_text("ssh-ed25519 " + BLOB + " a comment with spaces\n")
        self.out = self.config / "trust.json"

    def test_writes_an_owner_only_trust_file_the_toolkit_accepts(self):
        path = trust_init.init(self.key, self.out, "developer")
        self.assertEqual(path, self.out)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        value = json.loads(self.out.read_text())
        self.assertEqual(set(value), {"version", "identity", "public_key", "ssh_keygen"})
        self.assertEqual((value["version"], value["identity"], value["public_key"]),
                         (1, "developer", "ssh-ed25519 " + BLOB))
        self.assertTrue(os.path.isabs(value["ssh_keygen"]))
        trust = grants.Trust(self.out, self.repo())
        self.assertEqual(trust.identity, "developer")

    def test_an_existing_file_or_link_is_never_replaced(self):
        self.out.write_text("mine\n")
        with self.assertRaisesRegex(Refused, "already exists"):
            trust_init.init(self.key, self.out, "developer")
        self.assertEqual(self.out.read_text(), "mine\n")
        self.out.unlink()
        target = self.root / "elsewhere.json"
        self.out.symlink_to(target)
        with self.assertRaisesRegex(Refused, "already exists"):
            trust_init.init(self.key, self.out, "developer")
        self.assertFalse(target.exists())

    def test_unusable_inputs_refuse_and_write_nothing(self):
        cases = [("ssh-ed25519 " + BLOB + "\n", "two words", "one literal identity"),
                 ("sk-ssh-ed25519@openssh.com " + BLOB + "\n", "developer", "unsupported public key"),
                 ("ssh-ed25519 not*base64\n", "developer", "invalid public key"),
                 ("ssh-ed25519\n", "developer", "unsupported public key")]
        for text, identity, message in cases:
            self.public.write_text(text)
            with self.assertRaisesRegex(Refused, message):
                trust_init.init(self.key, self.out, identity)
            self.assertFalse(os.path.lexists(self.out))
        self.public.unlink()
        with self.assertRaisesRegex(Refused, "no public key at .*signing-key.pub.*ssh-keygen"):
            trust_init.init(self.key, self.out, "developer")
        self.public.write_text("ssh-ed25519 " + BLOB + "\n")
        with self.assertRaisesRegex(Refused, "ssh-keygen was not found"):
            trust_init.init(self.key, self.out, "developer", which=lambda name: None)
        self.assertFalse(os.path.lexists(self.out))

    def test_a_signing_tool_the_toolkit_would_later_reject_refuses_now(self):
        big = self.root / "big-tool"
        with big.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)  # sparse; over the bound grants.Trust applies
        big.chmod(0o755)
        plain = self.root / "not-executable"
        plain.write_text("#!/bin/sh\n")
        for tool, message in ((big, "bounded regular file"), (plain, "not executable"), (self.root, "regular file")):
            with self.subTest(tool=tool), self.assertRaisesRegex(Refused, message):
                trust_init.init(self.key, self.out, "developer", which=lambda name: str(tool))
            self.assertFalse(os.path.lexists(self.out))

    def test_a_failed_write_leaves_no_trust_file_and_no_leftover(self):
        from unittest.mock import patch
        before = sorted(p.name for p in self.config.iterdir())
        with patch("os.fsync", side_effect=OSError("disk full")), self.assertRaisesRegex(OSError, "disk full"):
            trust_init.init(self.key, self.out, "developer")
        self.assertEqual(sorted(p.name for p in self.config.iterdir()), before)
        trust_init.init(self.key, self.out, "developer")  # and the next attempt is not blocked by a partial file

    def test_a_linked_output_or_key_directory_is_refused(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(real)
        with self.assertRaisesRegex(Refused, "symlink"):
            trust_init.init(self.key, link / "trust.json", "developer")
        self.assertEqual(list(real.iterdir()), [])
        (real / "signing-key.pub").write_text(self.public.read_text())
        with self.assertRaisesRegex(Refused, "symlink"):
            trust_init.init(link / "signing-key", self.out, "developer")
        self.assertFalse(os.path.lexists(self.out))

    def test_printed_paths_are_quoted_and_carry_no_control_characters(self):
        odd = self.root / "odd dir\x1b[2J"
        odd.mkdir(mode=0o700)
        (odd / "signing-key.pub").write_text(self.public.read_text())
        seen, errors = io.StringIO(), io.StringIO()
        argv = ["trust", "init", "--key", str(odd / "signing-key"), "--out", str(odd / "trust.json")]
        with contextlib.redirect_stdout(seen), contextlib.redirect_stderr(errors):
            self.assertEqual(cli.main(argv), 0)
            self.assertEqual(cli.main(argv), 3)
        self.assertNotIn("\x1b", seen.getvalue() + errors.getvalue())
        self.assertNotIn("--trust", seen.getvalue())

    def test_offered_flags_give_back_the_paths_that_were_used(self):
        import shlex
        spaced = self.root / "my keys"
        spaced.mkdir(mode=0o700)
        (spaced / "signing-key.pub").write_text(self.public.read_text())
        argv = ["trust", "init", "--key", str(spaced / "signing-key"), "--out", str(spaced / "trust.json")]
        with contextlib.redirect_stdout(io.StringIO()) as seen:
            self.assertEqual(cli.main(argv), 0)
        offered = seen.getvalue().split("Start a shift with: ", 1)[1].strip()
        self.assertEqual(shlex.split(offered),
                         ["--trust", str(spaced / "trust.json"), "--key", str(spaced / "signing-key")])

    def test_offered_flags_are_both_full_paths_when_the_key_was_named_relatively(self):
        import shlex
        there = self.root / "relative keys"
        there.mkdir(mode=0o700)
        (there / "signing-key.pub").write_text(self.public.read_text())
        back = os.getcwd()
        os.chdir(there)
        try:
            here = os.getcwd()  # as the process names it, links in the temporary path included
            with contextlib.redirect_stdout(io.StringIO()) as seen:
                self.assertEqual(cli.main(["trust", "init", "--key", "signing-key", "--out", "trust.json"]), 0)
        finally:
            os.chdir(back)
        offered = shlex.split(seen.getvalue().split("Start a shift with: ", 1)[1].strip())
        self.assertEqual(offered, ["--trust", os.path.join(here, "trust.json"),
                                   "--key", os.path.join(here, "signing-key")])

    def test_report_help_says_what_its_duration_limits(self):
        with contextlib.redirect_stdout(io.StringIO()) as seen, self.assertRaises(SystemExit):
            cli.main(["report", "--help"])
        shown = " ".join(seen.getvalue().split())
        self.assertIn("--for DURATION", shown)
        self.assertIn("not the length of the shift", shown)

    def test_the_usual_place_needs_no_flags_and_the_output_says_so(self):
        home = self.own_home()
        usual = trust_init.directory(home)
        usual.mkdir(parents=True, mode=0o700)
        (usual / "signing-key.pub").write_text(self.public.read_text())
        with contextlib.redirect_stdout(io.StringIO()) as seen:
            self.assertEqual(cli.main(["trust", "init"]), 0)
        self.assertTrue(trust_init.default_trust(home).is_file())
        self.assertIn("no --trust or --key flag is needed", seen.getvalue())
        self.assertNotIn("Start a shift with", seen.getvalue())

    def test_a_path_that_cannot_be_shown_as_it_is_gets_no_ready_flags(self):
        for name in ("cl\u00e9s", "two\nlines", "odd\x1b[2J"):
            with self.subTest(name=name):
                odd = self.root / name
                odd.mkdir(mode=0o700)
                (odd / "signing-key.pub").write_text(self.public.read_text())
                argv = ["trust", "init", "--key", str(odd / "signing-key"), "--out", str(odd / "trust.json")]
                with contextlib.redirect_stdout(io.StringIO()) as seen:
                    self.assertEqual(cli.main(argv), 0)
                shown = seen.getvalue()
                self.assertTrue((odd / "trust.json").is_file())
                self.assertNotIn("Start a shift with", shown)
                self.assertNotIn("--trust '", shown)
                self.assertIn("shown here with escapes", shown)
                self.assertTrue(shown.isascii())
                # Fixed line breaks only: none comes from the path.
                self.assertEqual(shown.count("\n"), 2)
                self.assertNotIn("\x1b", shown)

    def test_a_refusal_stays_on_one_line(self):
        self.assertEqual(cli.safe("a\nb\rc\u00e9"), "a\\nb\\rc\\xe9")

    def test_the_command_reports_the_file_and_refuses_in_plain_words(self):
        argv = ["trust", "init", "--key", str(self.key), "--out", str(self.out)]
        seen, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(seen), contextlib.redirect_stderr(errors):
            self.assertEqual(cli.main(argv), 0)
            self.assertEqual(cli.main(argv), 3)
        self.assertIn("Trust file written: " + str(self.out), seen.getvalue())
        self.assertIn("identity developer", seen.getvalue())
        self.assertIn("sortie: refused: ", errors.getvalue())
        self.assertIn("already exists", errors.getvalue())
