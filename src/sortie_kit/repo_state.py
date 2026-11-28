"""What the target repository looks like to Git: HEAD and what is uncommitted.

Two looks. `observe` is an observation for the closeout, taken by the host when a shift begins and
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

`candidate_look` is the host's look for a review of a candidate commit: the
branch, the commits of the task, the remotes of the repository's own
configuration, the remote-tracking refs that hold the candidate, what is
not committed, the candidate's commit message and whether tracked files have
edits that are not staged. It is held to the same rules and gives no URL
beyond a scheme and a host. The message is read from the commit object by
one command and parsed here. Edits that are not staged are found as `observe`
finds them, by the host's own comparison of bytes and never by a comparison
of Git's (`diff`, `diff-files`, `ls-files -m` and `status` can each run the
repository's clean filter), and for every tracked file as far as the time
allows, whatever the file's times: an edit that the index and not the file
made stale (`git reset --mixed`) is seen too.
"""
import errno
import hashlib
import itertools
import json
import os
import re
import select
import stat
import subprocess
import time

from . import cli_words

LIMIT = 200  # rows kept from one observation
PATH_SHOWN = 400
WORKTREES = 20  # linked working trees under .sortie/ named from one observation
ENCODED = 16 * 1024  # bytes of JSON one observation's rows may come to
OUTPUT = 8 * 1024 * 1024  # bytes read from one Git command
TRACKED = 50000  # index entries looked at
HASHED = 8 * 1024 * 1024  # a touched file larger than this is called changed without being read
REAPING = 0.2  # seconds of a command's time kept for stopping and collecting it
REMOTES = 20  # remotes named from one look
NAME_SHOWN = 200  # characters of a remote's name kept in a look
URLS = 4  # values of one remote's `url` or `pushurl` given in a look for a review
LOOK_SECONDS = 3.0  # the most a look for a review takes
COMMITS = 1000  # commits of a task counted
COMMITS_SHOWN = 20
UNTRACKED = 1000  # untracked files counted
UNTRACKED_SHOWN = 50
TRACKING = 20  # remote-tracking refs that hold a candidate, named
# In the order they are looked at. The last two came later and give way first when the time is short.
FACTS = ("branch", "commits", "remotes", "uncommitted", "tracking", "message", "unstaged")
MESSAGE_OBJECT = 64 * 1024  # bytes read of the candidate's commit object: a larger one is not read
MESSAGE_HEAD, MESSAGE_TAIL = 28, 12  # lines shown of a message that has more than both together
MESSAGE_LINES = MESSAGE_HEAD + MESSAGE_TAIL
MESSAGE_LINE = 200  # characters of one shown line of a message, escapes and the mark of a cut counted
MESSAGE_PARTS = 8  # places where something that is no text parts a line, each read closed and as a space
# Characters read of one message for the readings of its parted lines (a line: its readings times its
# length). The mask takes about 80 ns a character, so this is about a tenth of a second.
MESSAGE_WORK = 1024 * 1024
ENCODING = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")  # a value of the header `encoding` that is shown
UNSTAGED_SHOWN = 50  # names given of each kind of tracked file that is not what the index holds
# The keys of the repository's own configuration that make Git convert content on its way in or out.
CONVERTS = r"^(filter\..*|core\.autocrlf|core\.eol)$"
NOT_SET = frozenset((b"false", b"no", b"off", b"0"))  # values of `core.autocrlf` that convert nothing
PLAIN_FLAGS, SPARSE_FLAGS, MERGE_FLAGS = frozenset("Hh"), frozenset("Ss"), frozenset("Mm")  # of `ls-files -v`
# Why a fact is not in a look for a review.
LATE = "the time for preparing this review did not allow it"
SILENT = "Git did not answer"
CUT = "the listing was cut at a bound"
MOVED = "the repository's Git directory is not the one seen when the shift began"
UNKNOWN = "Git listed the index in a form the host does not know"
# What `content` gives in place of a file's bytes, beside LATE.
UNREADABLE, OTHER = "not read", "not what the index holds there"
REMOTE_KEY = r"^remote\."  # every key of every remote: Git takes a section for a remote whatever it sets
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
PORT = re.compile(r"[0-9]{1,5}\Z")
SCHEMES = ("http", "https", "ssh", "git")
NO_HOST = frozenset(SCHEMES + ("file", "ftp", "ftps"))  # before a single colon these are a scheme, not a host
UNLISTED = ("added", "changed", "unsure")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def run(argv, cwd, seconds, common=None, limit=None):
    """(output, exit status, cut) of one Git command. Output is read as it comes:
    never more than `limit` bytes are kept, OUTPUT where none is given. `seconds` is one deadline for all of
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
    limit = OUTPUT if limit is None else limit  # read now: the bound is the module's at the call
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
            if size > limit:
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
    return b"".join(chunks)[:limit], None if cut else code, cut


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


def content(parent, leaf, mode, left):
    """The bytes of a tracked file as it is on disk, for the kind of entry the index
    holds (`mode`): a link's target for a link, read and never followed. In their
    place LATE when `left`, asked before the file and before each piece of it, says
    that no time is left; UNREADABLE for a file larger than HASHED or one that
    cannot be opened or read; OTHER where something else stands there now, a link
    where the index holds a file, a file where it holds a link, a directory."""
    if left() <= 0:
        return LATE
    try:
        if mode == "120000":
            try:
                return os.fsencode(os.readlink(leaf, dir_fd=parent))
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    return OTHER  # there, and no link
                raise
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return OTHER  # a link: what it names is never read
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return OTHER
            chunks, size = [], 0
            while True:
                if left() <= 0:
                    return LATE
                chunk = os.read(fd, min(1024 * 1024, HASHED + 1 - size))
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > HASHED:
                    return UNREADABLE
        finally:
            os.close(fd)
    except OSError:
        return UNREADABLE


def object_id(data, width):
    """Git's object ID for a blob of these bytes, in the hash whose IDs have `width` characters."""
    mark = hashlib.sha1(usedforsecurity=False) if width == 40 else hashlib.sha256()
    mark.update(b"blob " + str(len(data)).encode() + b"\0" + data)
    return mark.hexdigest()


