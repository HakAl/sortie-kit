"""Bounded local coordination files; publication is never execution authority."""
from contextlib import contextmanager
import fcntl
import os
import re
import stat
import time

from .layout import Refused
from .receipts import session, encoded, digest
from .receipt_schema import decode


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise Refused("expected a bounded identifier")
    return value


def text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Refused("expected bounded nonblank text")
    return value


def unstarted(value):
    """What a driver names with `finish` as in the direction and never briefed: a
    list of at most 32 items, each a short name and a reason. It is the driver's
    statement; the host cannot read a direction and checks only the shape."""
    if not isinstance(value, list) or len(value) > 32:
        raise Refused("not_started is a list of at most 32 items, each with a name and a reason")
    for item in value:
        fields(item, {"name", "reason"})
        text(item["name"], 200)
        text(item["reason"], 500)
    return [dict(name=item["name"], reason=item["reason"]) for item in value]


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - value.keys() or value.keys() - set(required) - set(optional):
        raise Refused("missing or unknown coordination fields")


def response(identity, status, detail, *, state=None, evidence=None, sha=None):
    return dict(version=1, id=identity, status=status, detail=detail[:2000],
                state=state, evidence=evidence, digest=sha)


@contextmanager
def locked(shift, name, seconds=10, *, authorize=None, local_only=False):
    with session(shift, seconds, local_only=local_only) as view:
        if authorize is not None:
            bounded_guard = view.guard
            def guard():
                bounded_guard()
                if why := authorize():
                    raise Refused(why)
            view.guard = guard
        view.guard()
        with view.parent(name) as (parent, leaf):
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                fd = os.open(leaf, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            except FileExistsError:
                fd = os.open(leaf, flags, dir_fd=parent)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise Refused("coordination lock must be a regular file")
                while True:
                    view.guard()
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(min(0.02, view.budget.remaining()))
                view.guard()
                yield view
            finally:
                os.close(fd)


def poll_bytes(shift, request_path, response_path, handler, last, write_seconds=lambda: 5, read_seconds=lambda: 10):
    """Process one immutable reading. Never infer which overwrite the producer intended.
    The caller says how long the reading and the writing of the answer may take."""
    with session(shift, read_seconds()) as view:
        try:
            raw = view.read(request_path)
        except FileNotFoundError:
            return last
        sha = digest(raw)
        if sha == last:
            return last
    try:
        value = decode(raw)
    except Refused as exc:
        result = response(None, "invalid", str(exc), sha=sha)
    else:
        result = handler(value)
        result["digest"] = sha
    with session(shift, write_seconds()) as view:
        view.atomic(response_path, encoded(result), replace=True)
    return sha
