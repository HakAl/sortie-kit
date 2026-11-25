"""One directory per shift for the temporary files of builder turns and reviews.

It is short and outside the repository: a project's tests may bind Unix sockets
under the temporary directory (a path of 103 bytes at most) or need to run
outside any Git checkout, and the shift's own `scratch/` is neither. The host
makes it when its loop starts and removes it at its end. Nothing here is
signed, and nothing is read back out of the shift, which a builder can write.
"""
import fcntl
import os
import secrets
import stat
import sys
import threading
import time

NAMED = ("TMPDIR", "TEMP", "TMP")  # read, never probed: looking for a base writes nothing anywhere
# Tried in this order, each by its real path; after them what the environment names.
BASES = ("/tmp",)
# With the eight random characters of the name, the longest path is 28 bytes on macOS, where the
# real path is /private/tmp/sortie-XXXXXXXX, and 20 on Linux, /tmp/sortie-XXXXXXXX.
PREFIX = "sortie-"  # then eight random characters
REMOVE_SECONDS = 1.0  # the most one removal is given
DEPTH = 64  # directories below one another that a removal goes into; each holds one descriptor
NONE = "no directory outside the repository and outside any Git checkout could be used"


class Late(Exception):
    """The time for the removal ran out."""


class Replaced(Exception):
    """The name in the base is no longer the directory the host made, or was not when it was removed."""


def candidates(bases, environ=None):
    """The bases to try: the short location, then the directories the host's
    environment names as temporary. They are only read out of the environment.
    Python's own search for a temporary directory is not asked, because it finds
    one by writing a file into each place it tries, the current directory among
    them, and this runs before anything is signed."""
    if bases is not None:
        return list(bases)
    environ = os.environ if environ is None else environ
    return list(BASES) + [environ[name] for name in NAMED if environ.get(name)]


def checkout(path, ceiling=None):
    """The nearest directory at or above `path` that holds a `.git` entry, or None.
    Directories at or above `ceiling` are not looked at."""
    ceiling = None if ceiling is None else os.path.realpath(ceiling)
    while path != ceiling:
        if os.path.lexists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return None


def guarded(info, own=False):
    """Whether nobody but this user can rename or remove what this user makes in the
    directory: it is root's and sticky, as the system's own temporary directories
    are. Mode bits are not the whole of it where a directory can carry an access
    list that lets another user rename what is in it, and this module cannot read
    such a list. Only root can put one on a directory of root's, so only root's
    directories are taken. `own` also takes a directory of this user's that no
    one else can write, or that is sticky: for a caller that vouches for its own
    directories, which the host of a shift never does."""
    if info.st_mode & stat.S_ISVTX and info.st_uid == 0:
        return True
    mine = own and info.st_uid == os.getuid()
    return bool(mine and (not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX))


def anchored(real, ceiling=None, own=False):
    """Whether no other user can make the path `real` mean another place: every
    directory above it is root's (with `own`, or this user's) and is either
    writable by its owner alone or sticky. Nothing is followed: `real` is a real
    path. Directories at or above `ceiling` are not looked at."""
    ceiling = None if ceiling is None else os.path.realpath(ceiling)
    owners = (0, os.getuid()) if own else (0,)
    path = os.path.dirname(real)
    while path != ceiling:
        try:
            info = os.lstat(path)
        except OSError:
            return False
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            return False
        parent = os.path.dirname(path)
        if parent == path:
            return True
        path = parent
    return True


def usable(repo, bases=None, ceiling=None, own=False):
    """The real paths of the bases a directory may be made in, in order: each a
    directory this user can write and in which no other user can touch what it
    makes, reached by a path no other user can turn elsewhere, not the
    repository or under it, and in no Git checkout. Reads only."""
    repo, found = os.path.realpath(repo), []
    for item in candidates(bases):
        try:
            real = os.path.realpath(item, strict=True)
            info = os.stat(real)
        except (OSError, ValueError):
            continue
        if (real in found or not os.path.isabs(real) or not stat.S_ISDIR(info.st_mode) or not guarded(info, own)
                or not anchored(real, ceiling, own) or not os.access(real, os.W_OK | os.X_OK)
                or real == repo or real.startswith(repo.rstrip(os.sep) + os.sep) or checkout(real, ceiling)):
            continue
        found.append(real)
    return found