def unstaged_edits(root, listing, since, left):
    """Which tracked files are not what the index holds, by the host's own comparison
    of bytes: the fact `unstaged` of `candidate_look` without `since` and `converts`,
    or None when `listing`, the entries of `git ls-files -z -v --stage`, is not in a
    form the host knows.

    Each entry is looked at whatever its times. An entry a sparse checkout keeps off
    disk is counted and not looked for (`sparse`); a submodule and an entry of a merge
    are counted (`skipped`). Of the others first each is looked for, and one that is
    gone, or whose directory is gone or is a link now, is `deleted`. Then each is read
    (`content`), those first whose later time is at or after `since` less 2 seconds,
    so that when the time ends the likeliest are done: `changed` when its bytes are
    not the index's object, `endings` when they are once every CR LF is read as LF,
    `unread` when it could not be read or is larger than HASHED, which is never
    called changed. `left` is asked before each entry; when it says that no time is
    left the walk ends, the file in hand is counted as nothing and `whole` is false.
    `compared` counts the entries whose outcome was reached, `entries` those that
    were to be compared, and `touched` says whether all touched since `since` were
    among the compared (None without `since`): not when the time ended before one of
    them, when one of them was unread, or when the index holds more entries than
    TRACKED, of which only the first are looked at (`whole` is false then too)."""
    plain, sparse, skipped, more, late = [], 0, 0, False, False
    for entry in listing:
        head, tab, name = entry.partition(b"\t")
        fields = head.decode("ascii", "replace").split(" ")
        if not tab or not name or len(fields) != 4 or len(fields[0]) != 1:
            return None
        flag, mode, oid, stage = fields
        if flag in SPARSE_FLAGS:
            sparse += 1
            continue
        path = os.fsdecode(name)
        parts = path.split("/")
        if flag in MERGE_FLAGS or stage != "0" or mode == "160000" or any(part in ("", ".", "..") for part in parts):
            skipped += 1
            continue
        if flag not in PLAIN_FLAGS or len(oid) not in (40, 64) or not set(oid) <= OBJECT:
            return None
        if len(plain) >= TRACKED:
            more = True  # more entries than are looked at: those taken up are compared, and that is not all
            break
        plain.append((path, parts, mode, oid))
    found = dict(changed=[], deleted=[], endings=[])
    unread = compared = 0
    touched_all = True  # no file touched since the start, or one that may have been, went unread
    first, rest, walk = [], [], Walk(root)
    try:
        # Where each is, and when it was last touched. Nothing of a file is read here.
        for number, (path, parts, mode, oid) in enumerate(plain):
            if left() <= 0:
                late = True  # the order is not known for all, and there is no time: none is read
                break
            try:
                info = os.lstat(parts[-1], dir_fd=walk.parent(parts))
            except (FileNotFoundError, NotADirectoryError):
                found["deleted"].append(path)
                compared += 1
                continue
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    found["deleted"].append(path)  # a directory on its way is a link now
                    compared += 1
                else:
                    unread += 1
                    touched_all = False  # when it was touched is not known
                continue
            recent = since is not None and max(info.st_mtime, info.st_ctime) >= since - 2
            (first if recent or since is None else rest).append((path, parts, mode, oid))
        every = since is not None and not late and not more  # whether all touched since then get compared
        for group in (first, rest):
            for path, parts, mode, oid in group:
                if late:
                    break
                if left() <= 0:  # before its directory is opened, too
                    late, every = True, every and group is rest
                    break
                try:
                    data = content(walk.parent(parts), parts[-1], mode, left)
                except OSError:
                    data = UNREADABLE  # its directory was there a moment ago and cannot be opened now
                if isinstance(data, str) and data == LATE:
                    late = True
                    every = every and group is rest
                    break
                if isinstance(data, str):
                    if data == OTHER:
                        found["changed"].append(path)
                        compared += 1
                    else:
                        unread += 1
                        touched_all = touched_all and group is rest
                    continue
                compared += 1
                if object_id(data, len(oid)) == oid:
                    continue
                same = mode != "120000" and b"\r\n" in data and object_id(data.replace(b"\r\n", b"\n"), len(oid)) == oid
                found["endings" if same else "changed"].append(path)
    finally:
        walk.close()
    seen = dict(unread=unread, sparse=sparse, skipped=skipped, compared=compared, entries=len(plain),
                whole=not late and not more, touched=None if since is None else bool(every and touched_all))
    for kind, names in found.items():
        seen[kind], seen[kind + "_count"] = sorted(names)[:UNSTAGED_SHOWN], len(names)
    return seen


