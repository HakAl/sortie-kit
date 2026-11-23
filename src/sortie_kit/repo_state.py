"""What the target repository looks like to Git: HEAD and what is uncommitted.

An observation for the closeout, taken by the host when a shift begins and
when it ends, so that what a stopped builder turn left behind is named. Git is
asked only for listings of its index and of untracked files, which read no
tracked file's content. Nothing the repository itself configures is run: no
hook, no file-system monitor, no filter and no external diff, because a
builder may have written that configuration. Git compares a tracked file's
content through the repository's filters, so that comparison is done here
instead, without them, and only for files touched since `since`: a `changed`
file is one whose bytes do not hash to what the index holds. A filter that
rewrites content on its way in (line endings, large-file pointers) makes such
a file look changed, and so does an edit from before the shift in a file that
was touched during it. A change of the executable bit alone is not seen.

Where it reads. Git is given the repository's Git directory and its working
tree explicitly, so a `core.worktree` written into the repository's
configuration cannot make the listing describe another directory. Tracked
files are opened one path component at a time from a descriptor of the
repository root, following no link, so a directory swapped for a link cannot
lead a read outside. Git still reads the repository's configuration and what
that includes, and its ignore rules, as it does for any command there: those
decide which untracked names are listed and can hide one, so an empty listing
is not proof that nothing was left.
"""
import hashlib
import json
import os
import select
import stat
import subprocess
import time

