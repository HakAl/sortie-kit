"""Run using an installed wheel's interpreter; all fixtures remain repo-local."""
import base64
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile

import sortie_kit

ROOT = Path(__file__).resolve().parents[1]
scratch = ROOT / ".scratch" / "install-check"
scratch.mkdir(parents=True, exist_ok=True)
assert Path(sortie_kit.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
cli = Path(sysconfig.get_path("scripts")) / "sortie"

with tempfile.TemporaryDirectory(dir=scratch) as directory:
    work = Path(directory)
    target = work / "target with spaces"
    target.mkdir()
    (target / "direction.md").write_text("Build a small feature.\n")
    trap = work / "bin"
    trap.mkdir()
    marker = work / "herdr-called"
    (trap / "herdr").write_text('#!/bin/sh\nprintf called > "$HERDR_TRAP"\nexit 99\n')
    (trap / "herdr").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("GIT_")}
    env.update(TMPDIR=str(work), TMP=str(work), TEMP=str(work), PYTHONDONTWRITEBYTECODE="1",
               HERDR_TRAP=str(marker), PATH=str(trap) + os.pathsep + env.get("PATH", ""))
    # A home of the check's own: what the person running it keeps in theirs is not looked at.
    home = work / "home"
    usual = home / ".config" / "sortie-kit"
    usual.mkdir(parents=True, mode=0o700)
    env.update(HOME=str(home))
    empty_config = work / "empty-gitconfig"; empty_config.touch()
    empty_hooks = work / "empty-hooks"; empty_hooks.mkdir()
    env.update(GIT_CONFIG_GLOBAL=str(empty_config), GIT_CONFIG_SYSTEM=str(empty_config),
               GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="core.hooksPath",
               GIT_CONFIG_VALUE_0=str(empty_hooks), GIT_CONFIG_KEY_1="commit.gpgsign", GIT_CONFIG_VALUE_1="false")

    def run(argv, expected=0):
        result = subprocess.run([str(a) for a in argv], cwd=work, env=env,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == expected, (argv, result.returncode, result.stdout, result.stderr)
        return result

    run(["git", "init", "-q", target])
    # One source for the version: the command, the package and the installed metadata agree.
    from importlib.metadata import version
    assert run([cli, "--version"]).stdout.strip() == sortie_kit.__version__ == version("sortie-kit")
    args = [cli, "start", "--repo", target, "--direction", target / "direction.md",
            "--for", "1m"]
    live = run(args, 2)
    assert "sortie: missing: " in live.stderr and "--agent driver=NAME:MODEL" in live.stderr
    seats = ["--agent", "driver=claude:example", "--agent", "builder=codex:example"]
    assert "missing: --trust FILE" in run(args + seats, 2).stderr
    # The reviewer is a seat that is picked; the earlier flag for a review model names the new spelling.
    assert "--agent reviewer=NAME[:MODEL]" in run(args + seats + ["--review", "gemini-4-carbon-high"], 3).stderr
    # Every seat named but no duration, and no terminal here to ask in: listed, exit 2, nothing written.
    unasked = run(args[:-2] + seats + ["--trust", work / "no-trust", "--key", work / "no-key"], 2).stderr
    assert "missing: --for DURATION; no terminal to ask in" in unasked
    config = work / "config"
    config.mkdir(mode=0o700)
    (config / "signing-key.pub").write_text(
        "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(32)).decode()
        + " synthetic, never used to sign\n")
    trust_args = [cli, "trust", "init", "--key", config / "signing-key", "--out", config / "trust.json"]
    assert "Trust file written: " in run(trust_args).stdout
    assert (config / "trust.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((config / "trust.json").read_text())["identity"] == "developer"
    assert "already exists" in run(trust_args, 3).stderr
    # Without flags the trust file goes to the usual place, and a start finds it and the key there.
    (usual / "signing-key.pub").write_text((config / "signing-key.pub").read_text())
    assert "finds both there and needs no --trust or --key flag" in run([cli, "trust", "init"]).stdout
    half = run(args + seats, 2).stderr
    assert "--key FILE" in half and "--trust FILE" not in half, half
    (usual / "signing-key").write_text("stands in for a key; never read here\n")
    found = run(args[:-2] + seats, 2).stderr
    assert "Using the trust file " + str(usual / "trust.json") in found, found
    assert "missing: --for DURATION; no terminal to ask in" in found, found
    assert json.loads(run([cli, "wait", "--shift", target], 3).stdout)["status"] == "refused"
    assert not (target / ".sortie").exists()
    run(args + ["--prepare-only", "--reviewer", "reviewer"], 2)
    for old in (["--driver", "driver"], ["--builder", "builder"], ["--runtime", "builder=codex"]):
        assert "--agent" in run(args + ["--prepare-only"] + old, 3).stderr
    assert "only when signing" in run(args + ["--prepare-only", "--agent", "builder=codex:example"], 3).stderr
    assert not (target / ".sortie").exists()
    run(args + ["--prepare-only"])
    shifts = list((target / ".sortie/shifts").iterdir())
    assert len(shifts) == 1
    for name in ("grant.draft.json", "state.md", "completion.md", "profile.json", "sources.json"):
        assert (shifts[0] / name).is_file(), name
    assert not (shifts[0] / "events.jsonl").exists()
    assert json.loads((shifts[0] / "grant.draft.json").read_text())["allowed_actions"] == []
    assert json.loads((shifts[0] / "grant.draft.json").read_text())["seats"] == {"driver": None, "builder": None}
    assert json.loads((shifts[0] / "profile.json").read_text())["roles"] == ["driver", "builder"]
    assert not marker.exists(), "installed command called herdr"
    from importlib.resources import files
    templates = files("sortie_kit").joinpath("data", "templates")
    # tiger.md is AGENTS.md without its one block marked as for this repository only.
    # A published AGENTS.md has no such block and is tiger.md byte for byte.
    # Appended, so nothing under tests/ can stand in for an installed module.
    sys.path.append(str(Path(__file__).resolve().parent))
    from support import without_repository_block
    assert templates.joinpath("tiger.md").read_bytes() == without_repository_block((ROOT / "AGENTS.md").read_bytes())
    assert all("this repository only" not in item.read_text().casefold() for item in templates.iterdir())
    # The installed codex builder is given no network unless the start asks for the second form.
    codex = json.loads(files("sortie_kit").joinpath("data", "agents", "codex.json").read_text())
    assert "sandbox_workspace_write.network_access=true" not in codex["headless_argv"]
    assert "sandbox_workspace_write.network_access=true" in codex["headless_network_argv"]
    assert "only when signing" in run([cli, "start", "--repo", target, "--direction", target / "direction.md",
                                       "--for", "1m", "--prepare-only", "--builder-network"], 3).stderr
    for role in ("driver", "builder"):
        instruction = run([cli, "instructions", role]).stdout
        assert "driver" in instruction.lower()
        assert (shifts[0] / (role + ".md")).is_file()
    assert "under `answers`" in run([cli, "instructions", "builder"]).stdout
    assert "TOOLKIT receipt write --shift SHIFT" in run([cli, "instructions", "builder"]).stdout
    # The review contract ships in the wheel: what a reviewer is told, and the check of its answer.
    for kind in ("plan", "commit"):
        told = run([cli, "review", "instructions", kind]).stdout
        assert '"schema": "sortie-kit.review.v' + ("3" if kind == "commit" else "1") + '"' in told and "=====BEGIN SORTIE REVIEW " in told
    assert '"status": "unusable"' in run([cli, "review", "check", "/nonexistent", "--expected", "/nonexistent"], 2).stdout
    # The pane transport, the journal and the runtime starters are not in the wheel.
    import importlib.util
    for gone in ("send", "wait", "tick", "runtime", "journal"):
        assert importlib.util.find_spec("sortie_kit." + gone) is None, gone
    assert not files("sortie_kit").joinpath("data", "runtime").is_dir()
    assert "invalid choice" in run([cli, "runtime", "example", "--runtime", "codex", "--role", "driver",
                                    "--part", "config"], 2).stderr
    receipt = json.loads(run([cli, "receipt", "template", "acknowledgment"]).stdout)
    expected = json.loads(run([cli, "receipt", "template", "acknowledgment", "--expected"]).stdout)
    # The builder starts no review, so there is no review command for it to have checked.
    assert "invalid choice: 'check-review'" in run([cli, "receipt", "check-review", "--shift", str(shifts[0])], 2).stderr
    # Nothing in the wheel names the earlier review script or a skills directory.
    def packaged(node):
        for item in node.iterdir():
            if item.is_dir():
                yield from packaged(item)
            elif not item.name.endswith(".pyc"):
                yield item
    for item in packaged(files("sortie_kit")):
        text = item.read_bytes().decode("utf-8", "replace")
        for name in ("agy_review.py", ".agents/skills", ".claude/skills", ".codex/skills"):
            assert name not in text, (str(item), name)
    shift = shifts[0]
    brief = {"path": "direction.md", "sha256": hashlib.sha256((shift / "direction.md").read_bytes()).hexdigest()}
    for item in (receipt, expected):
        item.update(shift_id=shift.name, brief=brief)
    receipt["receipt_id"] = "installed-ack"
    evidence = shift / "logs/ack.txt"
    evidence.write_text("Builder acknowledges the bound brief.\n")
    receipt["data"]["evidence"] = [{"path": "logs/ack.txt", "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]
    (shift / "expected.json").write_text(json.dumps(expected))
    # The installed command writes the receipt from a filled template kept in the shift.
    (shift / "logs/draft-ack.json").write_text(json.dumps(receipt))
    wrote = json.loads(run([cli, "receipt", "write", "--shift", shift, "--expected", "expected.json",
                            "--from", "logs/draft-ack.json"]).stdout)
    assert (wrote["status"], wrote["path"], wrote["check"]["status"]) == \
        ("written", "receipts/installed-ack.json", "diagnostic"), wrote
    assert json.loads(run([cli, "receipt", "write", "--shift", shift, "--expected", "expected.json",
                           "--from", "logs/draft-ack.json"], 3).stdout)["status"] == "invalid"
    receipt_args = [cli, "receipt", "validate", "--shift", shift, "--receipt", "receipts/installed-ack.json",
                    "--expected", "expected.json"]
    for _ in range(2):
        diagnostic = json.loads(run(receipt_args, 5).stdout)
        assert diagnostic["status"] == "diagnostic"
        assert diagnostic["satisfies_phase"] is False and diagnostic["recorded"] is False
    recorded = json.loads(run(receipt_args + ["--record"]).stdout)
    assert recorded["status"] == "satisfied"
    assert recorded["satisfies_phase"] is True and recorded["recorded"] is True
    assert json.loads(run(receipt_args + ["--record"], 4).stdout)["satisfies_phase"] is False
    execution_marker = work / "check-must-not-run"
    check_spec = dict(version=1, id="queued-only", task_id="fixture", request_id="fixture", round=0,
                      finding_id="F1", candidate="0" * 40, baseline=None, baseline_reason="Queue-only check.",
                      review_request=receipt["data"]["evidence"][0],
                      argv=[sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(execution_marker)) + ").touch()"],
                      timeout_seconds=1, baseline_exit=1, candidate_exit=0,
                      baseline_contains="unused", candidate_contains="unused", harness=None,
                      harness_reason="Queue-only check.")
    (shift / "briefs/check.json").write_text(json.dumps(check_spec))
    queued = json.loads(run([cli, "queue-check", "--shift", shift, "--request", "briefs/check.json"]).stdout)
    assert queued["status"] == "queued" and not execution_marker.exists()
    assert (shift / "builder-check.json").is_file()
    summary = json.loads(run([cli, "report", "--shift", shift, "--for", "3s"]).stdout)
    assert summary["status"] == "partial"  # Preparation has no admitted driver state.
    document = json.loads((shift / "report.json").read_text())
    assert document["grant"]["provenance"] == "unsigned preparation only"
    assert document["delta"]["tasks"]["value"] == "unknown"
    assert hashlib.sha256((shift / "report.json").read_bytes()).hexdigest() in (shift / "report.md").read_text()
    assert templates.joinpath("closing-report.md").is_file()
    # The same retained-evidence fixtures exercise the installed package, not source imports.
    run([sys.executable, "-B", "-m", "unittest", "discover", "-s", ROOT / "tests", "-p", "test_report.py", "-v"])
    assert not execution_marker.exists(), "reporting executed a queued check"
    from sortie_kit.receipt_schema import asset, decode, validate_shape
    from sortie_kit.receipts import PHASES
    for phase in PHASES:
        validate_shape(decode(asset(phase + "-v3.json")), "receipt")
        validate_shape(decode(asset("expected-" + phase + "-v3.json")), "expected")
    for module in ("tick_supervise",):
        run([sys.executable, "-B", "-m", "sortie_kit." + module], 2)
        run([sys.executable, "-B", "-m", "sortie_kit." + module, "--for", "inf"], 2)
        run([sys.executable, "-B", "-m", "sortie_kit." + module, "--for", "1s"], 3)
    assert not marker.exists()
print("PASS: installed CLI/resources, no pane-era modules, receipt schemas/validation/replay refusal, offline preparation through report, complete report fixtures, live start and wait refusals, missing choices listed, trust init, finite entry points, zero herdr calls")