PARTED = "\x01"  # stands where something that is no text stood in a line of a message, while it is read
# Characters that count as printable and that a reader sees as nothing or as a blank: they part a line as
# what is no text does. A list of those known here, not of all there are.
UNSEEN = re.compile("[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180d\u180f\u2800\u3164\ufe00-\ufe0f\uffa0"
                    "\U000e0100-\U000e01ef]")


def message_of(raw):
    """The fact `message` of `candidate_look` from the bytes of a commit object, or None
    when they are not one. The headers end at the first empty line. A line that begins
    with a space continues the header before it and is never read as one, so nothing
    inside a signature or a `mergetag` passes for a header of the commit."""
    if not raw.startswith(b"tree "):
        return None
    head, _, body = raw.partition(b"\n\n")
    signed, encoding = False, None
    for line in head.split(b"\n"):
        if line.startswith(b" "):
            continue
        key, _, value = line.partition(b" ")
        if key in (b"gpgsig", b"gpgsig-sha256"):
            signed = True
        elif key == b"encoding":
            value = value.decode("ascii", "replace")
            # One header that is not a plain token, or a second header, and none is shown.
            encoding = value if encoding is None and ENCODING.fullmatch(value) else ""
    lines = (body[:-1] if body.endswith(b"\n") else body).split(b"\n") if body else []
    shown = lines if len(lines) <= MESSAGE_LINES else lines[:MESSAGE_HEAD] + lines[-MESSAGE_TAIL:]
    rows, masked, cut, work = [], 0, 0, 0
    for line in shown:
        # In the order of `cli_words.last`, for its reason: nothing is escaped or cut before the masks
        # have run on the whole line. What is no text (a control sequence, a byte that is no UTF-8, a
        # character that is not printable, a tab among them, or one of UNSEEN) may part a credential in two or stand
        # where a space would, and a mask reads a word joined to a token as no credential. One line
        # may hold both: a tab on each side of a scheme word and a control sequence inside the token
        # (the merge gate's case). So the line is read in every way, each such place closed or a
        # space, and where any reading has something to leave out the whole line is left out: no
        # reading says where in the others the thing stands. The readings double with each place, so
        # a line parted in more than MESSAGE_PARTS places is left out unread, and so is one whose
        # readings would take the message past MESSAGE_WORK. What stands at the very beginning or
        # the very end of a line parts nothing: the line between is masked as any line is, and its
        # edges are shown as they are.
        decoded = line.decode("utf-8", "replace")
        text = cli_words.CONTROL.sub("", decoded).replace(cli_words.NO_TEXT, "")
        apart = UNSEEN.sub(PARTED, cli_words.CONTROL.sub(PARTED, decoded).replace(cli_words.NO_TEXT, PARTED))
        parts = [part for part in re.split(PARTED + "+", "".join(c if c.isprintable() else PARTED for c in apart))
                 if part] or [""]
        places = len(parts) - 1
        if not places:
            hidden = cli_words.masked(parts[0])
            if hidden != parts[0]:
                # `text` is the one part with, before and after it, what is shown and is no text.
                edge = next(at for at, c in enumerate(text) if c.isprintable() and not UNSEEN.match(c))
                text, masked = text[:edge] + hidden + text[edge + len(parts[0]):], masked + 1
        else:
            cost = (sum(map(len, parts)) + places) << min(places, MESSAGE_PARTS)
            if places > MESSAGE_PARTS or work + cost > MESSAGE_WORK:
                text, masked = cli_words.LEFT_OUT, masked + 1
            else:
                work += cost
                if any(cli_words.masked(view) != view for view in (
                        parts[0] + "".join(gap + part for gap, part in zip(gaps, parts[1:]))
                        for gaps in itertools.product(("", " "), repeat=places))):
                    text, masked = cli_words.LEFT_OUT, masked + 1
        pieces = ["\\\\" if c == "\\" else c if " " <= c <= "~" else
                  "\\x%02x" % ord(c) if ord(c) < 0x100 else "\\u%04x" % ord(c) if ord(c) < 0x10000 else
                  "\\U%08x" % ord(c) for c in text]
        if sum(map(len, pieces)) > MESSAGE_LINE:
            kept, size = [], len(cli_words.CUT)
            for piece in pieces:  # never inside an escape
                if size + len(piece) > MESSAGE_LINE:
                    break
                kept.append(piece)
                size += len(piece)
            pieces, cut = kept + [cli_words.CUT], cut + 1
        rows.append("".join(pieces))
    return dict(bytes=len(body), lines=len(lines), high=sum(1 for byte in body if byte >= 0x80),
                control=sum(1 for byte in body if byte == 0x7f or (byte < 0x20 and byte not in (0x09, 0x0a))),
                encoding=encoding, subject=len(lines[0].decode("utf-8", "replace")) if lines else 0, rows=rows,
                omitted=len(lines) - len(shown), masked=masked, cut=cut, signed=signed)


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
        named = remotes(lambda *args: git(bound, *args, common=shared))
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
    if named is not None:
        # Always when Git answered, also with none: a look without the key did not read them.
        seen["remotes"] = [dict(name=name[:NAME_SHOWN], sha256=digest(named[name])) for name in sorted(named)[:REMOTES]]
        if len(named) > REMOTES:
            seen["remotes_truncated"] = True
    return seen