LIMIT = 200  # rows kept from one observation
PATH_SHOWN = 400
WORKTREES = 20  # linked working trees under .sortie/ named from one observation
ENCODED = 16 * 1024  # bytes of JSON one observation's rows may come to
OUTPUT = 8 * 1024 * 1024  # bytes read from one Git command
TRACKED = 50000  # index entries looked at
HASHED = 8 * 1024 * 1024  # a touched file larger than this is called changed without being read
REAPING = 0.2  # seconds of a command's time kept for stopping and collecting it
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def run(argv, cwd, seconds, common=None):
    """(output, exit status, cut) of one Git command. Output is read as it comes:
    never more than OUTPUT bytes are kept. `seconds` is one deadline for all of
    it, starting the command, reading it, and stopping and collecting it: the
    reading ends a little earlier (REAPING, or half of a shorter time), and a
    killed command is waited for only until the deadline. `cut` says the
    command was killed at a bound; its status is then None and the output may
    end inside an entry."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    if common is not None:
        # Refs and objects come from this directory, whatever `commondir` says by now.
        env["GIT_COMMON_DIR"] = common
    end = time.monotonic() + seconds
    stop = end - min(REAPING, seconds / 2)
    child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    chunks, size, cut, code = [], 0, False, None
    try:
        fd = child.stdout.fileno()
        while True:
            left = stop - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                cut = True
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > OUTPUT:
                cut = True
                break
        if not cut:
            try:
                code = child.wait(timeout=max(0, stop - time.monotonic()))
            except subprocess.TimeoutExpired:
                cut = True
    finally:
        child.stdout.close()
        if child.poll() is None:
            child.kill()
            try:
                child.wait(timeout=max(0, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass  # killed and not yet collected; nothing is waited for past the deadline
    return b"".join(chunks)[:OUTPUT], None if cut else code, cut


def entries(out, cut):
    """The NUL-terminated entries of a listing; a cut one loses its unfinished last entry."""
    parts = out.split(b"\0")
    return [part for part in (parts[:-1] if cut else parts) if part]


def blob(parent, leaf, mode, width, end, clock):
    """Git's object ID for the file as it is on disk, or None when it cannot be
    read, within the bounds, as the kind of entry the index holds."""
    if mode == "120000":
        data = os.fsencode(os.readlink(leaf, dir_fd=parent))
    else:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            chunks, size = [], 0
            while True:
                if clock() >= end:
                    return None
                chunk = os.read(fd, min(1024 * 1024, HASHED + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > HASHED:
                    return None
            data = b"".join(chunks)
        finally:
            os.close(fd)
    digest = hashlib.sha1(usedforsecurity=False) if width == 40 else hashlib.sha256()
    digest.update(b"blob " + str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


class Walk:
    """Directories of the working tree, opened from the root one component at a
    time and never through a link. The last one stays open: an index listing is
    sorted, so neighbours share it."""
    def __init__(self, root):
        self.root, self.at, self.fd = os.open(root, DIRECTORY), None, None

    def parent(self, parts):
        """A descriptor of the directory holding the path, or the OSError that
        opening it met (a part that is missing, not a directory, or a link)."""
        if self.at != parts[:-1] or self.fd is None:
            self.drop()
            fd = os.dup(self.root)
            try:
                for part in parts[:-1]:
                    nxt = os.open(part, DIRECTORY, dir_fd=fd)
                    os.close(fd)
                    fd = nxt
            except OSError:
                os.close(fd)
                raise
            self.at, self.fd = parts[:-1], fd
        return self.fd

    def drop(self):
        if self.fd is not None:
            os.close(self.fd)
        self.at = self.fd = None

    def close(self):
        self.drop()
        os.close(self.root)


def tracked(root, listing, since, end, clock):
    """Rows for index entries that are gone from disk, and for those touched at or
    after `since` whose bytes are not what the index holds. Returns (rows, cut)."""
    rows, cut, walk = set(), False, Walk(root)
    try:
        for count, entry in enumerate(listing):
            if count >= TRACKED or clock() >= end:
                cut = True
                break
            meta, _, name = entry.partition(b"\t")
            try:
                mode, oid, stage = meta.decode("ascii", "replace").split(" ")
            except ValueError:
                cut = True  # not a line of this listing: say the look was incomplete
                continue
            path = os.fsdecode(name)
            parts = path.split("/")
            if mode == "160000" or stage != "0":
                continue  # a submodule, or an entry in a merge: neither is a file to compare
            if any(part in ("", ".", "..") for part in parts):
                continue
            try:
                parent = walk.parent(parts)
                info = os.lstat(parts[-1], dir_fd=parent)
            except (FileNotFoundError, NotADirectoryError):
                rows.add((path, "deleted"))
                continue
            except OSError:
                rows.add((path, "changed"))  # a directory on its way is now a link, or cannot be opened
                continue
            if since is None or max(info.st_mtime, info.st_ctime) < since - 2:
                continue
            try:
                same = blob(parent, parts[-1], mode, len(oid), end, clock) == oid
            except OSError:
                same = False
            if not same:
                rows.add((path, "changed"))
    finally:
        walk.close()
    return rows, cut


def observe(repo, seconds=3.0, clock=time.monotonic, since=None, git_dir=None, common_dir=None):
    """`{head, changes, truncated, git_dir, common_dir, pinned}` or `{error}`; never
    raises for what Git says.

    `changes` rows are `{path, state}`: `untracked`; `staged` (the index differs
    from HEAD); `deleted` (in the index, not on disk); and, only when `since`
    (seconds since the epoch) is given, `changed`: touched since then and not
    the bytes the index holds. `truncated` says the rows are not the whole
    picture: a listing was cut at a bound of time, output, rows or size.
    `head` is None only when Git answered that HEAD names no commit.
    `git_dir` is this working tree's own Git directory and `common_dir` the one
    its refs and objects come from; they differ for a linked worktree. Given
    both (those an earlier look reported), a repository that now resolves to
    another of either is not read: a `.git` or a `commondir` rewritten in
    between cannot make this look describe something else. `pinned` says the
    look was held to an earlier one in that way. Every listing is told all
    three places outright, so none of them is looked up a second time.
    """
    end = clock() + seconds
    try:
        root = os.path.realpath(repo)
        if not os.path.lexists(os.path.join(root, ".git")):
            raise ValueError("no .git at the repository root")

        def git(base, *args, common=None):
            left = end - clock()
            if left <= 0:
                raise TimeoutError("no time left")
            return run(["git", "--no-optional-locks", "--no-replace-objects", *base, "-c", "core.fsmonitor=false",
                        "-c", "core.hooksPath=" + os.devnull, *args], root, left, common)

        def listing(*args):
            out, code, cut = git(bound, *args, common=shared)
            if code != 0 and not cut:
                raise ValueError("git " + args[0] + " did not answer")
            return entries(out, cut), cut
        # Where Git keeps this working tree's data, found from the root and not from
        # configuration. Every later command is told both places outright.
        out, code, _ = git(("-C", root), "rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir")
        places = out.split(b"\n")
        if code != 0 or len(places) != 3 or not places[0] or not places[1] or places[2]:
            raise ValueError("git rev-parse did not answer")
        found, shared = os.fsdecode(places[0]), os.path.realpath(os.fsdecode(places[1]))
        pinned = git_dir is not None and common_dir is not None
        if (git_dir is not None and found != git_dir) or (common_dir is not None and shared != common_dir):
            raise ValueError("the repository's Git directory is not the one seen when the shift began")
        bound = ("--git-dir=" + found, "--work-tree=" + root)
        out, code, _ = git(bound, "rev-parse", "--verify", "-q", "HEAD^{commit}", common=shared)
        if code == 0 and out.strip():
            head = out.decode("ascii", "replace").strip()
        elif code == 1 and not out.strip():
            head = None  # Git's answer for a HEAD that names no commit
        else:
            raise ValueError("git rev-parse HEAD did not answer")
        index, cut = listing("ls-files", "-z", "--stage")
        untracked, cut_untracked = listing("ls-files", "-z", "--others", "--exclude-standard")
        staged, cut_staged = (listing("diff-index", "-z", "--cached", "--name-only", "--no-ext-diff", "--no-textconv", head)
                              if head else listing("ls-files", "-z", "--cached"))
        rows, cut_tracked = tracked(root, index, since, end, clock)
        linked = worktrees(lambda *args: git(bound, *args, common=shared), root)
    except (OSError, ValueError, TimeoutError) as exc:
        return dict(error=str(exc)[:200])
    rows |= {(os.fsdecode(name), state) for state, names in (("untracked", untracked), ("staged", staged)) for name in names}
    kept, size = [], 0
    for path, state in sorted((path[:PATH_SHOWN], state) for path, state in rows):
        row = dict(path=path, state=state)
        size += len(json.dumps(row)) + 1
        if len(kept) >= LIMIT or size > ENCODED:
            break
        kept.append(row)
    seen = dict(head=head, changes=kept,
                truncated=cut or cut_untracked or cut_staged or cut_tracked or len(kept) < len(rows),
                git_dir=found, common_dir=shared, pinned=pinned)
    if linked:
        seen["worktrees"] = linked  # only when there are some: an older reader sees what it knew
    return seen


def worktrees(git, root):
    """Linked working trees Git has registered under the repository's `.sortie/`, as
    paths from the repository root; at most WORKTREES. A builder makes them under a
    shift's `scratch/` for its checks, and Git lists them until they are pruned.
    Advisory and last: when Git does not answer in the time left, none are named."""
    try:
        out, code, cut = git("worktree", "list", "--porcelain")
    except (OSError, ValueError, TimeoutError):
        return []
    if code != 0 or cut:
        return []
    inside, found = os.path.join(root, ".sortie") + os.sep, []
    for line in out.split(b"\n"):
        if line.startswith(b"worktree "):
            path = os.path.realpath(os.fsdecode(line[9:]))
            if path.startswith(inside) and len(found) < WORKTREES:
                found.append(os.path.relpath(path, root)[:PATH_SHOWN])
    return sorted(found)


OBJECT = frozenset("0123456789abcdef")


def held(repo, start, end, seconds):
    """Which paths uncommitted at the `start` look a commit made since holds.

    One Git command, bounded and told the same things as the listings: the
    commits reachable from the HEAD of the `end` look and not from that of
    the `start` look, and which of the start's paths any of them added or
    changed. `{paths, complete}`, `{error}`, or None when there is nothing to
    look for (a look that is no listing, or no uncommitted path at the start).
    `complete` is false when the answer may leave a path out: the start look
    was cut, a path may have been shortened in it, the command was cut, or it
    listed something that is not one of the paths asked about.
    A path a commit holds is then still named. Commits on a branch HEAD is not
    on at the end are not looked at.
    """
    if not usable(start) or not usable(end) or not start["changes"]:
        return None
    first, last = start.get("head"), end.get("head")
    if first == last:
        return dict(paths=[], complete=True)  # no commit between the two

    def commit(head):
        return isinstance(head, str) and len(head) in (40, 64) and set(head) <= OBJECT
    places = [start.get(name) for name in ("git_dir", "common_dir")]
    if not commit(last) or not (first is None or commit(first)) or not all(isinstance(p, str) and p for p in places):
        return dict(error="no commit or Git directory to look from")
    every = {row["path"] for row in start["changes"]}
    names = sorted(path for path in every if path and len(path) < PATH_SHOWN)
    whole = len(names) == len(every) and start.get("truncated") is False
    if not names:
        return dict(paths=[], complete=False)
    if seconds <= 0:
        return dict(error="no time left")
    try:
        root = os.path.realpath(repo)
        out, code, cut = run(
            ["git", "--no-optional-locks", "--no-replace-objects", "--literal-pathspecs",
             "--git-dir=" + places[0], "--work-tree=" + root, "-c", "core.fsmonitor=false",
             "-c", "core.hooksPath=" + os.devnull, "-c", "log.showSignature=false", "-c", "log.follow=false",
             # No mailmap is read, in the working tree or wherever `mailmap.file` points; and
             # the first commit's paths are listed whatever `log.showRoot` says.
             "-c", "log.mailmap=false", "-c", "mailmap.file=", "-c", "mailmap.blob=",
             "log", "-z", "-m", "--root", "--full-history", "--no-renames", "--no-ext-diff", "--no-textconv",
             "--no-show-signature", "--no-mailmap", "--diff-filter=AMT", "--name-only", "--format=",
             (first + ".." if first else "") + last, "--", *names], root, seconds, places[1])
    except OSError as exc:
        return dict(error=str(exc)[:200])
    if code != 0 and not cut:
        return dict(error="git log did not answer")
    # Names as Git wrote them, untrimmed. Only paths that were asked about can be
    # listed; anything else means the output was not read as it was meant.
    found = {os.fsdecode(entry) for entry in entries(out, cut)}
    return dict(paths=sorted(found & set(names)), complete=whole and not cut and found <= set(names))


def usable(seen):
    return (isinstance(seen, dict) and isinstance(seen.get("changes"), list)
            and all(isinstance(row, dict) and isinstance(row.get("path"), str) and isinstance(row.get("state"), str)
                    for row in seen["changes"]))


def new(start, end):
    """Rows of `end` that `start` did not have; None when either is not a listing."""
    if not usable(start) or not usable(end):
        return None
    before = {(row["path"], row["state"]) for row in start["changes"]}
    return [row for row in end["changes"] if (row["path"], row["state"]) not in before]