def base(repo, bases=None, ceiling=None, own=False):
    """Where `make` would make the directory now, or None."""
    return next(iter(usable(repo, bases, ceiling, own)), None)


def words(repo, bases=None, ceiling=None, own=False):
    """One line for the signer. Not part of what is signed: the directory does not exist yet."""
    where = base(repo, bases, ceiling, own)
    if where is None:
        return ("Temporary files: " + NONE + " here, so builder turns and reviews keep theirs in the shift's "
                "scratch/, inside the repository. Tests that bind Unix sockets under the temporary directory, or "
                "that must run outside a Git checkout, can fail for that alone.")
    return ("Temporary files: builder turns and reviews get a directory the host makes when the shift starts, "
            "outside the repository and open to your user alone. As things are now that would be under " + where
            + "; the place is looked at again then and may be another, or the shift's scratch/ when none will do. "
            "At the end the host tries to remove it with whatever is in it; the report names it and says what "
            "became of it.")


class TurnTemp:
    """The directory, or the fact that none was made, and what became of it."""

    def __init__(self, path, fd, why=None, parent=None):
        # `fd` is the directory itself and `parent` the base it was made in, both held
        # open from the making on: the removal goes through them and never by the path.
        self.path, self.fd, self.why, self.parent = path, fd, why, parent
        info = os.fstat(fd) if fd is not None else None
        self.same = None if info is None else (info.st_dev, info.st_ino)
        self.state = "not made" if path is None else "in use"
        self.entries = self.gone = None
        self.replaced = False  # the path was seen to name something other than what the host made
        self.unverified = False  # removed by name, with no way on this system to see that it was the host's own

    def record(self):
        """For the closeout. `entries` is what lay directly in the directory before the
        removal, None when that was never counted; `removed_entries` how many of them went."""
        if self.path is None:
            return dict(path=None, state=self.state, why=self.why)
        out = dict(path=self.path, bytes=len(os.fsencode(self.path)), state=self.state)
        if self.state in ("removed", "left"):
            out.update(entries=self.entries, removed_entries=self.gone)
        if self.state == "removed" and self.unverified:
            out["unverified"] = True
        if self.state == "left":
            out["why"] = self.why
            if self.replaced:
                out["replaced"] = True
        return out

    def close(self):
        for name in ("fd", "parent"):
            if getattr(self, name) is not None:
                try:
                    os.close(getattr(self, name))
                except OSError:
                    pass
                setattr(self, name, None)

    def left(self, why):
        self.state, self.why = "left", why[:300]
        self.close()

    def remove(self, seconds, *, running=None, clock=time.monotonic):
        """Remove the directory with what is in it, in at most `seconds`. Never raises:
        whatever keeps it from going is in the record. `running` says why nothing is
        touched (a process that may still write there).

        The work is done beside this call, which waits for it no longer than its
        time: one call into a slow file system cannot be cut short, and the
        closeout and the report must not wait on it. Work that was not seen to
        finish is told to stop at its next step and keeps the handles until then."""
        if self.path is None or self.state != "in use" or self.fd is None:
            return
        self.gone = 0
        if running:
            return self.left(running)
        if seconds <= 0:
            return self.left("no time was left for the removal")
        limit = min(seconds, REMOVE_SECONDS)
        until = time.monotonic() + limit  # the wait below ends here, whatever starting the work took
        work = Removal(self.path, self.fd, self.parent, self.same, clock() + limit, clock)
        try:
            thread = threading.Thread(target=work.run, name="sortie-temp-removal", daemon=True)
            thread.start()
        except RuntimeError as exc:
            return self.left("it could not be removed: " + (str(exc) or type(exc).__name__))
        thread.join(max(0.0, until - time.monotonic()))
        with work.lock:
            if not work.done:
                # From here on the handles are the work's own, which closes them when it ends.
                work.abandoned = True
                work.stop.set()
                self.fd = self.parent = None
            abandoned = work.abandoned
        if abandoned:
            # Nothing is read from work that still runs: what it counted or took is not known here.
            self.gone = None
            return self.left("the removal was not seen to finish in its time and was told to stop; "
                             "what is still there was not looked at")
        self.entries, self.gone, self.replaced = work.entries, work.gone, work.replaced
        self.unverified = work.unverified
        if work.why is not None:
            return self.left(work.why)
        self.state = "removed"
        self.close()