def remotes(git):
    """The remotes of the repository's own configuration: `{name: [(key, value), ...]}`
    with key `url` or `pushurl`, in Git's order; None when Git did not answer in the
    time left. A remote is every section `remote "NAME"` that sets anything, also one
    with a fetch rule and no URL, whose list is then empty: Git lists such a remote and
    pushes to it under its name. The values of its other keys are not kept, and a key of
    the section `remote` itself (`remote.pushDefault`, with one dot) names no remote. A
    section with an empty name (`remote..url`, with two) is not that: Git takes each of
    its keys for a remote named by a dot and the key, `.url`, and so does this. Advisory
    and last in `observe`, as `worktrees` is. Neither the user's nor the machine's
    configuration is read, nor a file the repository's configuration includes: a
    builder can write an include, and what it points at is not the repository's."""
    try:
        out, code, cut = git("config", "--local", "--no-includes", "--null", "--get-regexp", REMOTE_KEY)
    except (OSError, ValueError, TimeoutError):
        return None
    if cut or code not in (0, 1) or (code == 1 and out):
        return None  # 1 with no output is Git's answer when no key matches
    found = {}
    for entry in entries(out, False):
        key, _, value = entry.partition(b"\n")
        key = os.fsdecode(key)
        name, dot, kind = key[len("remote."):].rpartition(".")
        if not key.startswith("remote.") or not kind:
            return None  # not what was asked for: the output was not read as it was meant
        if not dot:
            continue  # `remote.KEY`, a setting for all remotes
        if not name:
            name = "." + kind  # `remote..KEY`: the name Git itself gives the remote of that key
        urls = found.setdefault(name, [])
        if kind in ("url", "pushurl"):
            urls.append((kind, os.fsdecode(value)))
    return found


