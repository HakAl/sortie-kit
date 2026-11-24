"""`sortie setup`: the directory, the key pair and the trust file in one command.

No real key is made and no real ssh-keygen is started: a stand-in takes the place
of the process and writes a synthetic OpenSSH envelope around random bytes.
"""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import struct
import termios
import threading
from unittest.mock import patch

from sortie_kit import cli, grants, setup, signing
from sortie_kit.layout import Refused
from support import LocalTest

PUBLIC = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
BLOB = base64.b64encode(PUBLIC).decode()


def envelope(cipher=b"aes256-ctr", kdf=b"bcrypt", options=b"\x00" * 24, public=PUBLIC):
    """The outside of an OpenSSH private key file, with noise where a key would be."""
    def s(value):
        return struct.pack(">I", len(value)) + value
    data = b"openssh-key-v1\x00" + s(cipher) + s(kdf) + s(options) + struct.pack(">I", 1) + s(public) + s(os.urandom(64))
    body = base64.encodebytes(data)
    return b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + body + b"-----END OPENSSH PRIVATE KEY-----\n"


class Keygen:
    """Stands in for subprocess.Popen starting ssh-keygen: records how it was started
    and leaves what the test asks for at the path after -f."""
    def __init__(self, code=0, key=True, public=True, private=None, before=None, waits=False):
        self.code, self.key, self.public, self.private, self.before = code, key, public, private, before
        self.calls, self.running, self.told = [], waits, []

    def __call__(self, argv, **options):
        self.calls.append((list(argv), options))
        if self.before:
            self.before()
        path = Path(argv[argv.index("-f") + 1])
        if self.key:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, self.private or envelope())
            os.close(fd)
        if self.public:
            Path(str(path) + ".pub").write_text("ssh-ed25519 " + BLOB + " sortie-kit\n")
        return self

    def poll(self):
        return None if self.running else self.code

    def wait(self, timeout=None):
        return self.poll()

    @property
    def returncode(self):
        return self.poll()

    def terminate(self):
        self.told.append("terminate")
        self.running, self.code = False, -15

    def kill(self):
        self.told.append("kill")
        self.running, self.code = False, -9


class SetupTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.home / ".config" / "sortie-kit"
        self.key, self.public, self.trust = (self.config / n for n in ("signing-key", "signing-key.pub", "trust.json"))
        self.tool = self.root / "bin" / "ssh-keygen"
        self.tool.parent.mkdir()
        self.tool.write_text("#!/bin/sh\nexit 9\n")
        self.tool.chmod(0o755)
        self.asked = []
        self.fds = []

    def which(self, name):
        self.asked.append(name)
        return str(self.tool)

    def terminal(self):
        """A terminal of the test's own, so that echo can be turned off and looked at."""
        master, fd = os.openpty()
        self.addCleanup(os.close, master)
        self.fds.append(fd)
        # Read what is written to it, as a terminal would: unread output holds up the close.
        def drain():
            try:
                while os.read(master, 4096):
                    pass
            except OSError:
                pass
        threading.Thread(target=drain, daemon=True).start()
        return fd

    def staged(self):
        return sorted(p for p in self.config.glob(".setup-*"))

    def run_setup(self, keygen=None, **options):
        keygen = keygen if keygen is not None else Keygen()
        options.setdefault("terminal", self.terminal)
        lines = setup.run(self.home, which=self.which, spawn=keygen, **options)
        return keygen, "\n".join(lines)

    def make(self, key=True, public=True, trust=False, private=None, mode=0o700):
        self.config.mkdir(parents=True, mode=mode, exist_ok=True)
        self.config.chmod(mode)
        if key:
            self.key.write_bytes(private or envelope())
            self.key.chmod(0o600)
        if public:
            self.public.write_text("ssh-ed25519 " + BLOB + " mine\n")
        if trust:
            self.trust.write_bytes(grants.encoded({"version": 1, "identity": "me", "public_key": "ssh-ed25519 " + BLOB,
                                                   "ssh_keygen": str(self.tool.resolve())}))
            self.trust.chmod(0o600)

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode))
                for p in sorted(self.config.iterdir())} if self.config.exists() else None

    def test_without_a_terminal_it_refuses_before_anything_is_made_or_started(self):
        def none():
            raise Refused("sign only in the developer's own terminal; redirected streams refused")
        keygen = Keygen()
        with self.assertRaisesRegex(Refused, "sortie setup is run by a person in their own terminal.*redirected streams"):
            self.run_setup(keygen, terminal=none)
        self.assertEqual((keygen.calls, self.asked), ([], []))
        self.assertFalse((self.home / ".config").exists())

    def test_a_terminal_that_cannot_be_opened_refuses_in_the_same_words(self):
        def gone():
            raise OSError(6, "Device not configured")
        with self.assertRaisesRegex(Refused, "sortie setup is run by a person in their own terminal.*Device not"):
            self.run_setup(terminal=gone)
        self.assertFalse((self.home / ".config").exists())

    def test_the_terminal_check_is_the_one_signing_uses(self):
        import inspect
        self.assertIs(inspect.signature(setup.run).parameters["terminal"].default, signing.terminal)
        with patch("os.isatty", return_value=False), self.assertRaisesRegex(Refused, "their own terminal"):
            setup.run(self.home, which=self.which, spawn=Keygen())
        self.assertFalse((self.home / ".config").exists())

    def test_nothing_there_makes_the_directory_the_key_and_the_trust_file(self):
        keygen, said = self.run_setup()
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o700)
        (argv, options), = keygen.calls
        tool = str(self.tool.resolve())
        # The key is made in a directory of this run's own and linked into place.
        made = Path(argv[-1])
        self.assertEqual(argv[:-1], [tool, "-t", "ed25519", "-C", "sortie-kit", "-f"])
        self.assertEqual((made.name, made.parent.parent, made.parent.name[:7]), ("signing-key", self.config, ".setup-"))
        self.assertEqual(sorted(p.name for p in self.config.iterdir()), ["signing-key", "signing-key.pub", "trust.json"])
        self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)
        fd, = self.fds
        self.assertEqual((options["stdin"], options["stdout"], options["stderr"]), (fd, fd, fd))
        self.assertEqual(set(options), {"stdin", "stdout", "stderr", "env"})
        with self.assertRaises(OSError):
            os.fstat(fd)  # closed when the command is done
        value = json.loads(self.trust.read_text())
        self.assertEqual(value, {"version": 1, "identity": "developer", "public_key": "ssh-ed25519 " + BLOB, "ssh_keygen": tool})
        self.assertEqual(stat.S_IMODE(self.trust.stat().st_mode), 0o600)
        self.assertEqual(self.asked, ["ssh-keygen"])  # resolved once: what made the key is what the file names
        self.assertEqual(grants.Trust(self.trust, self.root / "elsewhere").identity, "developer")
        for part in ("Made the directory", "Made the key pair", "Trust file written", "sortie start finds both"):
            self.assertIn(part, said)

    def test_the_arguments_given_to_ssh_keygen_hold_no_passphrase_option(self):
        keygen, _ = self.run_setup()
        (argv, options), = keygen.calls
        for option in ("-N", "-P", "-q"):
            self.assertNotIn(option, argv)
        self.assertFalse(any(a.startswith(("-N", "-P")) for a in argv))
        self.assertNotIn("input", options)
        self.assertNotIn(-1, (options["stdin"], options["stdout"], options["stderr"]))  # subprocess.PIPE

    def test_ssh_keygen_gets_an_environment_of_its_own_and_nothing_inherited(self):
        poison = dict(SSH_ASKPASS="/nowhere/helper", SSH_ASKPASS_REQUIRE="force", SSH_AUTH_SOCK="/nowhere/agent",
                      DISPLAY=":0", DYLD_INSERT_LIBRARIES="/nowhere/x.dylib", LD_PRELOAD="/nowhere/x.so",
                      SORTIE_SENTINEL="a secret")
        with patch.dict(os.environ, poison):
            keygen, _ = self.run_setup()
        (argv, options), = keygen.calls
        stage = str(Path(argv[-1]).parent)
        self.assertEqual(options["env"], {"PATH": os.defpath, "HOME": stage, "LC_ALL": "C", "SSH_ASKPASS_REQUIRE": "never",
                                          "TMPDIR": stage, "TMP": stage, "TEMP": stage})

    def test_echo_is_off_while_ssh_keygen_runs_and_put_back_after(self):
        seen = []

        def terminal():
            fd = self.terminal()
            state = termios.tcgetattr(fd)
            state[3] |= both  # a terminal with both kinds of echo on
            termios.tcsetattr(fd, termios.TCSANOW, state)
            seen.append(termios.tcgetattr(fd)[3] & both == both)
            return fd
        both = termios.ECHO | termios.ECHONL
        keygen = Keygen(before=lambda: seen.append(bool(termios.tcgetattr(self.fds[0])[3] & both)))
        closed = []
        real = os.close

        def close(fd):
            if self.fds and fd == self.fds[0] and not closed:
                closed.append(termios.tcgetattr(fd)[3] & both == both)
            real(fd)
        with patch("os.close", close):
            self.run_setup(keygen, terminal=terminal)
        self.assertEqual((seen, closed), ([True, False], [True]))
        # A terminal whose echo cannot be turned off gets no prompt and no key.
        for path in self.config.iterdir():
            path.unlink()
        keygen = Keygen()
        with self.assertRaisesRegex(Refused, "echo"):
            self.run_setup(keygen, terminal=lambda: os.open(os.devnull, os.O_RDWR))
        self.assertEqual(keygen.calls, [])
        self.assertEqual(list(self.config.iterdir()), [])  # the run's own empty directory is gone too

    def test_a_key_pair_already_there_is_kept_and_the_trust_file_is_written(self):
        self.make()
        before = self.snapshot()
        keygen, said = self.run_setup()
        self.assertEqual(keygen.calls, [])
        after = self.snapshot()
        self.assertEqual({k: after[k] for k in before}, before)
        self.assertEqual(json.loads(self.trust.read_text())["public_key"], "ssh-ed25519 " + BLOB)
        self.assertIn("already there and was kept", said)
        self.assertIn("Trust file written", said)

    def test_with_all_three_there_nothing_changes(self):
        self.make(trust=True)
        before = self.snapshot()
        keygen, said = self.run_setup()
        self.assertEqual((keygen.calls, self.snapshot()), ([], before))
        self.assertIn("Nothing was changed", said)

    def test_a_trust_file_already_there_that_a_start_would_refuse_is_not_called_ready(self):
        good = dict(version=1, identity="me", public_key="ssh-ed25519 " + BLOB, ssh_keygen=str(self.tool.resolve()))
        other = "ssh-ed25519 " + base64.b64encode(PUBLIC[:-1] + b"\xff").decode()
        cases = [(b"not json\n", 0o600, "not one sortie start accepts"),
                 (grants.encoded(dict(good, public_key=other)), 0o600, "names another key"),
                 (grants.encoded(dict(good, ssh_keygen=str(self.root / "gone"))), 0o600, "not one sortie start accepts"),
                 (grants.encoded(good), 0o644, "not yours alone.*chmod 600")]
        self.make()
        for raw, mode, message in cases:
            with self.subTest(message=message, mode=mode):
                self.trust.write_bytes(raw)
                self.trust.chmod(mode)
                before = self.snapshot()
                keygen = Keygen()
                with self.assertRaisesRegex(Refused, message):
                    self.run_setup(keygen)
                self.assertEqual((keygen.calls, self.snapshot()), ([], before))
        self.trust.chmod(0o600)
        self.assertIn("Nothing was changed", self.run_setup()[1])
        with patch("sortie_kit.setup.os.getuid", return_value=os.getuid() + 1), \
                self.assertRaisesRegex(Refused, "another account") as caught:
            self.run_setup()
        self.assertNotIn("chmod", str(caught.exception))

    def test_a_link_where_a_file_is_read_is_refused_and_not_followed(self):
        for name in ("signing-key", "signing-key.pub", "trust.json"):
            with self.subTest(name=name):
                for path in (self.key, self.public, self.trust):
                    path.unlink(missing_ok=True)
                self.make(trust=True)
                real = self.root / ("real-" + name)
                (self.config / name).rename(real)
                (self.config / name).symlink_to(real)
                before = real.read_bytes()
                keygen = Keygen()
                with self.assertRaisesRegex(Refused, "not a link"):
                    self.run_setup(keygen)
                self.assertEqual((keygen.calls, real.read_bytes()), ([], before))
                self.assertTrue((self.config / name).is_symlink())

    def test_a_trust_file_that_appears_while_the_key_is_made_is_left_and_the_new_key_is_named(self):
        def theirs():
            self.trust.write_text("theirs\n")
        with self.assertRaisesRegex(Refused, "The trust file was not written .*trust.json already exists.*The new key "
                                             "pair is at .*signing-key and was left there"):
            self.run_setup(Keygen(before=theirs))
        self.assertEqual(self.trust.read_text(), "theirs\n")

    def test_what_ssh_keygen_made_that_is_no_usable_key_is_left_in_the_runs_own_directory(self):
        with self.assertRaisesRegex(Refused, "what ssh-keygen made is not a key sortie start would sign with .*does not "
                                             "match.*Nothing uses the directory .*setup-") as caught:
            self.run_setup(Keygen(private=envelope(public=PUBLIC[:-1] + b"\xff")))
        self.assertNotIn("chmod", str(caught.exception))
        stage, = self.staged()
        self.assertEqual(list(self.config.iterdir()), [stage])

    def test_a_signal_before_ssh_keygen_is_started_starts_none(self):
        real, keygen = os.write, Keygen()
        def write(fd, data):
            os.kill(os.getpid(), signal.SIGINT)
            return real(fd, data)
        with patch("sortie_kit.setup.os.write", write), self.assertRaisesRegex(Refused, "interrupted.*It left no file"):
            self.run_setup(keygen)
        self.assertEqual((keygen.calls, list(self.config.iterdir())), ([], []))

    def test_half_of_what_it_makes_refuses_and_changes_nothing(self):
        for have, message in ((dict(public=False), "no public key"), (dict(key=False), "no key at"),
                              (dict(key=False, public=False, trust=True), "trust file .* but no key")):
            with self.subTest(have=have):
                for path in (self.key, self.public, self.trust):
                    path.unlink(missing_ok=True)
                self.make(**have)
                before = self.snapshot()
                keygen = Keygen()
                with self.assertRaisesRegex(Refused, message):
                    self.run_setup(keygen)
                self.assertEqual((keygen.calls, self.snapshot()), ([], before))

    def test_a_link_at_a_name_counts_as_there_and_is_not_followed(self):
        self.make(key=False, public=False)
        target = self.root / "elsewhere"
        self.key.symlink_to(target)
        keygen = Keygen()
        with self.assertRaises(Refused):
            self.run_setup(keygen)
        self.assertEqual(keygen.calls, [])
        self.assertFalse(target.exists())

    def test_a_failed_ssh_keygen_writes_no_trust_file_and_names_what_it_left(self):
        keygen = Keygen(code=1, public=False)
        with self.assertRaises(Refused) as caught:
            self.run_setup(keygen)
        text = str(caught.exception)
        self.assertIn("ssh-keygen did not finish (exit status 1)", text)
        stage, = self.staged()
        self.assertIn(str(stage / "signing-key") + " is there now and was left", text)
        self.assertNotIn("signing-key.pub is there", text)
        self.assertEqual([p.name for p in stage.iterdir()], ["signing-key"])  # named, not removed
        self.assertEqual(list(self.config.iterdir()), [stage])  # no key under its name, no trust file
        # Nothing at all left: said too, and the empty directory of that run is gone.
        with self.assertRaisesRegex(Refused, "It left no file"):
            self.run_setup(Keygen(code=1, key=False, public=False))
        self.assertEqual(list(self.config.iterdir()), [stage])
        # What an earlier run left does not stand in the way of the next.
        self.run_setup()
        self.assertTrue(self.key.exists() and self.trust.exists() and stage.exists())

    def test_an_interrupted_ssh_keygen_writes_no_trust_file(self):
        before = signal.getsignal(signal.SIGINT)
        keygen = Keygen(code=0, before=lambda: os.kill(os.getpid(), signal.SIGINT))
        with self.assertRaisesRegex(Refused, "interrupted") as caught:
            self.run_setup(keygen)
        stage, = self.staged()
        self.assertIn(str(stage / "signing-key") + " is there now and was left", str(caught.exception))
        self.assertEqual(sorted(p.name for p in stage.iterdir()), ["signing-key", "signing-key.pub"])
        self.assertEqual(list(self.config.iterdir()), [stage])
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_a_signal_ends_an_ssh_keygen_that_does_not_end_by_itself(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig), patch("sortie_kit.setup.GRACE", 0.1):
                keygen = Keygen(key=False, public=False, waits=True, before=lambda: os.kill(os.getpid(), sig))
                with self.assertRaisesRegex(Refused, "interrupted.*It left no file"):
                    self.run_setup(keygen)
                self.assertEqual(keygen.told, ["terminate"])
                self.assertEqual(list(self.config.iterdir()), [])

    def test_a_file_that_appears_while_the_key_is_made_is_not_replaced(self):
        def theirs():
            self.public.write_text("theirs\n")
        with self.assertRaisesRegex(Refused, "appeared at .*signing-key.pub while the key was being made"):
            self.run_setup(Keygen(before=theirs))
        self.assertEqual(self.public.read_text(), "theirs\n")
        stage, = self.staged()
        # Seen before anything was linked: nothing of this run is under either name.
        self.assertEqual(sorted(p.name for p in self.config.iterdir()), sorted([stage.name, "signing-key.pub"]))
        self.assertEqual(sorted(p.name for p in stage.iterdir()), ["signing-key", "signing-key.pub"])

    def test_nothing_is_removed_by_name_when_the_second_name_is_taken(self):
        """Between the two links another process moves this run's key away and puts its own
        files at both names. Neither is removed, and the message does not say otherwise."""
        real = os.link
        done = []

        def link(src, dst, **options):
            real(src, dst, **options)
            if not done and dst == "signing-key":
                done.append(dst)
                self.key.rename(self.config / "moved-by-them")
                self.key.write_text("their key\n")
                self.public.write_text("their public key\n")
        with patch("sortie_kit.setup.os.link", link), self.assertRaises(Refused) as caught:
            self.run_setup()
        text = str(caught.exception)
        self.assertEqual((self.key.read_text(), self.public.read_text()), ("their key\n", "their public key\n"))
        self.assertTrue((self.config / "moved-by-them").exists())
        stage, = self.staged()
        self.assertEqual(sorted(p.name for p in stage.iterdir()), ["signing-key", "signing-key.pub"])
        self.assertFalse(self.trust.exists())
        self.assertIn("Nothing was removed", text)
        self.assertIn(str(stage), text)

    def test_the_first_link_of_this_run_is_left_and_said_when_the_second_name_is_taken(self):
        real = os.link

        def link(src, dst, **options):
            real(src, dst, **options)
            if dst == "signing-key":
                self.public.write_text("theirs\n")
        with patch("sortie_kit.setup.os.link", link), self.assertRaises(Refused) as caught:
            self.run_setup()
        stage, = self.staged()
        self.assertEqual(self.public.read_text(), "theirs\n")
        self.assertEqual(self.key.stat().st_ino, (stage / "signing-key").stat().st_ino)  # this run's, not removed
        self.assertIn(str(self.key) + " was given its name by this run and was left there", str(caught.exception))
        self.assertFalse(self.trust.exists())

    def swap(self):
        """Another process puts a directory of its own where the checked one was."""
        self.config.rename(self.home / ".config" / "moved")
        self.config.mkdir(mode=0o700)

    def test_a_directory_replaced_while_ssh_keygen_runs_gets_nothing_put_in_it(self):
        with self.assertRaisesRegex(Refused, "was replaced while this command ran"):
            self.run_setup(Keygen(before=self.swap, key=False, public=False))
        self.assertEqual(list(self.config.iterdir()), [])

    def test_the_trust_file_is_written_in_the_directory_that_was_checked_or_not_at_all(self):
        from sortie_kit import trust_init
        real = trust_init.init
        for have in (False, True):  # a new key, and a key pair that was there
            with self.subTest(have=have):
                if (self.home / ".config").exists():
                    import shutil
                    shutil.rmtree(self.home / ".config")
                if have:
                    self.make()

                def init(*arguments, **options):
                    self.swap()
                    return real(*arguments, **options)
                with patch("sortie_kit.trust_init.init", init), \
                        self.assertRaisesRegex(Refused, "was replaced while this command ran"):
                    self.run_setup()
                # Nothing in the directory that took its place: no trust file without the key it names.
                self.assertEqual(list(self.config.iterdir()), [])

    def test_a_directory_made_by_this_run_is_not_chmodded_after_it_is_opened_by_name(self):
        """What is opened may be another directory than the one just made: its mode is judged, never set."""
        with patch("sortie_kit.setup.os.fchmod", side_effect=AssertionError("fchmod on a directory opened by name")):
            real = os.mkdir

            def mkdir(path, mode=0o777, **options):
                real(path, mode, **options)
                if Path(path) == self.config:
                    os.chmod(path, 0o777)  # stands for a replacement that others can write to
            with patch("sortie_kit.setup.os.mkdir", mkdir), self.assertRaisesRegex(Refused, "writable by others"):
                self.run_setup()
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o777)

    def test_a_signal_right_after_echo_is_turned_off_is_caught_and_echo_is_put_back(self):
        """Major 3 of the merge gate: the handlers are in place before the terminal is changed."""
        real, keygen, state = termios.tcsetattr, Keygen(), []

        def tcsetattr(fd, when, attributes):
            real(fd, when, attributes)
            if not state:
                state.append(signal.getsignal(signal.SIGTERM))
                os.kill(os.getpid(), signal.SIGTERM)  # the default disposition would end the test run here
        before = signal.getsignal(signal.SIGTERM)
        self.assertIs(before, signal.SIG_DFL)
        closed = []
        close = os.close

        def closing(fd):
            if self.fds and fd == self.fds[0] and not closed:
                closed.append(bool(termios.tcgetattr(fd)[3] & termios.ECHO))
            close(fd)
        with patch("sortie_kit.setup.termios.tcsetattr", tcsetattr), patch("os.close", closing), \
                self.assertRaisesRegex(Refused, "interrupted.*It left no file"):
            self.run_setup(keygen)
        self.assertIsNot(state[0], signal.SIG_DFL)
        self.assertEqual((keygen.calls, closed, list(self.config.iterdir())), ([], [True], []))
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_an_ssh_keygen_replaced_while_it_ran_is_not_named_in_a_trust_file(self):
        """Major 4 of the merge gate: the file at the path is the one that was checked, or nothing is published."""
        def replaced():
            self.tool.unlink()
            self.tool.write_text("#!/bin/sh\nexit 7\n")
            self.tool.chmod(0o755)
        for change in (replaced, self.tool.unlink):
            with self.subTest(change=change.__name__):
                if (self.home / ".config").exists():
                    import shutil
                    shutil.rmtree(self.home / ".config")
                self.tool.write_text("#!/bin/sh\nexit 9\n")
                self.tool.chmod(0o755)
                with self.assertRaisesRegex(Refused, "ssh-keygen at .* changed while this command ran") as caught:
                    self.run_setup(Keygen(before=change))
                stage, = self.staged()
                self.assertEqual(list(self.config.iterdir()), [stage])  # no key under its name, no trust file
                self.assertIn(str(stage), str(caught.exception))
        # A key pair that was there: the trust file is not written either.
        import shutil
        shutil.rmtree(self.home / ".config")
        self.tool.write_text("#!/bin/sh\nexit 9\n")
        self.tool.chmod(0o755)
        self.make()
        from sortie_kit import trust_init
        real = trust_init.init

        def init(*arguments, **options):
            replaced()
            return real(*arguments, **options)
        with patch("sortie_kit.trust_init.init", init), self.assertRaisesRegex(Refused, "changed while this command ran"):
            self.run_setup()
        self.assertFalse(self.trust.exists())

    def test_what_ssh_keygen_wrote_beneath_a_replaced_path_is_said_and_not_denied(self):
        """ssh-keygen is given a path. If the path led elsewhere, the refusal says where to look."""
        keygen = Keygen()

        def spawn(argv, **options):
            self.swap()
            Path(argv[-1]).parent.mkdir(mode=0o700)  # theirs, of the same name, in what took the place
            return keygen(argv, **options)
        with self.assertRaisesRegex(Refused, "was replaced while this command ran") as caught:
            setup.run(self.home, which=self.which, spawn=spawn, terminal=self.terminal)
        text = str(caught.exception)
        theirs, = self.staged()
        self.assertNotIn("put nothing in it", text)
        self.assertIn("ssh-keygen was given the path " + str(theirs / "signing-key"), text)
        self.assertEqual(sorted(p.name for p in theirs.iterdir()), ["signing-key", "signing-key.pub"])  # not removed
        self.assertEqual(list(self.config.iterdir()), [theirs])  # no name given, no trust file

    def test_a_key_pair_or_trust_file_changed_while_it_is_checked_is_not_called_ready(self):
        from sortie_kit import trust_init

        def again(path):
            raw = path.read_bytes()
            path.unlink()
            path.write_bytes(raw)
            path.chmod(0o600)
        for name in ("signing-key.pub", "trust.json"):  # all three there
            with self.subTest(name=name):
                for path in (self.key, self.public, self.trust):
                    path.unlink(missing_ok=True)
                self.make(trust=True)
                real = setup.ready

                def ready(*arguments):
                    result = real(*arguments)
                    again(self.config / name)
                    return result
                with patch("sortie_kit.setup.ready", ready), \
                        self.assertRaisesRegex(Refused, "removed or replaced while this command ran.*Nothing was changed"):
                    self.run_setup()
        # The key pair there and no trust file: none is written for a pair that is no longer the one checked.
        self.trust.unlink()
        real_init = trust_init.init

        def init(*arguments, **options):
            self.public.unlink()
            return real_init(*arguments, **options)
        with patch("sortie_kit.trust_init.init", init), \
                self.assertRaisesRegex(Refused, "removed or replaced while this command ran.*No trust file was written"):
            self.run_setup()
        self.assertEqual(sorted(p.name for p in self.config.iterdir()), ["signing-key"])

    def test_a_trust_file_that_has_its_name_is_not_reported_as_not_written(self):
        """What goes wrong while the temporary name is taken away does not hide what happened before it."""
        real = os.unlink

        def unlink(path, **options):
            if str(path).startswith(".trust-"):
                real(path, **options)
                raise OSError(5, "Input/output error")
            return real(path, **options)
        with patch("sortie_kit.trust_init.os.unlink", unlink):
            _, said = self.run_setup()
        self.assertIn("Trust file written", said)
        self.assertEqual(sorted(p.name for p in self.config.iterdir()), ["signing-key", "signing-key.pub", "trust.json"])

    def test_a_link_put_above_the_directory_counts_as_replaced(self):
        self.make()
        parent = os.open(self.config, os.O_RDONLY)
        self.addCleanup(os.close, parent)
        self.assertTrue(setup.same(self.config, parent))
        (self.home / ".config").rename(self.root / "moved")
        (self.home / ".config").symlink_to(self.root / "moved")
        self.assertFalse(setup.same(self.config, parent))  # the same directory, by a path a start refuses

    def test_an_ssh_keygen_that_can_no_longer_be_run_counts_as_changed(self):
        with self.assertRaisesRegex(Refused, "ssh-keygen at .* changed while this command ran"):
            self.run_setup(Keygen(before=lambda: self.tool.chmod(0o644)))
        stage, = self.staged()
        self.assertEqual(list(self.config.iterdir()), [stage])

    def test_a_good_setup_is_ready_whatever_else_is_in_the_directory_and_however_the_key_is_spaced(self):
        """Minor of the merge gate: no stand-in on the filesystem, and the key compared as type and bytes."""
        self.make(trust=True)
        (self.config / ".no-repository").symlink_to(self.config)
        self.assertIn("Nothing was changed", self.run_setup()[1])
        self.trust.write_bytes(grants.encoded({"version": 1, "identity": "me", "public_key": "ssh-ed25519   " + BLOB,
                                               "ssh_keygen": str(self.tool.resolve())}))
        self.assertIn("Nothing was changed", self.run_setup()[1])

    def test_a_trust_file_that_could_not_be_written_after_a_new_key_says_where_the_key_is(self):
        for error in (OSError(28, "No space left on device"), Refused("some refusal")):
            with self.subTest(error=error):
                if (self.home / ".config").exists():
                    import shutil
                    shutil.rmtree(self.home / ".config")
                with patch("sortie_kit.trust_init.init", side_effect=error), self.assertRaises(Refused) as caught:
                    self.run_setup()
                text = str(caught.exception)
                self.assertIn("The trust file was not written", text)
                self.assertIn("The new key pair is at " + str(self.key) + " and was left there", text)
                self.assertNotIn("appeared", text)
                self.assertTrue(self.key.exists() and self.public.exists() and not self.trust.exists())

    def test_a_path_that_cannot_be_shown_as_it_is_gets_no_ready_command(self):
        """As `sortie trust init` does: an escaped path is not the path, so no command to copy is offered."""
        self.home = self.root / "h\u00f6me"
        self.home.mkdir()
        self.config = self.home / ".config" / "sortie-kit"
        self.key, self.public, self.trust = (self.config / n for n in ("signing-key", "signing-key.pub", "trust.json"))
        self.make(key=False, public=False, mode=0o777)
        with self.assertRaisesRegex(Refused, "writable by others") as caught:
            self.run_setup()
        self.assertNotIn("chmod 700 ", str(caught.exception))
        self.assertIn("shown here with escapes", str(caught.exception))
        self.config.chmod(0o700)
        self.make(private=envelope(cipher=b"none", kdf=b"none", options=b""))
        with self.assertRaisesRegex(Refused, "has no passphrase") as caught:
            self.run_setup()
        self.assertNotIn("ssh-keygen -p -f '", str(caught.exception))
        self.assertIn("shown here with escapes", str(caught.exception))

    def test_a_key_without_a_passphrase_gets_no_trust_file_new_or_already_there(self):
        bare = envelope(cipher=b"none", kdf=b"none", options=b"")
        # Made by this run: the person is not left with a key that a start refuses.
        with self.assertRaisesRegex(Refused, "just made has no passphrase.*sortie start does not sign with such a key.*"
                                             "It was removed.*Run sortie setup again"):
            self.run_setup(Keygen(private=bare))
        self.assertEqual(list(self.config.iterdir()), [])
        # Already there: theirs, so it is left, and the way to give it a passphrase is said.
        self.make(private=bare)
        before = self.snapshot()
        keygen = Keygen()
        with self.assertRaisesRegex(Refused, "has no passphrase.*left as it is.*ssh-keygen -p -f"):
            self.run_setup(keygen)
        self.assertEqual((keygen.calls, self.snapshot()), ([], before))
        # With a trust file beside it too: still not called ready.
        self.trust.write_text("{}\n")
        self.trust.chmod(0o600)
        with self.assertRaisesRegex(Refused, "has no passphrase"):
            self.run_setup(keygen)

    def test_a_key_that_is_not_the_public_keys_gets_no_trust_file(self):
        self.make(private=envelope(public=PUBLIC[:-1] + b"\xff"))
        with self.assertRaisesRegex(Refused, "does not match"):
            self.run_setup()
        self.assertFalse(self.trust.exists())

    def test_a_key_already_there_that_others_can_read_gets_no_trust_file(self):
        self.make()
        self.key.chmod(0o644)
        with self.assertRaisesRegex(Refused, "not yours alone.*chmod 600"):
            self.run_setup()
        self.assertEqual((stat.S_IMODE(self.key.stat().st_mode), self.trust.exists()), (0o644, False))
        self.key.chmod(0o600)
        real = os.fstat
        def foreign(fd):
            info = real(fd)
            return os.stat_result((*info[:4], info.st_uid + 1, *info[5:])) if stat.S_ISREG(info.st_mode) else info
        with patch("sortie_kit.setup.os.fstat", foreign), self.assertRaisesRegex(Refused, "another account") as caught:
            self.run_setup()
        self.assertNotIn("chmod", str(caught.exception))
        self.assertFalse(self.trust.exists())

    def test_an_existing_directory_is_never_changed_and_a_writable_one_refuses(self):
        self.make(key=False, public=False, mode=0o777)
        keygen = Keygen()
        with self.assertRaisesRegex(Refused, "writable by others.*chmod 700"):
            self.run_setup(keygen)
        self.assertEqual((keygen.calls, stat.S_IMODE(self.config.stat().st_mode)), ([], 0o777))
        self.config.chmod(0o770)
        with self.assertRaisesRegex(Refused, "writable by others"):
            self.run_setup(keygen)
        self.config.chmod(0o700)
        with patch("os.getuid", return_value=os.getuid() + 1), self.assertRaisesRegex(Refused, "another account") as caught:
            self.run_setup(keygen)
        self.assertNotIn("chmod", str(caught.exception))
        self.assertEqual((keygen.calls, list(self.config.iterdir())), ([], []))
        self.config.chmod(0o755)
        keygen, said = self.run_setup()
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o755)
        self.assertIn("can be read by others", said)
        self.assertIn("chmod 700", said)
        self.assertTrue(self.trust.exists())

    def test_a_directory_reached_through_a_link_refuses(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        (self.home / ".config").mkdir()
        self.config.symlink_to(real)
        keygen = Keygen()
        with self.assertRaisesRegex(Refused, "symlink"):
            self.run_setup(keygen)
        self.assertEqual((keygen.calls, list(real.iterdir())), ([], []))
        # A link further up, with the directory not yet there: nothing is made through it.
        self.config.unlink()
        (self.home / ".config").rmdir()
        (self.home / ".config").symlink_to(real)
        with self.assertRaisesRegex(Refused, "symlink"):
            self.run_setup(keygen)
        self.assertEqual((keygen.calls, list(real.iterdir())), ([], []))

    def test_the_command_prints_what_was_done_or_refuses_with_exit_3(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("sortie_kit.setup.run", return_value=["Made the key pair."]) as run:
            self.assertEqual(cli.main(["setup"]), 0)
        run.assert_called_once_with()
        self.assertEqual(out.getvalue(), "Made the key pair.\n")
        with contextlib.redirect_stderr(err), patch("sortie_kit.setup.run", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["setup"]), 130)
        self.assertIn("sortie: interrupted", err.getvalue())
        home = self.own_home()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("os.isatty", return_value=False):
            self.assertEqual(cli.main(["setup"]), 3)
        self.assertIn("sortie: refused: sortie setup is run by a person in their own terminal", err.getvalue())
        self.assertEqual(list(home.iterdir()), [])

    def test_the_real_ssh_keygen_in_a_terminal_of_the_tests_own(self):
        """The command as installed, with the machine's ssh-keygen and a passphrase that is
        random bytes of this test: first none at all, then one."""
        import secrets
        import shutil
        import sys
        from test_grants import terminal_run
        tool = shutil.which("ssh-keygen", path=os.defpath)
        if not tool:
            self.skipTest("no ssh-keygen on the default PATH")
        source = str(Path(setup.__file__).resolve().parents[1])
        env = {"HOME": str(self.home), "PATH": os.defpath, "PYTHONPATH": source, "PYTHONDONTWRITEBYTECODE": "1",
               "LC_ALL": "C"}
        argv = [sys.executable, "-m", "sortie_kit", "setup"]
        code, output, prompts = terminal_run(argv, env, b"", timeout=60)
        text = output.decode(errors="replace")
        self.assertEqual((code, prompts), (3, 2), text)
        self.assertIn("just made has no passphrase", text)
        self.assertEqual(list(self.config.iterdir()), [])
        secret = secrets.token_urlsafe(24).encode()  # never a user credential; terminal_run fails if it is shown
        code, output, prompts = terminal_run(argv, env, secret, timeout=60)
        text = output.decode(errors="replace")
        self.assertEqual((code, prompts), (0, 2), text)
        self.assertIn("no --trust or --key flag is needed", text)
        self.assertEqual(sorted(p.name for p in self.config.iterdir()), ["signing-key", "signing-key.pub", "trust.json"])
        self.assertEqual([stat.S_IMODE(p.stat().st_mode) for p in (self.config, self.key, self.trust)], [0o700, 0o600, 0o600])
        trust = grants.Trust(self.trust, self.root / "elsewhere")
        signing.encrypted_key(self.key.read_bytes(), trust.key_blob)  # what a start checks before it signs
        self.assertEqual(json.loads(self.trust.read_text())["ssh_keygen"], str(Path(tool).resolve()))
        if os.environ.get("SORTIE_TEST_TRANSCRIPT"):
            Path(os.environ["SORTIE_TEST_TRANSCRIPT"]).write_text(text)

    def test_trust_init_takes_the_tool_it_is_given_and_looks_for_none(self):
        from sortie_kit import trust_init
        self.make()
        def never(name):
            raise AssertionError("looked for " + name)
        trust_init.init(self.key, self.trust, "developer", which=never, tool=str(self.tool.resolve()))
        self.assertEqual(json.loads(self.trust.read_text())["ssh_keygen"], str(self.tool.resolve()))
        # And the public key it is given, without reading the file again.
        self.trust.unlink()
        self.public.unlink()
        trust_init.init(self.key, self.trust, "developer", which=never, tool=str(self.tool), public="ssh-ed25519 " + BLOB)
        self.assertEqual(json.loads(self.trust.read_text())["public_key"], "ssh-ed25519 " + BLOB)
