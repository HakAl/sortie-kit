"""What the host puts in front of a reviewer, and how it tells that a review changed something.

Nothing here starts a reviewer. One directory is made for one run of one
review, under the shift: the review instructions, the brief, the plan, the
task's whole patch and a plain export of the files (`git archive`, not a
linked worktree, so nothing the reviewer does there reaches the repository's
Git data). The reviewer is started in that directory.

`git archive` leaves out paths the tree itself marks `export-ignore`. The
patch beside it is made without that rule and shows every change, the
attribute included.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import time

from . import review
from .layout import Refused

FOLDER = "reviews"
PATCH_LIMIT = 16 * 1024 * 1024
TREE_LIMIT = 1024 * 1024 * 1024  # bytes of the exported archive
NAME = review.re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def git(repo, args, seconds, *, stdout=subprocess.PIPE):
    """One read-only Git command on the repository, as the receipt check runs them:
    no inherited Git variables, no optional locks, no replaced objects, no prompt."""
    if seconds <= 0:
        raise Refused("no time was left for a Git command")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(["git", "--no-optional-locks", "--no-replace-objects", "-C", str(repo),
                                 "-c", "core.fsmonitor=false", *args],
                                env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.PIPE, timeout=seconds)
    except subprocess.TimeoutExpired as exc:
        raise Refused("a Git command did not finish in its time: git " + args[0]) from exc
    except OSError as exc:
        raise Refused("Git could not be run: " + str(exc)[:200]) from exc
    return result


def head(repo, seconds=5):
    """The full ID of the commit the repository is at, or None when it has none yet."""
    result = git(repo, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], seconds)
    value = result.stdout.decode("ascii", "replace").strip()
    if result.returncode == 0 and review.COMMIT.fullmatch(value):
        return value
    if result.returncode == 1 and not value:
        return None  # an unborn branch
    raise Refused("the repository's current commit could not be read")


def snapshot(repo, seconds=5):
    """The repository's commit and status, to compare before and after a review.
    The shift itself is under an excluded directory and is no part of the status."""
    end = time.monotonic() + seconds
    at = head(repo, seconds)
    result = git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"], end - time.monotonic())
    if result.returncode:
        raise Refused("the repository's status could not be read")
    return dict(head=at, status_sha256=hashlib.sha256(result.stdout).hexdigest())


def empty_tree(repo, seconds):
    result = git(repo, ["hash-object", "-t", "tree", os.devnull], seconds)
    value = result.stdout.decode("ascii", "replace").strip()
    if result.returncode or not review.COMMIT.fullmatch(value):
        raise Refused("the empty tree could not be named")
    return value


def patch(repo, base, commit, seconds):
    """The whole change from `base` (None: from nothing) to `commit`, as bytes."""
    end = time.monotonic() + seconds
    for value in (base, commit):
        if value is not None and not review.COMMIT.fullmatch(value):
            raise Refused("a patch is made between full commit IDs")
    start = base if base is not None else empty_tree(repo, end - time.monotonic())
    result = git(repo, ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", start, commit, "--"],
                 end - time.monotonic())
    if result.returncode:
        raise Refused("the task's patch could not be made")
    if len(result.stdout) > PATCH_LIMIT:
        raise Refused("the task's patch is too large to hand to a reviewer")
    return result.stdout


def member_ok(member):
    name = member.name
    return ((member.isreg() or member.isdir()) and name and not name.startswith("/")
            and ".." not in name.split("/") and "\x00" not in name)


def export(repo, commit, where, seconds):
    """Write the files of `commit` under the new directory `where`. Returns how many
    entries the archive held that were not written (links and special files when
    this Python has no extraction filter)."""
    end = time.monotonic() + seconds
    if not review.COMMIT.fullmatch(commit):
        raise Refused("an export is of a full commit ID")
    os.mkdir(where, 0o700)
    # The archive of a commit that holds no file is a header and nothing else, which
    # tarfile refuses to read: such a commit is exported as the empty directory.
    tree = git(repo, ["rev-parse", "--verify", "--quiet", commit + "^{tree}"], end - time.monotonic())
    if tree.returncode:
        raise Refused("the commit could not be exported for the review")
    if tree.stdout.decode("ascii", "replace").strip() == empty_tree(repo, end - time.monotonic()):
        return 0
    archive = where + ".tar"
    fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            result = git(repo, ["archive", "--format=tar", commit], end - time.monotonic(), stdout=stream)
        if result.returncode:
            raise Refused("the commit could not be exported for the review")
        if os.stat(archive).st_size > TREE_LIMIT:
            raise Refused("the repository is too large to export for a review")
        skipped = 0
        try:
            with tarfile.open(archive, "r:") as tar:
                if hasattr(tarfile, "data_filter"):
                    for member in tar:
                        if time.monotonic() >= end:
                            raise Refused("the export for the review did not finish in its time")
                        tar.extract(member, where, filter="data")
                else:
                    for member in tar:
                        if time.monotonic() >= end:
                            raise Refused("the export for the review did not finish in its time")
                        if member.type in (tarfile.XGLTYPE, tarfile.XHDTYPE):
                            continue
                        if not member_ok(member):
                            skipped += 1
                            continue
                        tar.extract(member, where)
        except (tarfile.TarError, OSError) as exc:
            raise Refused("the export for the review could not be unpacked: " + str(exc)[:200]) from exc
        return skipped
    finally:
        try:
            os.unlink(archive)
        except FileNotFoundError:
            pass


def make(view, name, *, kind, target, base, brief, plan, prior, findings, answers, nonce, seconds):
    """Make reviews/NAME in the shift for one run of one review and fill it.

    `view` is the shift's open evidence view. `target` is the reviewed thing as
    receipts name it; `base` the commit the task began at (None: the
    repository had none); `brief` and `plan` are artifacts of the shift (plan
    may be None for a commit review); `prior` the earlier findings to answer
    as [{id, severity}], with `findings` their full text and `answers` what
    the builder said of each. Returns the directory's path relative to the
    shift and what the instructions' SHA256 is.
    """
    end = time.monotonic() + seconds
    if not NAME.fullmatch(name):
        raise Refused("a review directory has a plain name")
    text = review.instructions(kind, target, nonce, prior)
    files = {"REVIEW.md": text.encode(), "brief.md": view.artifact(brief)}
    if kind == "plan":
        files["plan.md"] = view.artifact(target["artifact"])
        commit = head(view.repo, end - time.monotonic())
    else:
        commit = target["commit"]
        if plan is not None:
            files["plan.md"] = view.artifact(plan)
        files["change.patch"] = patch(view.repo, base, commit, end - time.monotonic())
    if prior:
        files["answers.json"] = (json.dumps(dict(earlier_findings=findings, builder_answers=answers), indent=2,
                                            sort_keys=True) + "\n").encode()
    shift = os.dup(view.fd)
    try:
        try:
            os.mkdir(FOLDER, 0o700, dir_fd=shift)
        except FileExistsError:
            pass
        parent = os.open(FOLDER, DIRECTORY, dir_fd=shift)
        try:
            os.mkdir(name, 0o700, dir_fd=parent)  # new: a directory already there is never reused
            folder = os.open(name, DIRECTORY, dir_fd=parent)
            try:
                for leaf, raw in files.items():
                    fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
            finally:
                os.close(folder)
        finally:
            os.close(parent)
    except FileExistsError as exc:
        raise Refused("the review directory " + FOLDER + "/" + name + " already exists") from exc
    finally:
        os.close(shift)
    relative = FOLDER + "/" + name
    where = os.path.join(str(view.shift), FOLDER, name)
    if commit is not None:
        export(view.repo, commit, os.path.join(where, "tree"), end - time.monotonic())
    else:
        os.mkdir(os.path.join(where, "tree"), 0o700)  # a repository without a commit has no files to show
    view.guard()
    return relative, hashlib.sha256(files["REVIEW.md"]).hexdigest()