def digest(values):
    """One SHA-256 for a remote's configured URLs, so that a look can say that they
    changed without keeping them: its `url` values and then its `pushurl` values,
    each with its key, so that a URL moved from one key to the other is a change.
    It is for comparing and protects nothing: a URL that holds a short secret can be
    found from it by trying, so a look is kept where the shift's other records are."""
    mark = hashlib.sha256()
    for kind in ("url", "pushurl"):
        for key, value in values:
            if key == kind:
                mark.update(kind.encode() + b"\0" + os.fsencode(value) + b"\0")
    return mark.hexdigest()


def url_shown(value):
    """The scheme and the host of a remote's URL, or None when it is not shown.

    Only for `http`, `https`, `ssh` and `git` URLs and for the form `host:path` (a
    colon before any slash, and no `://`), which is given as `ssh://host`. A user
    and a password, the path, a query and a fragment are never given: a path can
    hold a token, and no mask by shape can promise to find one. A path on this
    machine, a `file://` URL, a remote helper (`name::address`) and a host that is
    not made of letters, digits, dots and hyphens are not shown at all; nor is what
    stands before a single colon when it is the name of a scheme (`file:/path`) or
    one letter (a drive)."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    head, found, rest = value.partition("://")
    if found:
        scheme = head.lower()
        if scheme not in SCHEMES:
            return None  # also `helper::https`, whose scheme is not one of them
        place = re.split(r"[/?#]", rest, maxsplit=1)[0].rpartition("@")[2]
        host, colon, port = place.partition(":")
        if not HOST.fullmatch(host) or (colon and not PORT.fullmatch(port)):
            return None
        return scheme + "://" + host + colon + port
    host, colon, path = value.partition(":")
    if not colon or "/" in host or path.startswith(":") or "@" in path:
        return None  # a path, a remote helper, or a colon inside what stands before the host
    host = host.rpartition("@")[2]
    if len(host) < 2 or host.lower() in NO_HOST:
        return None
    return "ssh://" + host if HOST.fullmatch(host) else None


def candidate_look(repo, base, commit, seconds, room=None, start=None, clock=time.monotonic, since=None, window=False):
    """What the host itself sees of the repository when it prepares the review of the
    candidate `commit` of a task that began at `base` (None: the repository had no
    commit). Read only. `{at, branch, commits, remotes, uncommitted, tracking, message,
    unstaged}`: `at` is the time of the look, and each of FACTS is the fact or
    `{left_out: reason}`.
    Never raises for what Git says; an id that is not a full commit id is refused
    before anything is run.

    `seconds` is one deadline for all of it, never more than LOOK_SECONDS. `room`,
    when given, is the caller's own question for the seconds that may still be
    used: it is asked once before each command, which gets no more than it says. A
    command that gave more than is read of one (CUT) or that the time ended (LATE)
    leaves its fact out and is never read as none, and once the time has ended no
    further command is started.
    `start` is the look taken when the shift began. Where it gives the Git
    directories, a repository that now resolves to others is not read. Where it
    gives the remotes and the untracked files, those of now are compared with it.
    `since` is when the shift began, in seconds since the epoch, and `window` says
    that it is the signed start of the shift's window in its place. It decides only
    which tracked files are compared first.

    - `branch`: `{name, ref, head, at_candidate, holds_candidate}`. `ref` is what
      HEAD names (None: detached) and `name` the branch where that is one;
      `holds_candidate` says, when HEAD is at another commit, whether the
      candidate is an ancestor of it.
    - `commits`: `{based, count, more, rows}`. `based`: whether `base` is an
      ancestor of the candidate (None without a base). `count` commits are
      reachable from the candidate and not from the base, counted to COMMITS
      (`more`: there are more). `rows` are the newest COMMITS_SHOWN, newest first,
      each `{id, merge}`.
    - `remotes`: `{rows, removed, more, compared, unlisted}`. `rows`, at most
      REMOTES, each `{name, urls, urls_more, since}`: `urls` is `[{key, shown}]`
      with `shown` as `url_shown` gives it, at most URLS of each key, and
      `urls_more` how many it has beyond those. `since` is `added`, `changed` or
      `there` against the start, None when that cannot be said: `compared` is
      false, or the start look kept a name cut at NAME_SHOWN that this name begins
      with. `more` remotes are not in `rows`, and `unlisted` counts those of them
      that were added, that changed and of which it cannot be said.
    - `uncommitted`: `{untracked, count, more, staged, compared}`. `untracked` are
      the first UNTRACKED_SHOWN names that are untracked and not ignored, each
      `{path, before}` (`before`: the start look listed it; None when it cannot
      say, as for a path the start look may have kept cut at PATH_SHOWN); `count`
      of them all, to UNTRACKED; `staged` paths differ between the index and HEAD,
      or with no commit are all the index holds.
    - `tracking`: `{any, on, more}`: whether a ref exists under `refs/remotes/`,
      and those that hold the candidate, at most TRACKING, without that prefix.
    - `message`: the candidate's commit message, from one `git cat-file commit` of
      at most MESSAGE_OBJECT bytes (a larger object is CUT and not read) that the
      host parses itself (`message_of`): `{bytes, lines, high, control, encoding,
      subject, rows, omitted, masked, cut, signed}`. `bytes` and `lines` of the
      message; `high` bytes of 0x80 and above and `control` bytes below 0x20 other
      than tab and line feed, or 0x7f, both counted raw; `subject` the characters of
      the first line as decoded. `encoding` is the header's value when it is a
      plain token, "" when one is there that is not shown, else None. `signed`: a
      signature header is there, which nobody checked. `rows` are the lines shown,
      all of them, or of more than MESSAGE_LINES the first MESSAGE_HEAD and the
      last MESSAGE_TAIL with `omitted` between. Each row is printable ASCII: what
      has the shape of a credential is left out by `cli_words.masked` on the whole
      line, and the whole line where something that is no text (or a character
      of UNSEEN) parts it between its first and its last text and any
      reading of it, each such place closed or a space, holds such a thing, or
      where it is parted in more than MESSAGE_PARTS places or its readings would
      pass MESSAGE_WORK for the message (`masked` rows), then every
      other character is written as an escape and a backslash doubled, then the row
      is cut at MESSAGE_LINE (`cut` rows). The words are the builder's.
    - `unstaged`: `{changed, changed_count, deleted, deleted_count, endings,
      endings_count, unread, sparse, skipped, compared, entries, whole, touched,
      since, converts}`, see `unstaged_edits`: the names of each kind to
      UNSTAGED_SHOWN and their counts. `since` is `shift`, `window` or None, the
      time the order was taken from. `converts`: whether the repository's own
      configuration sets a filter, `core.eol` or `core.autocrlf` (None: the read
      failed), since the host compares bytes and converts nothing. Looked at last:
      it is the one fact that reads files, and gives way when the time is short.
    """
    for value, needed in ((base, False), (commit, True)):
        if (needed or value is not None) and not (
                isinstance(value, str) and len(value) in (40, 64) and set(value) <= OBJECT):
            raise ValueError("a look for a review is between full commit IDs")
    if isinstance(since, bool) or not isinstance(since, (int, float)) or since != since:
        since = None  # no time to order by: the index's order
    end = clock() + max(0, min(seconds, LOOK_SECONDS))
    seen = dict(at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    root = os.path.realpath(repo)
    place = {}

    class Late(Exception):
        pass

    def remaining():
        """The seconds that may still be used: of the look's own time and of the caller's."""
        left = end - clock()
        if left > 0 and room is not None:
            left = min(left, room())
        return left

    def git(*args, pinned=True, fine=(0,), limit=None):
        """The output of one command. A status outside `fine` is no answer."""
        left = remaining()
        if left <= 0:
            raise Late()
        base_args = (("--git-dir=" + place["git"], "--work-tree=" + root) if pinned else ("-C", root))
        argv = ["git", "--no-optional-locks", "--no-replace-objects", "--no-pager", *base_args,
                "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, *args]
        # A bound of its own only where one is given, so that a `run` that knows none is called as before.
        out, code, cut = (run(argv, root, left, place.get("common")) if limit is None
                          else run(argv, root, left, place.get("common"), limit))
        if cut:
            # More than is read of one command, or the time: `run` ends a command for either.
            if len(out) >= (OUTPUT if limit is None else limit):
                raise ValueError(CUT)
            raise Late()
        if code not in fine:
            raise ValueError(SILENT)
        return out, code

    def lines(out):
        return [line for line in out.decode("utf-8", "surrogateescape").split("\n") if line]

    def ancestor(first, second):
        return git("merge-base", "--is-ancestor", first, second, fine=(0, 1))[1] == 0

    def at():
        """The commit HEAD is at, None when the repository has none. Asked once: an answer
        is kept, and a question that got none is asked again by the next fact that needs it."""
        if "head" not in place:
            out, code = git("rev-parse", "--verify", "-q", "HEAD^{commit}", fine=(0, 1))
            head = out.decode("ascii", "replace").strip() or None
            if (code == 0) != bool(head) or (head is not None and not (len(head) in (40, 64) and set(head) <= OBJECT)):
                raise ValueError(SILENT)
            place["head"] = head
        return place["head"]

    def branch():
        head = at()
        out, code = git("symbolic-ref", "-q", "HEAD", fine=(0, 1))
        ref = os.fsdecode(out).rstrip("\n") if code == 0 else None
        if code == 0 and not ref:
            raise ValueError(SILENT)
        name = ref[len("refs/heads/"):] if ref and ref.startswith("refs/heads/") else None
        holds = None if head == commit or head is None else ancestor(commit, head)
        return dict(name=name, ref=ref, head=head, at_candidate=head == commit, holds_candidate=holds)

    def commits():
        based = None if base is None else ancestor(base, commit)
        out, _ = git("rev-list", "--parents", "--max-count=" + str(COMMITS + 1),
                     *(["^" + base] if base is not None else []), commit, "--")
        rows = [line.split() for line in lines(out)]
        if any(not row or any(not set(part) <= OBJECT for part in row) for row in rows):
            raise ValueError(SILENT)
        return dict(based=based, count=min(len(rows), COMMITS), more=len(rows) > COMMITS,
                    rows=[dict(id=row[0], merge=len(row) > 2) for row in rows[:min(COMMITS_SHOWN, COMMITS)]])

    def remote():
        why = []

        def asked(*args):
            try:
                return git(*args, fine=(0, 1)) + (False,)
            except ValueError as exc:
                why.append(exc)  # `remotes` answers None for it: the reason is kept here
                raise
        found = remotes(asked)
        if found is None:
            raise why[0] if why else ValueError(SILENT)
        before = start.get("remotes") if isinstance(start, dict) else None
        # Compared only with a start look that read all the remotes and wrote rows as `observe`
        # writes them. A name it kept at the full NAME_SHOWN may have been cut there: such a
        # row says nothing of one name, and is kept apart from those that are whole.
        compared = (isinstance(before, list) and start.get("remotes_truncated") is not True
                    and all(isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]
                            and len(row["name"]) <= NAME_SHOWN and isinstance(row.get("sha256"), str) for row in before)
                    and len({row["name"] for row in before}) == len(before))
        known = {row["name"]: row["sha256"] for row in before if len(row["name"]) < NAME_SHOWN} if compared else {}
        cut = {row["name"] for row in before if len(row["name"]) == NAME_SHOWN} if compared else set()

        def since(name):
            if not compared or (len(name) >= NAME_SHOWN and name[:NAME_SHOWN] in cut):
                return None
            if name not in known:
                return "added"  # also a long name where the start look kept none that it begins with
            return "there" if known[name] == digest(found[name]) else "changed"
        rows, unlisted = [], dict.fromkeys(UNLISTED, 0)
        for number, name in enumerate(sorted(found)):
            if number >= REMOTES:
                if compared and since(name) != "there":
                    unlisted[since(name) or "unsure"] += 1
                continue
            urls = {kind: [dict(key=key, shown=url_shown(value)) for key, value in found[name] if key == kind]
                    for kind in ("url", "pushurl")}
            rows.append(dict(name=name, urls=urls["url"][:URLS] + urls["pushurl"][:URLS],
                             urls_more=sum(max(0, len(urls[kind]) - URLS) for kind in urls), since=since(name)))
        gone = sorted((set(known) - set(found)) | {name for name in cut
                                                   if not any(now[:NAME_SHOWN] == name for now in found)})
        return dict(rows=rows, removed=gone, more=max(0, len(found) - REMOTES), compared=bool(compared),
                    unlisted=unlisted)

    def uncommitted():
        out, _ = git("ls-files", "-z", "--others", "--exclude-standard")
        names = sorted(os.fsdecode(name) for name in entries(out, False))
        head = at()
        staged, _ = (git("diff-index", "-z", "--cached", "--name-only", "--no-ext-diff", "--no-textconv", head)
                     if head else git("ls-files", "-z", "--cached"))
        compared = usable(start) and start.get("truncated") is False
        before = {row["path"] for row in start["changes"] if row["state"] == "untracked"} if compared else set()

        def there(name):
            """Whether the start look listed it. That look keeps a path cut at PATH_SHOWN, so
            of a path that long it says only whether one with the same beginning was there."""
            if not compared:
                return None
            if len(name) < PATH_SHOWN:
                return name in before
            return None if name[:PATH_SHOWN] in before else False
        return dict(untracked=[dict(path=name, before=there(name)) for name in names[:UNTRACKED_SHOWN]],
                    count=min(len(names), UNTRACKED), more=len(names) > UNTRACKED,
                    staged=len(entries(staged, False)), compared=bool(compared))

    def tracking():
        out, _ = git("for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes/")
        if not lines(out):
            return dict(any=False, on=[], more=False)
        out, _ = git("for-each-ref", "--count=" + str(TRACKING + 1), "--format=%(refname)", "--contains", commit,
                     "refs/remotes/")
        names = [name[len("refs/remotes/"):] for name in lines(out) if name.startswith("refs/remotes/")]
        return dict(any=True, on=names[:TRACKING], more=len(names) > TRACKING)

    def message():
        # One command and no size asked first: nothing can change between a check and a read.
        out, _ = git("cat-file", "commit", commit, limit=MESSAGE_OBJECT)
        found = message_of(out)
        if found is None:
            raise ValueError(SILENT)
        return found

    def unstaged():
        out, _ = git("ls-files", "-z", "-v", "--stage")
        listing = entries(out, False)
        try:
            out, code = git("config", "--local", "--no-includes", "--null", "--get-regexp", CONVERTS, fine=(0, 1))
            # A key with no value is listed without the line feed and is true to Git.
            pairs = [entry.partition(b"\n") for entry in entries(out, False)]
            if (code == 1 and out) or any(key not in (b"core.autocrlf", b"core.eol") and not key.startswith(b"filter.")
                                          for key, _, _ in pairs):
                converts = None  # not what was asked for: the output was not read as it was meant
            else:
                converts = any(key != b"core.autocrlf" or not found or value.strip().lower() not in NOT_SET
                               for key, found, value in pairs)
        except (OSError, ValueError):
            converts = None  # not read: the line says that the host could not tell
        seen = unstaged_edits(root, listing, since, remaining)
        if seen is None:
            return dict(left_out=UNKNOWN)  # returned: raised, it would be written as Git not answering
        return dict(seen, since=None if since is None else "window" if window else "shift", converts=converts)

    # Where Git keeps this working tree's data, found from the root and not from
    # configuration, and held to what the shift's first look found.
    why = None
    try:
        if not os.path.lexists(os.path.join(root, ".git")):
            raise ValueError(SILENT)
        out, _ = git("rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir", pinned=False)
        places = out.split(b"\n")
        if len(places) != 3 or not places[0] or not places[1] or places[2]:
            raise ValueError(SILENT)
        place.update(git=os.fsdecode(places[0]), common=os.path.realpath(os.fsdecode(places[1])))
        held_to = [start.get(name) for name in ("git_dir", "common_dir")] if isinstance(start, dict) else [None, None]
        if (held_to[0] is not None and held_to[0] != place["git"]) or (
                held_to[1] is not None and held_to[1] != place["common"]):
            why = MOVED
    except Late:
        why = LATE
    except (OSError, ValueError) as exc:
        why = str(exc) if str(exc) in (CUT, SILENT) else SILENT
    for name, fact in zip(FACTS, (branch, commits, remote, uncommitted, tracking, message, unstaged)):
        if why is not None:
            seen[name] = dict(left_out=why)
            continue
        try:
            seen[name] = fact()
        except Late:
            seen[name] = dict(left_out=LATE)
            why = LATE  # for what is still to come as well
        except (OSError, ValueError) as exc:
            seen[name] = dict(left_out=str(exc) if str(exc) in (CUT, SILENT) else SILENT)
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
