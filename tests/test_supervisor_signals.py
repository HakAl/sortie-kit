"""Real signal delivery in a disposable PTY; never touches developer panes."""
import json
import os
from pathlib import Path
import pty
import signal
import sys
import time

import sortie_kit
from support import LocalTest


class SupervisorSignals(LocalTest):
    def test_terminal_hangup_stops_owned_group(self):
        self.signal_case(None)

    def test_sigterm_stops_owned_group(self):
        self.signal_case(signal.SIGTERM)

    def test_sigint_stops_owned_group(self):
        self.signal_case(signal.SIGINT)

    def signal_case(self, delivered):
        ready, release, effect, result = [self.root / name for name in
                                          ("ready", "release", "effect", "result")]
        transport = (
            "import json, os, signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"pending = Path({str(ready) + '.pending'!r})\n"
            "pending.write_text(json.dumps({'group': os.getpgrp()}))\n"
            f"pending.replace({str(ready)!r})\n"
            "end = time.monotonic() + 4\n"
            "while time.monotonic() < end:\n"
            f"    if Path({str(release)!r}).exists():\n"
            f"        Path({str(effect)!r}).write_text('late action')\n"
            "        break\n"
            "    time.sleep(0.01)\n"
        )
        worker = (
            "import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-B', '-c', {transport!r}]).wait(timeout=5)\n"
        )
        owner = self.root / "owner.py"
        owner.write_text(
            "import json, signal, sys\nfrom pathlib import Path\n"
            "from sortie_kit.bounds import Budget, Sources\n"
            "from sortie_kit.tick_supervise import Supervisor\n"
            "signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)\n"
            "previous = {sig: signal.getsignal(sig) for sig in signals}\n"
            "supervisor = Supervisor(Budget(5), Sources(), authorize=lambda: '')\n"
            f"outcome = supervisor.run([sys.executable, '-B', '-c', {worker!r}])\n"
            f"Path({str(result)!r}).write_text(json.dumps({{\n"
            "    'outcome': outcome, 'child_reaped': supervisor.child.poll() is not None,\n"
            "    'restored': all(signal.getsignal(sig) == previous[sig] for sig in signals),\n"
            "    'launches': supervisor.launches}))\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=str(Path(sortie_kit.__file__).resolve().parent.parent),
                   TMPDIR=str(self.root), TMP=str(self.root), TEMP=str(self.root))
        pid, master = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, "-B", str(owner)], env)
        status = None
        try:
            end = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "owned transport never became ready")
            began = time.monotonic()
            if delivered is None:
                os.close(master)  # Actual hangup of only this test's new terminal.
                master = None
            else:
                os.kill(pid, delivered)
            end = began + 2
            while time.monotonic() < end:
                reaped, observed = os.waitpid(pid, os.WNOHANG)
                if reaped:
                    status = observed
                    break
                time.sleep(0.01)
            self.assertIsNotNone(status, "supervisor did not finish bounded cleanup")
            self.assertLess(time.monotonic() - began, 2)
            release.touch()  # No effect is possible until the owner has exited.
            end = time.monotonic() + 0.3
            while not effect.exists() and time.monotonic() < end:
                time.sleep(0.01)
            self.assertFalse(effect.exists(), "transport acted after its owner exited")
            self.assertTrue(os.WIFEXITED(status), "signal bypassed supervisor cleanup")
            self.assertEqual(os.WEXITSTATUS(status), 0)
            self.assertEqual(json.loads(result.read_text()), {
                "outcome": [3, "interrupted"], "child_reaped": True,
                "restored": True, "launches": 1,
            })
        finally:
            if status is None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
            if master is not None:
                os.close(master)
            if ready.exists():
                try:
                    os.killpg(json.loads(ready.read_text())["group"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