class Removal:
    """One removal, run beside the host's own thread. It writes only to itself."""

    def __init__(self, path, fd, parent, same, end, clock):
        self.path, self.fd, self.parent, self.same, self.end = path, fd, parent, same, end
        self.stop, self.lock = threading.Event(), threading.Lock()
        # Once it is told to stop, every look at the time says the time is over.
        self.clock = lambda: float("inf") if self.stop.is_set() else clock()
        self.entries, self.gone, self.why, self.replaced = None, 0, None, False
        self.unverified = False  # the name was removed and this system gave no way to see what went
        self.done = self.abandoned = False

    def run(self):
        try:
            self.why = self.work()
        except BaseException as exc:  # nothing leaves this thread unsaid
            self.why = "it could not be removed: " + (str(exc) or type(exc).__name__)
        with self.lock:
            self.done = True
            if self.abandoned:
                for fd in (self.fd, self.parent):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def work(self):
        """None when the directory is gone, or why it is not."""
        end, clock = self.end, self.clock
        try:
            if clock() >= end:
                raise Late()
            # Through the handle the host has held since it made the directory: whatever
            # the name points at by now, only what is in that directory is taken.
            # A builder may have taken its own directory's permissions away; it is the host's.
            if stat.S_IMODE(os.fstat(self.fd).st_mode) & 0o700 != 0o700:
                timely(end, clock)
                os.fchmod(self.fd, 0o700)
            names = listed(self.fd, end, clock)
            self.entries = len(names)
            for name in names:
                clear(self.fd, name, end, clock, self.same[0], DEPTH)
                self.gone += 1
            if clock() >= end:
                raise Late()
            # The name is taken out of the base the host has held open, and only while it
            # still is the directory the host made.
            leaf = os.path.basename(self.path)
            try:
                info = os.lstat(leaf, dir_fd=self.parent)
            except FileNotFoundError:
                raise Replaced("the path no longer names anything; what the host made was emptied and is "
                               "somewhere else or gone") from None
            if (info.st_dev, info.st_ino) != self.same or not stat.S_ISDIR(info.st_mode):
                raise Replaced("the path is no longer the directory the host made; what the host made was emptied")
            timely(end, clock)  # after the look, which may have been slow: no change once told to stop
            try:
                os.rmdir(leaf, dir_fd=self.parent)
            except OSError:
                # The host's own directory was emptied, so a name that cannot be removed
                # may have come to mean something else since the look above.
                try:
                    now = os.lstat(leaf, dir_fd=self.parent)
                except FileNotFoundError:
                    now = None
                if now is None or (now.st_dev, now.st_ino) != self.same or not stat.S_ISDIR(now.st_mode):
                    raise Replaced("the path stopped naming the directory the host made as it was removed; "
                                   "what the host made was emptied") from None
                raise
            # Between that look and the removal the name can have come to mean another
            # empty directory, and no call removes a directory by its identity. So it is
            # asked afterwards, by reading only, whether the host's own directory is gone.
            gone = vanished(self.fd, self.same)
            if gone is False:
                raise Replaced("an empty directory that had taken its name was removed in its place; what the "
                               "host made was emptied and is no longer at that path")
            self.unverified = gone is None
        except Late:
            return "the time for the removal ran out"
        except Replaced as exc:
            self.replaced = True
            return str(exc)
        except (OSError, RecursionError) as exc:
            return "it could not be removed: " + (str(exc) or type(exc).__name__)
        return None


def timely(end, clock):
    """Late when the time is over, which it always is for work that was told to
    stop. Asked right before every change, since the call before it may have
    come back long after the host stopped waiting."""
    if clock() >= end:
        raise Late()


