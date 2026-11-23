"""Regressions for an owned child's transport descendants; synthetic transports, never real panes."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import sortie_kit
from sortie_kit.bounds import Budget, Sources
from sortie_kit.launcher import start
from sortie_kit.layout import Refused, Request
from sortie_kit.tick_supervise import Supervisor, spawn
from support import LocalTest


class ReviewRegressions(LocalTest):
    def test_shutdown_stops_transport_descendant_before_return(self):
        self.transport_case("withdraw")

    def test_completed_child_does_not_leave_transport_descendant(self):
        self.transport_case("complete")

    def test_failed_child_transport_stops_before_restart(self):
        self.transport_case("restart")

    def transport_case(self, mode):
        binary = self.root / "bin"
        binary.mkdir()
        home = self.root / "home"
        home.mkdir()
        ready, release, effect = [self.root / name for name in ("ready", "release", "effect")]
        fixture = binary / "herdr"
        fixture.write_text(
            "#!" + sys.executable + "\n"
            "import os, signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"pending = Path({str(ready) + '.pending'!r})\n"
            "pending.write_text(str(os.getpid()))\n"
            f"pending.replace({str(ready)!r})\n"
            "end = time.monotonic() + 3\n"
            "while time.monotonic() < end:\n"
            f"    if Path({str(release)!r}).exists():\n"
            f"        Path({str(effect)!r}).write_text('late action')\n"
            "        break\n"
            "    time.sleep(0.01)\n"
        )
        fixture.chmod(0o755)
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ.get("PATH", ""),
                   PYTHONPATH=str(Path(sortie_kit.__file__).resolve().parent.parent),
                   PYTHONDONTWRITEBYTECODE="1", HOME=str(home), TMPDIR=str(self.root),
                   TMP=str(self.root), TEMP=str(self.root))
        # A child that starts a program of its own, as the removed pane transport did.
        worker = ("import subprocess\n"
                  "try: subprocess.run(['herdr', 'synthetic-transport'], capture_output=True, timeout=3)\n"
                  "except (OSError, subprocess.TimeoutExpired): pass")
        if mode != "withdraw":
            worker = (
                "import os, threading, time\nfrom pathlib import Path\n"
                f"threading.Thread(target=lambda: exec({worker!r}), daemon=True).start()\n"
                "end = time.monotonic() + 2\n"
                f"while not Path({str(ready)!r}).exists() and time.monotonic() < end:\n"
                "    time.sleep(0.01)\n"
                f"os._exit({0 if mode == 'complete' else 1})\n"
            )
        calls = 0
        def tracked_spawn(argv, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                release.touch()
                time.sleep(0.1)
                self.assertFalse(effect.exists(), "old transport survived into restart")
                argv = [sys.executable, "-c", "pass", "--for", argv[-1]]
            return spawn(argv, **kwargs)
        supervisor = Supervisor(Budget(3), Sources(), spawn=tracked_spawn, floor=0.05,
                                authorize=lambda: "withdrawn" if mode == "withdraw" and ready.exists() else "")
        # A separate, finite test process represents something we must not signal.
        sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, process_group=0)
        try:
            self.assertEqual(supervisor.run([sys.executable, "-B", "-c", worker], env=env),
                             (3, "withdrawn") if mode == "withdraw" else (0, "child completed"))
            self.assertTrue(ready.exists(), "synthetic transport never started")
            self.assertIsNotNone(supervisor.child.poll())
            self.assertIsNone(sibling.poll(), "cleanup signaled an unrelated process")
            self.assertEqual(calls, 2 if mode == "restart" else 1)
            self.assertFalse(effect.exists())
            release.touch()  # The effect becomes possible ONLY after shutdown returns.
            end = time.monotonic() + 0.3
            while time.monotonic() < end and not effect.exists():
                time.sleep(0.01)
            self.assertFalse(effect.exists(), "owned transport acted after supervisor shutdown")
        finally:
            sibling.kill()
            sibling.wait(timeout=1)
            # Only the PID written by this test's finite synthetic fixture.
            if ready.exists():
                try:
                    os.kill(int(ready.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_preparation_refuses_a_reopened_sortie_tree_and_never_edits_gitignore(self):
        """Rules in .gitignore outrank the repository's exclude file. When they re-include
        .sortie, the artifacts would be exposed to ordinary staging, so nothing is prepared."""
        repo = self.repo()
        ignore = repo / ".gitignore"
        original = b"unrelated-rule\n/.sortie/\n!/.sortie/\n/.sortie/*\n!/.sortie/shifts/\n"
        ignore.write_bytes(original)
        request = Request(repo, repo / "direction.md", 30)
        for _ in range(2):
            with self.assertRaisesRegex(Refused, "re-included"):
                start(request, prepare_only=True)
        self.assertEqual(ignore.read_bytes(), original)
        self.assertFalse((repo / ".sortie").exists())
        self.assertEqual((repo / ".git/info/exclude").read_text().splitlines().count("/.sortie/"), 1)
        ignore.write_bytes(b"unrelated-rule\n")
        shift = start(request, prepare_only=True)
        exposed = subprocess.run(["git", "-C", str(repo), "ls-files", "--others",
                                  "--exclude-standard", "--", ".sortie"],
                                 check=True, capture_output=True, timeout=5).stdout
        self.assertEqual(exposed, b"", "generated artifacts are exposed to ordinary Git staging")
        self.assertTrue((shift / "direction.md").exists())
