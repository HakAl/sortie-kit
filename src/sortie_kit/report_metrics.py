"""Conventions of the report: unknown values kept visible, metric and time formats, and
numeric deltas against an earlier shift, narrowed to evidence kept on the local machine."""
from datetime import datetime, timezone
import math

UNKNOWN = 'unknown'


def unknown(reason):
    return {'value': UNKNOWN, 'status': UNKNOWN, 'reason': reason}


def metric(value, evidence, status='derived'):
    return {'value': value, 'status': status, 'evidence': list(evidence)}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, OverflowError):
        # OverflowError: a time at an end of the calendar whose offset takes it past that end.
        return None


def numeric(value):
    # JSON integers need no float conversion (which can overflow for retained inputs).
    return type(value) is int or (type(value) is float and math.isfinite(value))


def delta(current, prior, reason='no eligible prior shift report; first-shift delta unavailable'):
    result = {}
    for name, cell in current.items():
        before = prior.get(name, {}) if isinstance(prior, dict) else {}
        a, b = cell.get('value'), before.get('value') if isinstance(before, dict) else None
        result[name] = (metric(a - b, ['current metrics', 'retained prior report'])
                        if numeric(a) and numeric(b) else unknown(reason))
    return result
