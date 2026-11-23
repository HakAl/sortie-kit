"""Offline preparation inside an explicitly selected Git worktree."""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

from .bounds import Budget


class Refused(ValueError):
    pass


class Elapsed(Refused):
    """Refused because the time given for it ran out, and for no other reason."""


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args],
                            env=env, capture_output=True, timeout=10)
    if result.returncode:
        raise Refused("Git preflight failed: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def safe_child(root: Path, relative: str) -> Path:
    """No symlink components, absolute paths, or traversal in writable state."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ("..", ".") for p in parts):
        raise Refused("unsafe artifact path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise Refused("artifact path contains a symlink: " + relative)
    if not path.resolve().is_relative_to(root.resolve()):
        raise Refused("artifact path escapes repo")
    return path


@dataclass(frozen=True)
class Request:
    repo: Path
    direction: Path
    seconds: float

    def check(self):
        Budget(self.seconds)
        try:
            repo = self.repo.resolve(strict=True)
        except FileNotFoundError:
            raise Refused("there is no directory at " + str(self.repo.absolute()) + "; --repo must name the "
                          "top directory of a Git repository") from None
        top = Path(os.fsdecode(git(repo, "rev-parse", "--show-toplevel")).strip()).resolve()
        if top != repo:
            raise Refused("--repo must name the Git worktree root")
        if git(repo, "ls-files", "-z", "--", ".sortie"):
            raise Refused(".sortie contains tracked artifacts; untrack them before preparing a shift")
        for name in (".sortie", ".sortie/shifts"):
            path = safe_child(repo, name)
            if path.exists() and not path.is_dir():
                raise Refused("unexpected file type: " + name)
        try:
            direction = self.direction.resolve(strict=True)
        except FileNotFoundError:
            raise Refused("there is no direction file at " + str(self.direction.absolute()) + "; write what you "
                          "want done into a text file and name it with --direction") from None
        if not direction.is_file():
            raise Refused("--direction must be a regular UTF-8 file")
        with direction.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if not raw.strip() or len(raw) > 1024 * 1024:
            raise Refused("direction must be nonempty and at most 1 MiB")
        raw.decode("utf-8")
        return repo, raw


def ignored(repo) -> bool:
    """Whether Git ignores the existing .sortie directory itself, by any rule.

    Asked about the directory, not a path under it: Git cannot re-include a file
    whose parent directory is excluded, so this one answer covers the whole tree.
    The directory has to exist; for a missing one Git cannot apply directory rules.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), "check-ignore", "-q", "--", ".sortie"],
                                env=env, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise Refused("Git preflight failed: no answer about ignore rules within ten seconds") from exc
    if result.returncode not in (0, 1):
        raise Refused("Git preflight failed: " + result.stderr.decode("utf-8", "replace").strip())
    return result.returncode == 0


EXCLUDE_LIMIT = 1024 * 1024


def append_exclude(repo):
    """Append the rule to info/exclude under the repository's common Git directory.

    Every step goes through a directory descriptor opened without following a
    link, so the directory that was checked is the directory that is written:
    the common directory itself, then `info`, then the one open of `exclude`.
    A linked worktree's common directory lies outside its checkout; that is
    Git's own answer and is used as given.
    """
    named = Path(os.fsdecode(git(repo, "rev-parse", "--git-common-dir")).rstrip("\n"))
    common = named if named.is_absolute() else repo / named
    held = []
    try:
        try:
            held.append(os.open(common, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            try:
                os.mkdir("info", 0o755, dir_fd=held[0])
            except FileExistsError:
                pass
            held.append(os.open("info", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=held[0]))
            held.append(os.open("exclude", os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                0o644, dir_fd=held[1]))
            info = os.fstat(held[2])
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
            if info.st_nlink != 1:
                # A second name for the same file: an append here would change it there too.
                raise OSError("the file has another name (a hard link)")
            old = b""
            while len(old) <= EXCLUDE_LIMIT and (chunk := os.read(held[2], 65536)):
                old += chunk
            if len(old) > EXCLUDE_LIMIT:
                raise OSError("larger than 1 MiB")
            if b"/.sortie/" not in old.splitlines():
                os.write(held[2], (b"\n" if old and not old.endswith(b"\n") else b"") + b"/.sortie/\n")
        except OSError as exc:
            raise Refused("cannot use the Git exclude file " + str(common / "info" / "exclude") + " (it and the "
                          "directories above it must be real, not links, and the file small, regular and without a second name): "
                          + str(exc)) from exc
    finally:
        for fd in reversed(held):
            os.close(fd)


def exclude(repo):
    """Keep .sortie out of Git's view without touching a file a commit could carry.

    The rule goes into the repository's own exclude file (`.git/info/exclude`),
    which Git never tracks, and only when no rule ignores .sortie already.
    Rules in .gitignore outrank that file: when they re-include .sortie the
    preparation refuses and leaves .gitignore as it is. A rule already appended
    by then stays; it is the rule a later run needs.
    """
    root = safe_child(repo, ".sortie")
    created = not root.exists()
    root.mkdir(exist_ok=True)
    try:
        if ignored(repo):
            return
        append_exclude(repo)
        if not ignored(repo):
            raise Refused(".sortie is re-included by an ignore rule in this repository (for example a "
                          "`!/.sortie/` line in .gitignore), so its files would show up in Git. Remove that "
                          "rule; the toolkit does not edit .gitignore")
    except BaseException:
        if created:
            try:
                root.rmdir()
            except OSError:
                pass
        raise


def prepare(request: Request, sources) -> Path:
    repo, direction = request.check()
    exclude(repo)
    shifts = safe_child(repo, ".sortie/shifts")
    shifts.mkdir(parents=True, exist_ok=True)
    shift_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    shift = safe_child(repo, ".sortie/shifts/" + shift_id)
    shift.mkdir(mode=0o700)
    try:
        for folder in ("briefs", "receipts", "logs", "scratch", "reviews"):
            (shift / folder).mkdir()
        (shift / "direction.md").write_bytes(direction)
        draft = {
            "format": 1, "status": "UNSIGNED_PREPARATION_ONLY", "shift_id": shift_id,
            "repo": str(repo), "direction_sha256": hashlib.sha256(direction).hexdigest(),
            "requested_seconds": request.seconds,
            "seats": {"driver": None, "builder": None},
            "allowed_actions": [],
        }
        (shift / "grant.draft.json").write_text(json.dumps(draft, indent=2) + "\n")
        (shift / "sources.json").write_text(json.dumps(sources.original, indent=2) + "\n")
        data = files("sortie_kit").joinpath("data")
        for name in ("state.md", "completion.md", "driver.md", "builder.md", "tiger.md"):
            (shift / name).write_bytes(data.joinpath("templates", name).read_bytes())
        (shift / "profile.json").write_bytes(data.joinpath("profiles", "example.json").read_bytes())
        (shift / "drive.log").touch()
    except BaseException as exc:
        # Retain partial evidence without ever describing this as an admitted shift.
        try:
            (shift / "preparation-error.txt").write_text(type(exc).__name__ + ": " + str(exc) + "\n")
        except OSError:
            pass
        raise
    return shift
