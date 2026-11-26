"""`sortie setup`: the directory, the signing key and the trust file, once, for a person at a terminal.

The key is made by ssh-keygen, started with the terminal as its input and
output. Python never reads, passes, stores or makes a passphrase. Nothing that
is already there is replaced, changed or removed: the new key is made in a
directory of this run's own and given its name by a link, which fails when
something is at that name. Nothing is taken away again by its name either: a
name this run gave stays and is said. The directory that was checked is held
open from the check to the trust file, and the ssh-keygen that was found is
held to its bytes, so that what is reported ready is what was looked at.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import termios
import time
import uuid

from . import grants, signing, trust_init
from .layout import Refused

KEY, PUBLIC, TRUST = "signing-key", "signing-key.pub", "trust.json"
KEY_LIMIT = 1024 * 1024  # the bound grants.read_file puts on the key at signing
GRACE = 2.0  # seconds an interrupted ssh-keygen gets to end by itself, and again after it is told to


NOWHERE = Path("/dev/null/sortie-kit-no-repository")  # nothing can lie beneath it, and no link can stand for it


class NoPassphrase(Refused):
    pass


class Replaced(Refused):
    pass


def ident(info):
    return info.st_dev, info.st_ino


def named(parent, name, held):
    """Whether that name in the directory held open is, through no link, the file or
    directory that the descriptor `held` has open. A number alone would not say so: a file
    system may give the number of a removed file to the next one made, as Linux does at
    once. It cannot while the first is held open, so what is compared is held open."""
    try:
        return ident(os.lstat(name, dir_fd=parent)) == ident(os.fstat(held))
    except OSError:
        return False


def same(config, parent):
    """Whether the path still leads, through no link anywhere in it, to the directory held
    open. A start refuses a key or a trust file reached through a link, as `held` does."""
    try:
        if any(p.is_symlink() for p in (config, *config.parents)):
            return False
        return ident(os.stat(config, follow_symlinks=False)) == ident(os.fstat(parent))
    except OSError:
        return False


def replaced(config, done):
    return Replaced("the directory " + str(config) + " was replaced while this command ran: what is at that path now "
                    "is not the directory that was checked. " + done)


def changed(tool, pin):
    """Whether the file at the path is no longer the ssh-keygen that was found: other
    bytes, another file, none, or one that can no longer be run."""
    try:
        return grants.read_file(tool, trust_init.TOOL_LIMIT)[1] != pin or not os.access(tool, os.X_OK)
    except (OSError, ValueError):
        return True


def command(words, path):
    """A command to copy, when the path prints as it is. As `sortie trust init` holds it:
    an escaped path is not the path, and copying it would name another file."""
    text = str(path)
    if all(" " <= c <= "~" for c in text):
        return words + " " + shlex.quote(text)
    return (words + ", given the real path: this one holds characters that are shown here with escapes, so it is not "
            "offered as a ready command")


def drop(parent, name, stage):
    """Take away the run's own directory when it is empty and still at its name. Only an
    empty directory can go this way, so nothing of anyone's is lost by it."""
    try:
        if not os.listdir(stage) and named(parent, name, stage):
            os.rmdir(name, dir_fd=parent)
    except OSError:
        pass


def there(parent, name):
    """Whether anything is at that name in the directory held open, a link included."""
    try:
        os.lstat(name, dir_fd=parent)
        return True
    except FileNotFoundError:
        return False


