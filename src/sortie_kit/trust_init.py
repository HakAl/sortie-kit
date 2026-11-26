"""Write the trust file from a public key. Reads no private key and signs nothing."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from .grants import KEY_TYPES, encoded, read_file
from .layout import Refused

PUBLIC_LIMIT = 64 * 1024
TOOL_LIMIT = 32 * 1024 * 1024  # the bound grants.Trust applies to the signing tool


def directory(home=None):
    return Path(home or Path.home()) / ".config" / "sortie-kit"


def default_key(home=None):
    return directory(home) / "signing-key"


def default_trust(home=None):
    return directory(home) / "trust.json"


def start_words(trust, key, home):
    """What to say of the next `sortie start` once the trust file and the key are at
    their usual names under `home`. A start looks under the home directory it runs
    with, which need not be this one: a trial makes its files under a home of its own
    and starts under the real one. So the home is named and the flags for a start
    elsewhere are given, ready to paste when the paths can be shown as they are."""
    import shlex
    lines = ["A sortie start run with this home directory, " + str(home) + ", finds both there and needs no --trust "
             "or --key flag."]
    used = [str(trust), str(key)]
    if all(" " <= c <= "~" for text in used for c in text):
        lines.append("Run with another home directory, it needs both: --trust " + shlex.quote(used[0]) + " --key "
                     + shlex.quote(used[1]))
    else:
        # An escaped path is not the path: copying it would name another file.
        lines.append("Run with another home directory, it needs --trust and --key with the paths of these two "
                     "files. The paths hold characters that are shown here with escapes, so they are not offered "
                     "as ready flags.")
    return lines


def held(path, what):
    """An open descriptor for a directory reached through no link. What is read or
    created afterwards is named relative to it, so it lands in the directory checked."""
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Refused("symlink in the path of the " + what + ": " + str(path))
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Refused("cannot open the directory of the " + what + ": " + str(path) + " (" + str(exc) + ")") from exc


def parsed(raw, public):
    """`TYPE BASE64` from the bytes of a public key file, without its comment."""
    parts = raw.decode("utf-8", "replace").split() if len(raw) <= PUBLIC_LIMIT else []
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise Refused("unsupported public key in " + str(public))
    try:
        base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise Refused("invalid public key in " + str(public)) from exc
    return parts[0] + " " + parts[1]


def public_key(key):
    public = Path(str(Path(key).absolute()) + ".pub")
    parent = held(public.parent, "public key")
    try:
        try:
            fd = os.open(public.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError as exc:
            raise Refused("no public key at " + str(public) + "; create the key pair first with sortie setup, or "
                          "with ssh-keygen (see docs/grants.md)") from exc
        except OSError as exc:
            raise Refused("the public key must be a regular file, not a link: " + str(public)) from exc
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Refused("the public key must be a regular file: " + str(public))
            raw = stream.read(PUBLIC_LIMIT + 1)
    finally:
        os.close(parent)
    return parsed(raw, public)


def pinned_tool(which):
    """The ssh-keygen on PATH, held to what grants.Trust will require of it later
    (apart from lying outside the target repository, which is not known here), and
    the pin of its bytes as grants.Trust takes it: device, inode and digest."""
    found = which("ssh-keygen")
    if not found:
        raise Refused("ssh-keygen was not found on PATH")
    tool = Path(found).resolve(strict=True)
    if not tool.is_file():
        raise Refused("ssh-keygen at " + str(tool) + " is not a regular file")
    pin = read_file(tool, TOOL_LIMIT)[1]  # a bounded regular file, as at signing
    if not os.access(tool, os.X_OK):
        raise Refused("ssh-keygen at " + str(tool) + " is not executable")
    return str(tool), pin


def signing_tool(which):
    return pinned_tool(which)[0]


def init(key, out, identity, *, which=shutil.which, tool=None, public=None, parent=None, confirm=None):
    """Create `out` naming the public key beside `key` and the ssh-keygen on PATH,
    or the `tool` that `sortie setup` found and made the key with and the `public`
    key it read once and checked against the key.

    The complete file is written owner-only under a temporary name in the
    output directory and then given its name by a link, which never replaces
    anything: an existing path, link included, refuses, and a failed write
    leaves no file under the final name. The private key is not opened.

    `sortie setup` hands in `parent`, the descriptor of the directory it checked
    and still holds, so that the file lands in that directory whatever is at the
    path by now, and `confirm`, which is called when the file is complete and
    before it is given its name: when that raises, no trust file is left.
    """
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", identity):
        raise Refused("trust requires one literal identity: letters, digits and _ . @ - only")
    value = {"version": 1, "identity": identity, "public_key": public if public is not None else public_key(key),
             "ssh_keygen": tool if tool is not None else signing_tool(which)}
    out = Path(out).absolute()
    own = parent is None
    if own:
        parent = held(out.parent, "trust file")
    try:
        temporary = ".trust-" + uuid.uuid4().hex + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        # The file stays open until its temporary name is gone: while it is open no other
        # file can be given its number, so the name is taken away only when it is this file's.
        with os.fdopen(fd, "wb") as stream:
            try:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded(value))
                stream.flush()
                os.fsync(stream.fileno())
                if confirm is not None:
                    confirm()
                try:
                    os.link(temporary, out.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError as exc:
                    raise Refused(str(out) + " already exists; it is left as it is. Remove it yourself to write "
                                  "a new one") from exc
            finally:
                # What goes wrong here is not raised: it would stand in for a refusal on its
                # way out, or say of a trust file that has its name that it was not written.
                try:
                    there, mine = os.lstat(temporary, dir_fd=parent), os.fstat(stream.fileno())
                    if (there.st_dev, there.st_ino) == (mine.st_dev, mine.st_ino):
                        os.unlink(temporary, dir_fd=parent)
                except OSError:
                    pass
    finally:
        if own:
            os.close(parent)
    return out