def vanished(fd, same):
    """Whether the directory open as `fd` has been removed: True, False, or None
    when this system gives no way to tell. Reads only. Linux counts no link to a
    removed directory. macOS keeps counting them, and gives the path a handle is
    at, the last one it had for a directory that is gone: the directory exists
    when that path still names it."""
    try:
        if os.fstat(fd).st_nlink == 0:
            return True
        if sys.platform.startswith("linux"):
            return False
        flag = getattr(fcntl, "F_GETPATH", None)
        if flag is None:
            return None
        path = os.fsdecode(fcntl.fcntl(fd, flag, bytes(1024)).split(b"\0", 1)[0])
        if not os.path.isabs(path):
            return None
        try:
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return True
        return (info.st_dev, info.st_ino) != same
    except (OSError, ValueError):
        return None


def listed(fd, end, clock):
    """The names in the open directory `fd`, or Late when listing them takes too long."""
    names = []
    # Entry by entry, so that a directory with very many entries cannot hold the
    # removal past its time: the whole listing is never waited on at once.
    with os.scandir(fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) % 256 == 0 and clock() >= end:
                raise Late()
    if clock() >= end:
        raise Late()
    return names


def clear(parent, name, end, clock, device, depth):
    """Remove one entry of the open directory `parent`. A link is removed, never
    followed; a directory on another file system, or nested deeper than the
    bound, is not gone into and stops the removal."""
    if clock() >= end:
        raise Late()
    try:
        if not stat.S_ISDIR(os.lstat(name, dir_fd=parent).st_mode):
            timely(end, clock)
            os.unlink(name, dir_fd=parent)
            return
    except FileNotFoundError:
        return  # gone by itself since it was listed: that is what was wanted
    if depth <= 0:
        raise OSError("directories nested deeper than " + str(DEPTH))
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if info.st_dev != device:
            raise OSError("a directory on another file system is mounted in it")
        # A directory its maker left read-only: made writable through its own handle.
        if stat.S_IMODE(info.st_mode) & 0o700 != 0o700:
            timely(end, clock)
            os.fchmod(fd, 0o700)
        for inner in listed(fd, end, clock):
            clear(fd, inner, end, clock, device, depth - 1)
    finally:
        os.close(fd)
    timely(end, clock)
    os.rmdir(name, dir_fd=parent)


def make(repo, bases=None, ceiling=None, own=False):
    """Make the shift's directory in the first base that takes it: mode 700, a name
    of eight random characters that nobody could prepare. Never raises; with no base the record says `not made`."""
    failed = []
    for where in usable(repo, bases, ceiling, own):
        parent = fd = leaf = None
        try:
            # Made, opened and, when it will not do, removed through the handle of the
            # base, never by a path that could come to mean another place.
            parent = os.open(where, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            # What was opened is what was checked, and it still passes every check.
            if (not guarded(os.fstat(parent), own) or not os.path.samestat(os.fstat(parent), os.stat(where))
                    or usable(repo, [where], ceiling, own) != [where]):
                raise OSError("the base changed while it was looked at")
            for _ in range(16):
                name = PREFIX + secrets.token_hex(4)
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError:
                    continue
                leaf = name
                break
            else:
                raise OSError("no unused name was found")
            fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or os.listdir(fd):
                raise OSError("what was made is not an empty directory of this user alone")
            # The path given to the seats and written down names the directory that is held.
            if not os.path.samestat(info, os.lstat(os.path.join(where, leaf))):
                raise OSError("the path does not name the directory that was made")
            return TurnTemp(os.path.join(where, leaf), fd, parent=parent)
        except OSError as exc:
            failed.append(where + ": " + (str(exc) or type(exc).__name__))
            if leaf is not None:
                # Taken out again only when the name is seen to be the directory that was
                # opened, and only when empty. Otherwise it stays and is said.
                try:
                    if fd is None or not os.path.samestat(os.fstat(fd), os.lstat(leaf, dir_fd=parent)):
                        raise OSError("not seen to be what was made")
                    os.rmdir(leaf, dir_fd=parent)
                except OSError:
                    failed[-1] += "; " + leaf + " was left there"
            if fd is not None:
                os.close(fd)
            if parent is not None:
                os.close(parent)
    return TurnTemp(None, None, (NONE + ("; " + "; ".join(failed) if failed else ""))[:300])