def read_at(parent, name, limit, where, keep):
    """The bytes and the status of a regular file in the directory held open, through no
    link, and the descriptor it was read through. That stays open, in `keep`, for the
    caller to close when the name has been looked at for the last time."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError as exc:
        raise Refused(str(where) + " must be a regular file, not a link, that you can read (" + str(exc) + ")") from exc
    keep.append(fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise Refused(str(where) + " must be a regular file of ordinary size")
    raw = b""
    while len(raw) <= limit:
        more = os.read(fd, limit + 1 - len(raw))
        if not more:
            break
        raw += more
    if len(raw) > limit:
        raise Refused(str(where) + " must be a regular file of ordinary size")
    return raw, info, fd


def pair(parent, where, keep):
    """Read the key pair in the directory held open, each file once, and hold the key to
    what `sortie start` requires before it signs: yours alone, and by the check a start
    makes (`signing.encrypted_key`, which reads OpenSSH's unencrypted envelope and
    nothing inside it) encrypted with a passphrase and the key of that public key.
    Returns the public key as the trust file holds it, and the two files that were read,
    each by the descriptor it is held open with."""
    where = Path(where)
    key, public_path = where / KEY, where / PUBLIC
    public_raw, _, public_fd = read_at(parent, PUBLIC, trust_init.PUBLIC_LIMIT, public_path, keep)
    public = trust_init.parsed(public_raw, public_path)
    raw, info, key_fd = read_at(parent, KEY, KEY_LIMIT, key, keep)
    if info.st_uid != os.getuid():
        raise Refused("the key at " + str(key) + " belongs to another account, and sortie start does not sign with "
                      "such a key. It was left as it is")
    if info.st_mode & 0o077:
        raise Refused("the key at " + str(key) + " is not yours alone, and sortie start does not sign with such a key. "
                      "It was left as it is. To close it: " + command("chmod 600", key)
                      + ". Then run sortie setup again")
    try:
        signing.encrypted_key(raw, base64.b64decode(public.split()[1]))
    except Refused as exc:
        if "unencrypted" in str(exc):
            raise NoPassphrase("the key at " + str(key) + " has no passphrase, and sortie start does not sign with such "
                               "a key. It was left as it is and no trust file was written. Give it a passphrase with: "
                               + command("ssh-keygen -p -f", key) + ". Or remove it and its .pub file "
                               "yourself. Then run sortie setup again") from exc
        if "does not match" in str(exc):
            raise Refused("the key at " + str(key) + " does not match the public key beside it, so sortie start would "
                          "not sign with it. Both were left as they are") from exc
        raise Refused("the key at " + str(key) + " is not a passphrase-encrypted OpenSSH key (" + str(exc)
                      + "), so sortie start would not sign with it. It was left as it is") from exc
    return public, {KEY: key_fd, PUBLIC: public_fd}


def ready(parent, trust, public, keep):
    """Hold a trust file that is already there to what `sortie start` requires of it, by
    the check a start makes (`grants.Trust`), and to naming this key. It is not changed.
    Returns the descriptor it is held open with."""
    raw, info, held = read_at(parent, TRUST, trust_init.PUBLIC_LIMIT, trust, keep)  # a regular file, through no link
    left = " It was left as it is and nothing was changed"
    if info.st_uid != os.getuid():
        raise Refused("the trust file at " + str(trust) + " belongs to another account, and sortie start does not use "
                      "such a file." + left)
    if info.st_mode & 0o077:
        raise Refused("the trust file at " + str(trust) + " is not yours alone, and sortie start does not use such a "
                      "file." + left + ". To close it: " + command("chmod 600", trust))
    try:
        # No repository is at hand; a path that nothing can be at stands in, so that the check
        # of lying outside the target repository passes and every other check is the start's.
        checked = grants.Trust(trust, NOWHERE)
    except (OSError, ValueError) as exc:
        raise Refused("the trust file at " + str(trust) + " is not one sortie start accepts (" + str(exc) + ")."
                      + left) from exc
    # As the start reads it: the type and the bytes of the key, however the line is spaced.
    if (checked.public_key.split()[0], checked.key_blob) != (public.split()[0], base64.b64decode(public.split()[1])):
        raise Refused("the trust file at " + str(trust) + " names another key than the one beside it, so sortie start "
                      "would not sign." + left + ". Move the trust file away yourself and run sortie setup again, or "
                      "give sortie start the key it names with --key")
    # The start's check opens the path. What it read is held to being what was read here,
    # in the directory held open, and that file is still open, so the number is its own.
    if checked.pin != (*ident(info), hashlib.sha256(raw).hexdigest()):
        raise Replaced("the trust file at " + str(trust) + " was removed or replaced while this command ran: what is "
                       "there now is not what was checked. Nothing was changed by this command. Run sortie setup again")
    return held


def keygen(tool, key, home, tty, spawn):
    """Start ssh-keygen on the terminal and wait for it. Returns "" when it ended well,
    otherwise why it did not. Nothing but ssh-keygen reads the terminal meanwhile."""
    caught, previous, child, saved = [], {}, None, None
    try:
        # The three signals are caught before the terminal is changed and until it is put
        # back, so that none of them ends this process with echo left off.
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda signum, frame: caught.append(signum))
        # As at signing: echo is off before the prompt, so that a passphrase typed early
        # is not shown, and the terminal is put back whatever becomes of ssh-keygen.
        saved = termios.tcgetattr(tty)
        hidden = list(saved)
        hidden[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(tty, termios.TCSANOW, hidden)
        if termios.tcgetattr(tty)[3] & (termios.ECHO | termios.ECHONL):
            raise Refused("cannot disable terminal echo; refusing the passphrase prompt")
        if caught:
            return "it was interrupted"  # before anything was said or started
        os.write(tty, b"Making your signing key. Type a passphrase only at ssh-keygen's own prompt below, never into "
                      b"an agent.\nIt must not be empty: sortie start does not sign with a key that has none.\n")
        if caught:
            return "it was interrupted"  # before ssh-keygen was started
        # No passphrase option, no pipe, no askpass helper, no agent socket, no inherited
        # variable: the wiring and the environment of the child that signs.
        child = spawn([tool, "-t", "ed25519", "-C", "sortie-kit", "-f", str(key)], stdin=tty, stdout=tty, stderr=tty,
                      env={"PATH": os.defpath, "HOME": str(home), "LC_ALL": "C", "SSH_ASKPASS_REQUIRE": "never",
                           "TMPDIR": str(home), "TMP": str(home), "TEMP": str(home)})
        # ssh-keygen is in the foreground group and gets Ctrl-C itself. After any of the
        # three signals it is given a moment to end, then told to, then killed.
        limit = None
        while child.poll() is None:
            if caught and limit is None:
                limit = time.monotonic() + GRACE
            if limit is not None and time.monotonic() >= limit:
                child.terminate()
                try:
                    child.wait(timeout=GRACE)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                break
            time.sleep(0.05)
        if caught:
            return "it was interrupted"
        return "" if child.returncode == 0 else "exit status " + str(child.returncode)
    except termios.error as exc:
        raise Refused("terminal echo control unavailable; refusing the passphrase prompt") from exc
    finally:
        try:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()
        finally:
            try:
                if saved is not None:
                    termios.tcsetattr(tty, termios.TCSANOW, saved)
            except (OSError, termios.error):
                pass  # a terminal that hung up is gone
            for sig, handler in previous.items():  # last: echo is back by now
                signal.signal(sig, handler)


def new_pair(tool, pin, config, parent, tty, spawn, keep):
    """Have ssh-keygen make a key pair in a directory of this run's own inside the one
    held open, check it, and give both files their names by links that replace nothing.
    Returns the public key and which two files they are. What a failed or interrupted
    ssh-keygen left stays where it is and is named; a key it made with no passphrase is
    removed, and that is said. Nothing else is removed: a name given stays given."""
    name = ".setup-" + uuid.uuid4().hex
    os.mkdir(name, 0o700, dir_fd=parent)
    stage = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    place = config / name
    try:
        # Opened by name a moment after it was made: held to being that one, never made so.
        info = os.fstat(stage)
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or os.listdir(stage):
            raise Refused("the directory " + str(place) + " is not the one this command made a moment ago: it is not "
                          "empty and yours alone. No key was made in it and it was left as it is")
        try:
            why = keygen(tool, place / KEY, place, tty, spawn)
        except BaseException:
            drop(parent, name, stage)  # this run's own, and empty
            raise
        left = sorted(os.listdir(stage))
        if not same(config, parent):
            # ssh-keygen was given a path, and the path led elsewhere for some of that time.
            drop(parent, name, stage)
            raise replaced(config, "No key was given its name and no trust file was written. ssh-keygen was given "
                           "the path " + str(place / KEY) + " to write to, and that path led elsewhere for some of "
                           "the time: " + ("what it made in the directory that was checked is in " + name + " inside "
                                           "that directory, wherever it is now" if left else
                                           "the directory that was checked holds nothing of it")
                           + ". If a file is at that path now, ssh-keygen wrote it into what took the directory's "
                           "place. It was left as it is: look there before you run sortie setup again")
        if why or not {KEY, PUBLIC} <= set(left):
            why = why or "it left no whole key pair"
            if not left:
                drop(parent, name, stage)
                raise Refused("ssh-keygen did not finish (" + why + "), so no key and no trust file were made. "
                              "It left no file")
            raise Refused("ssh-keygen did not finish (" + why + "), so no key and no trust file were made. "
                          + "".join(str(place / n) + " is there now and was left as it is. " for n in left)
                          + "Nothing uses that directory: remove it yourself when you like, then run sortie setup again")
        if changed(tool, pin):
            raise Refused("ssh-keygen at " + tool + " changed while this command ran, so the program that made the key "
                          "is not the one a trust file would name. No key was given its name and no trust file was "
                          "written. What it made is in " + str(place) + " and was left there: remove that directory "
                          "yourself when you like, then run sortie setup again")
        try:
            public, ids = pair(stage, place, keep)
        except NoPassphrase as exc:
            # Made a moment ago by this command, in its own directory, and of no use to a start.
            for n in (KEY, PUBLIC):
                try:
                    os.unlink(n, dir_fd=stage)
                except FileNotFoundError:
                    pass  # gone already
            drop(parent, name, stage)
            raise Refused("the key ssh-keygen just made has no passphrase, and sortie start does not sign with such a "
                          "key. It was removed, and no key and no trust file were made. Run sortie setup again and "
                          "type a passphrase at both of ssh-keygen's prompts") from exc
        except Refused as exc:
            # Not what this command expects of ssh-keygen: left for the person to look at.
            raise Refused("what ssh-keygen made is not a key sortie start would sign with (" + str(exc) + "). No key and "
                          "no trust file were made. Nothing uses the directory " + str(place) + ": remove it yourself "
                          "when you like") from exc
        for n in (KEY, PUBLIC):  # looked at again before either name is given
            if there(parent, n):
                raise Refused("a file appeared at " + str(config / n) + " while the key was being made. It was left as "
                              "it is and nothing was put in its place. The new key pair is in " + str(place)
                              + " and was left there")
        given = []
        try:
            for n in (KEY, PUBLIC):
                os.link(n, n, src_dir_fd=stage, dst_dir_fd=parent, follow_symlinks=False)
                given.append(n)
        except (OSError, KeyboardInterrupt) as exc:
            # No name is taken back: what is at a name by now may be someone else's file.
            n = (KEY, PUBLIC)[len(given)]
            text = ("a file appeared at " + str(config / n) if isinstance(exc, FileExistsError) else
                    str(config / n) + " could not be given its name (" + (str(exc) or "interrupted") + ")")
            text += " while the key pair was being given its names. Nothing was removed and nothing was replaced. "
            if given and named(parent, KEY, ids[KEY]):
                text += (str(config / KEY) + " was given its name by this run and was left there, without its public "
                         "key beside it. ")
            raise Refused(text + "The whole new key pair is in " + str(place) + " and was left there. No trust file "
                          "was written. Look at what is at those names before you run sortie setup again") from exc
        try:
            for n in (KEY, PUBLIC):
                if named(stage, n, ids[n]):  # the second name of a file that has its first
                    os.unlink(n, dir_fd=stage)
        except OSError:
            pass  # both have their names; a second name that stays in the run's own directory harms nothing
        drop(parent, name, stage)
        return public, ids
    finally:
        os.close(stage)


def run(home=None, *, which=shutil.which, terminal=signing.terminal, spawn=subprocess.Popen):
    """Leave a person ready for `sortie start`. Returns the lines to print; raises
    Refused, having replaced nothing, when it cannot."""
    try:
        tty = terminal()  # the check a start makes before it signs, and before anything else here
    except (OSError, ValueError) as exc:
        # What is missing, and nothing on who may run it: the check is the one a start
        # makes, and the words of a start are about signing with a real key.
        missing = getattr(exc, "missing", None) or (
            "the terminal could not be opened or checked (" + (exc.strerror or str(exc)) + ")"
            if isinstance(exc, OSError) else "the check of the terminal failed")
        raise Refused("sortie setup needs a terminal and has none it can use: " + missing + ". It starts ssh-keygen, "
                      "which asks for the new key's passphrase there") from exc
    try:
        return work(home, which, tty, spawn)
    finally:
        os.close(tty)


def work(home, which, tty, spawn):
    tool, pin = trust_init.pinned_tool(which)  # found once: it makes the key and the trust file names it
    config = trust_init.directory(home).absolute()
    key, trust = config / KEY, config / TRUST
    lines, made = [], False
    if not os.path.lexists(config):
        if any(p.is_symlink() for p in config.parents):
            raise Refused("symlink in the path of the sortie-kit directory: " + str(config))
        os.makedirs(config.parent, exist_ok=True)
        try:
            os.mkdir(config, 0o700)
            made = True
        except FileExistsError:
            pass  # made by someone else just now
    # Held open from here to the end: what is read, made and written is in this directory,
    # whatever the path leads to meanwhile.
    parent = trust_init.held(config, "sortie-kit directory")
    keep = []  # each file that is read stays open until its name has been looked at for the last time
    try:
        # What was opened may not be what was made a moment ago, so its mode is judged
        # like that of a directory that was there, and never set.
        info = os.fstat(parent)
        if info.st_uid != os.getuid():
            raise Refused("the directory " + str(config) + " belongs to another account; nothing was made in it")
        have_key, have_public, have_trust = (there(parent, name) for name in (KEY, PUBLIC, TRUST))
        if info.st_mode & 0o022:
            # Whoever can write there can put another key or trust file in place of yours.
            raise Refused("the directory " + str(config) + " is writable by others, so nothing was made in it and it was "
                          "left as it is. Close it yourself and run sortie setup again: " + command("chmod 700", config))
        if made and stat.S_IMODE(info.st_mode) == 0o700:
            lines.append("Made the directory " + str(config) + ", open to you alone.")
        elif info.st_mode & 0o077:
            lines.append("The directory " + str(config) + " can be read by others. It was left as it is; the key and "
                         "the trust file are files of your own alone. To close it: " + command("chmod 700", config))
        if have_key != have_public:
            raise Refused((str(key) + " is there but there is no public key at " + str(config / PUBLIC) if have_key else
                           str(config / PUBLIC) + " is there but there is no key at " + str(key))
                          + ". Nothing was changed. Move the file away or restore the other half, then run sortie "
                          "setup again")
        if have_trust and not have_key:
            raise Refused("there is a trust file at " + str(trust) + " but no key pair beside it. Nothing was changed: "
                          "a new key would not be the one that file names. Move the trust file away yourself, or give "
                          "sortie start the key it names with --key")
        if have_key:
            public, ids = pair(parent, config, keep)
            if have_trust:
                if not same(config, parent):  # before the start's check, which opens the path
                    raise replaced(config, "Nothing was changed.")
                trust_id = ready(parent, trust, public, keep)
                if not same(config, parent):
                    raise replaced(config, "Nothing was changed.")
                if not (all(named(parent, n, ids[n]) for n in ids) and named(parent, TRUST, trust_id)):
                    raise Replaced("the key pair or the trust file in " + str(config) + " was removed or replaced "
                                   "while this command ran: what is there now is not what was checked. Nothing was "
                                   "changed by this command. Run sortie setup again to have what is there now checked")
                return lines + ["The key pair and the trust file are already there: " + str(key) + " and " + str(trust)
                                + ". Nothing was changed.",
                                *trust_init.start_words(trust, key, config.parent.parent)]
            lines.append("The key pair at " + str(key) + " was already there and was kept.")
            kept = "The key pair at " + str(key) + " was kept and nothing else was changed"
        else:
            public, ids = new_pair(tool, pin, config, parent, tty, spawn, keep)
            lines.append("Made the key pair " + str(key) + " and " + str(config / PUBLIC) + " with " + tool + ".")
            kept = "The new key pair is at " + str(key) + " and was left there"
        new = ("The new key pair was given its names in the directory that was checked, wherever that is now. "
               if not have_key else "")

        def confirm():
            """Called when the trust file is complete and before it has its name."""
            if not same(config, parent):
                raise replaced(config, new + "No trust file was written.")
            if not all(named(parent, n, ids[n]) for n in ids):
                raise Replaced("the key pair at " + str(key) + " was removed or replaced while this command ran: what "
                               "is there now is not the pair that was checked. No trust file was written, and this "
                               "command removed nothing. Run sortie setup again to have what is there now checked")
            if changed(tool, pin):
                raise Refused("ssh-keygen at " + tool + " changed while this command ran, so no trust file naming it "
                              "was written")
        try:
            trust_init.init(key, trust, "developer", which=which, tool=tool, public=public, parent=parent,
                            confirm=confirm)
        except Replaced:
            raise
        except (OSError, ValueError) as exc:
            raise Refused("The trust file was not written (" + str(exc) + "). " + kept + ". Run sortie setup again: it "
                          "writes the trust file or says what stands in the way") from exc
        # Before anything is called ready: the path is still the directory that was checked,
        # and the two names in it are still the two files that were read.
        if not same(config, parent):
            raise replaced(config, new + "The trust file was written beside the key, in the directory that was checked.")
        if not all(named(parent, n, ids[n]) for n in ids):
            raise Refused("the key pair at " + str(key) + " is not the one this command checked a moment ago: it "
                          "was removed or replaced meanwhile. The trust file at " + str(trust) + " was written and names "
                          "the key that was checked. Nothing was removed. Run sortie setup again to have what is "
                          "there now checked")
    finally:
        for fd in (*keep, parent):  # each by itself: one that fails neither keeps the rest open nor hides a refusal
            try:
                os.close(fd)
            except OSError:
                pass
    return lines + ["Trust file written: " + str(trust) + " (identity developer). It names that key and " + tool + ".",
                    *trust_init.start_words(trust, key, config.parent.parent)]
