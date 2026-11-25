"""Bounded closeout over retained local bytes. No commands, authority or pane reads."""
import html
from importlib.resources import files
import re
import shlex
import time

from . import channels, grants, receipts, repo_state
from .layout import Refused
from .receipt_schema import decode, validate_shape
from .report_metrics import delta, metric, timestamp, unknown

SCHEMA = 'sortie-kit.report.v2'
MAX_FILES = 512
MAX_RECEIPTS = 256
DIRECTION_SHOWN = 2000  # characters of the direction repeated in the report
TEXT_SHOWN = 600  # characters of any one retained text in report.md; report.json keeps the rest
# Bytes of retained detail one report repeats: the direction, the closeout, the driver's
# list of what was not started, the data of each receipt, the turns, reviews and fields
# of each task, the driver's notes, and each driver command. A real shift of 69 minutes with four tasks came
# to 80 KB, so the bound is several hours of that. What is kept is repeated in other
# places of report.json and takes more room in report.md, so this many bytes can still
# pass the 1 MiB an output may be: write() then makes the report again at DETAIL_SMALL
# before it falls back to the short form.
DETAIL_LIMIT = 600_000
NAMED = 64  # paths named for one receipt as changed, or as read from the host's copy
PATH = 512  # the longest path a receipt may give, kept whole so a check's output is found by it
MISSING = 'missing retained evidence'
LARGE = 'larger than any file a receipt may name, so not the bytes the receipt gives; not read'
DETAIL_SMALL = 80_000
# The report's own bound on details, as the reason it gives. It does not say that a kept
# file is missing or damaged, and the status line does not say so for it. Every file is
# read and checked before its details are counted, so nothing goes unread because of it.
LEFT_OUT = 'details left out of this report at its own size limit; the kept files have them'
# Here the reading stops, so what comes after the task that did not fit was not checked:
# this one keeps the wider words of the status line.
TASKS_LEFT_OUT = 'tasks left out of this report at its own size limit and not read further; driver-state.json has them'
OWN_LIMITS = (LEFT_OUT,)
LIMIT_BLOCKER = 'This report left some details out at its own size limit; see "Problems with the evidence"'


def only_limits(issues):
    """True when every problem listed is a bound of the report and none is of the evidence."""
    return bool(issues) and all(get(i, 'reason') in OWN_LIMITS for i in issues)


class Reader:
    def __init__(self, view, limit=None):
        self.view, self.limit = view, DETAIL_LIMIT if limit is None else limit
        self.end = time.monotonic() + max(0, view.budget.remaining() - min(1, view.budget.remaining() / 2))
        self.inputs, self.issues, self.cache = {}, [], {}
        self.details = 0
        # Of the receipt being read: the paths it names that do not hold the bytes it
        # gives, and those whose bytes were read from the host's own copy.
        self.changed, self.kept = [], []

    def again(self, limit):
        """A reader for one more pass with a smaller bound on details. It has what this
        one read, so that no file is read or counted twice against the bound on all
        that is read, and the same time to finish in."""
        other = Reader(self.view, limit)
        other.end, other.inputs, other.cache = self.end, self.inputs, self.cache
        return other

    def issue(self, path, reason):
        item = dict(path=str(path)[:300], reason=str(reason)[:500])
        own = item['reason'] in OWN_LIMITS
        if own and item in self.issues:
            return  # said once for each place
        if len(self.issues) < 128:
            self.issues.append(item)
        elif not own:
            # The list is full. A problem of the evidence is never the one dropped for a
            # bound of the report: it takes the place of the last of those, if there is one.
            for at in range(len(self.issues) - 1, -1, -1):
                if self.issues[at]['reason'] in OWN_LIMITS:
                    self.issues[at] = item
                    break

    def guard(self):
        self.view.guard()
        if time.monotonic() >= self.end:
            raise Refused('extraction budget exhausted; publication time reserved')

    def read(self, path, *, optional=False, limit=receipts.DOCUMENT_LIMIT, problems=None):
        """`problems`: a list that takes the reason a read failed, for a caller that
        has another place to look before it is a problem of the evidence."""
        said = self.issue if problems is None else lambda path, reason: problems.append(reason)
        try:
            self.guard()
            if path not in self.cache:
                if len(self.cache) >= MAX_FILES:
                    raise Refused('report input count limit reached')
                # The ledger is replaced under a lock of its own, which this report does not
                # hold, by a receipt check that records. Everything else is read once.
                raw = (self.view.settled if path == 'receipts/.consumed.json' else self.view.read)(path, limit)
                self.cache[path] = raw
                self.inputs[path] = dict(path=path, sha256=receipts.digest(raw), bytes=len(raw))
            return self.cache[path]
        except FileNotFoundError:
            if not optional:
                said(path, MISSING)
        except receipts.TooLarge as exc:
            said(path, exc if problems is None else LARGE)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            said(path, exc)
        return None

    def document(self, path, optional=False):
        raw = self.read(path, optional=optional)
        if raw is not None:
            try:
                # Draft grants permit fractional durations; receipt wire decoding below stays strict.
                value = grants.decoded(raw)
                receipts.encoded(value)  # Refuse non-finite numbers, including exponent overflow.
                if not isinstance(value, dict):
                    raise Refused('expected a JSON object')
                return value
            except (ValueError, RecursionError) as exc:
                self.issue(path, exc)
        return None

    def artifact(self, ref):
        if not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}:
            self.issue('artifact', 'invalid artifact reference')
            return False
        path, sha, problems = ref['path'], ref['sha256'], []
        raw = self.read(path, limit=receipts.ARTIFACT_LIMIT, problems=problems)
        if raw is not None and receipts.digest(raw) == sha:
            return True
        if raw is None and problems not in ([MISSING], [LARGE]):
            # The path could not be read (a limit of this report, a name that is not a
            # plain relative one, a file that is no regular file): nothing was seen of
            # its bytes, so it is neither called changed nor looked for among the copies.
            # A regular file over the size any artifact may have is not of these: it was
            # not read either, and its size says it is not the file the receipt names.
            for reason in problems:
                self.issue(path, reason)
            return False
        # The name is gone or no longer holds those bytes. A seat may use a name again;
        # the host kept its own copy when it consumed the receipt, named by the sum.
        name = str(path)[:PATH]
        if isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha):
            lost = []
            copy = self.read(receipts.kept_path(sha), limit=receipts.ARTIFACT_LIMIT, problems=lost)
            if copy is not None and receipts.digest(copy) == sha:
                if len(self.kept) < NAMED and not any(k['path'] == name for k in self.kept):
                    self.kept.append(dict(path=name, kept=receipts.kept_path(sha)))
                return True
            # A copy that is there and cannot back the receipt is a problem of its own.
            for reason in (['kept copy does not hold the bytes it is named by'] if copy is not None
                           else [r for r in lost if r != MISSING]):
                self.issue(receipts.kept_path(sha), reason)
        for reason in problems or ['artifact hash mismatch']:
            self.issue(path, reason)
        if len(self.changed) < NAMED and name not in self.changed:
            self.changed.append(name)
        return False

    def references(self, node):
        good = True
        if isinstance(node, dict):
            if set(node) == {'path', 'sha256'}:
                return self.artifact(node)
            for value in node.values():
                good = self.references(value) and good
        elif isinstance(node, list):
            for value in node:
                good = self.references(value) and good
        return good

    def retain(self, value, path):
        size = len(receipts.encoded(value))
        if self.details + size > self.limit:
            self.issue(path, LEFT_OUT)
            return None
        self.details += size
        return value


def review_kept(data):
    """What the report keeps of a review receipt the host wrote: every attempt as the
    host recorded it, and of the reviewer's own record its target, its findings in
    short and its answers to earlier findings. The receipt has the full text."""
    def short(value, limit):
        return value[:limit] if isinstance(value, str) else None

    attempts = []
    for attempt in data['attempts'][:8]:
        record = attempt.get('record') if isinstance(attempt.get('record'), dict) else {}
        listed = lambda name: [f for f in record.get(name) or [] if isinstance(f, dict)][:16]
        item = {k: attempt.get(k) for k in ('attempt', 'status', 'verdict', 'ended_by', 'exit_status', 'started', 'ended',
                                             'elapsed_seconds', 'repository', 'directory', 'stdout', 'stderr',
                                             'output_dropped_bytes', 'marker')}
        item.update(reasons=[short(reason, 400) for reason in attempt.get('reasons') or []][:4],
                    target=record.get('target'), findings_count=len(record.get('findings') or []),
                    findings=[dict(id=f.get('id'), severity=f.get('severity'), location=short(f.get('location'), 200),
                                   claim=short(f.get('claim'), 300)) for f in listed('findings')],
                    # Every answer, up to the contract's own bound: which earlier findings an
                    # ACCEPT left unresolved is read from these, and one must not be lost.
                    prior=[dict(id=f.get('id'), resolved=f.get('resolved'), reason=short(f.get('reason'), 300))
                           for f in record.get('prior') or [] if isinstance(f, dict)][:64])
        attempts.append(item)
    return dict({k: data.get(k) for k in ('target', 'base', 'implementer_family', 'reviewer', 'limit_seconds',
                                          'outcome', 'prior')}, attempts=attempts)


def receipt_summary(reader, ref, task, shift_id, ledger):
    out = dict(reference=ref, integrity='unavailable', consumed=False, phase=None, data=None)
    out['changed'], out['kept'] = reader.changed, reader.kept = [], []
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str) or not isinstance(ref.get('sha256'), str):
        reader.issue('driver-state.json', 'invalid receipt reference')
        return out
    raw = reader.read(ref['path'])
    if raw is None:
        return out
    try:
        if receipts.digest(raw) != ref['sha256']:
            raise Refused('receipt hash differs from driver record')
        value = decode(raw)
        validate_shape(value, 'receipt', versions=(1, 2, 3))
        # From version 3 on a review is the host's own receipt, written as the reviewer's.
        hosted = value['version'] == 3 and value['phase'] == 'review'
        if (value['shift_id'] != shift_id or value['task_id'] != task['id']
                or value['role'] != ('reviewer' if hosted else 'builder')
                or value['brief'] != task['brief'] or value['round'] != ref.get('round')):
            raise Refused('receipt identity/brief/round differs from driver record')
        phase = value['phase']
        expected_phase = {'ack': 'acknowledgment', 'plan_review': 'review'}.get(ref.get('stage'), ref.get('stage'))
        if phase != expected_phase:
            raise Refused('receipt phase differs from driver record')
        good = reader.references(value)
        slot = receipts.digest(receipts.encoded([value[k] for k in ('shift_id', 'task_id', 'request_id', 'role', 'phase', 'round')]))
        consumed = any(e.get('receipt_id') == value['receipt_id'] and e.get('slot') == slot
                       and e.get('sha256') == ref['sha256'] and e.get('satisfies_phase') is True for e in ledger)
        data = reader.retain(review_kept(value['data']) if hosted else value['data'], ref['path'])
        out.update(phase=phase, round=value['round'], request_id=value['request_id'],
                   data=data, consumed=consumed, integrity='matched' if good and data is not None else 'damaged')
        if value['version'] == 3:
            out.update(version=3, role=value['role'])
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        reader.issue(ref['path'], exc)
    return out


def task_summary(reader, task, shift_id, ledger, host_reviews=False):
    """`host_reviews`: the kept grant has a reviewer seat, so every review of this shift
    is the host's and only a receipt the host wrote as the reviewer backs an acceptance."""
    if (not isinstance(task, dict) or not isinstance(task.get('id'), str)
            or not isinstance(task.get('receipts'), list) or len(task['receipts']) > 128):
        raise Refused('invalid task structure')
    result = {k: task.get(k) for k in ('id', 'kind', 'stage', 'round', 'candidate', 'plan', 'reason', 'next_owner')}
    result.update(acceptance='acceptance missing', cessation='builder cessation unknown', receipts=[])
    accepted, cessation = task.get('accepted'), task.get('cessation')
    if accepted is not None and not isinstance(accepted, dict):
        raise Refused('invalid recorded acceptance')
    all_sound = True
    if accepted:
        # Whether the review receipt the acceptance names is, kept and consumed, the ACCEPT
        # of the candidate. An acceptance may be unbacked with this true: a build receipt
        # of the round may be what no longer holds.
        result['review_matched'] = False
    for ref in task['receipts']:
        item = receipt_summary(reader, ref, task, shift_id, ledger)
        result['receipts'].append(item)
        sound = item['integrity'] == 'matched' and item['consumed']
        hosted = item.get('role') == 'reviewer'
        # A review of the host's that gave no ACCEPT backs nothing and is never what an
        # acceptance rests on: one that failed and was followed by another in the same
        # round does not make the later one unsound.
        aside = hosted and isinstance(ref, dict) and ref.get('status') != 'satisfied'
        if (isinstance(ref, dict) and ref.get('round') == task.get('round') and ref.get('stage') in ('build', 'review')
                and not aside):
            all_sound = all_sound and sound
        data = item['data'] or {}
        if accepted and ref == accepted.get('receipt'):
            current = task.get('candidate')
            if hosted:
                # What the host itself requires before it takes an ACCEPT, as far as the
                # kept receipt says it. The reviewer's output is not parsed again here.
                last = (data.get('attempts') or [{}])[-1]
                match = (sound and accepted.get('candidate') == current and data.get('target') == current
                         and get(current, 'kind') == 'commit' and ref.get('stage') == 'review'
                         and data.get('outcome') == 'ACCEPT' and item['phase'] == 'review'
                         and item.get('round') == task.get('round') and last.get('target') == current
                         and last.get('status') == 'complete' and last.get('verdict') == 'ACCEPT'
                         and last.get('ended_by') == 'exit' and last.get('exit_status') == 0
                         and last.get('output_dropped_bytes') == 0 and isinstance(last.get('stdout'), dict)
                         and isinstance(last.get('marker'), str) and last.get('repository') == 'unchanged')
            elif host_reviews:
                match = False  # a review receipt the builder wrote, in a shift whose reviews are the host's
            else:
                match = (sound and accepted.get('candidate') == current and data.get('target') == current
                         and data.get('verdict') == 'ACCEPT' and item['phase'] == 'review'
                         and item.get('round') == task.get('round') and data.get('invocations')
                         and all(i['target'] == current and i['status'] == 'complete' and i['exit_status'] == 0
                                 for i in data['invocations']))
            if match:
                result['acceptance'] = 'local acceptance recorded; retained review matches candidate'
                result['review_matched'] = True
        if ref == cessation and sound and item['phase'] == 'stop' and data.get('scope') == 'seat' and data.get('status') == 'stopped':
            result['cessation'] = 'builder seat stop reported in retained receipt; not independently observed'
    if accepted and (not all_sound or not result['acceptance'].startswith('local acceptance recorded')):
        result['acceptance'] = UNBACKED
        reader.issue(task['id'], 'recorded acceptance is not backed by intact consumed build and review evidence '
                                 'of its round')
    result['turns'] = reader.retain(task.get('turns', []), task['id'])
    if isinstance(task.get('reviews'), list) and (task['reviews'] or any(r.get('role') == 'reviewer' for r in result['receipts'])):
        # The reviews the host ran for this task, as it recorded each when it ended.
        result['reviews'] = reader.retain(task['reviews'][:64], task['id']) or []
    if task.get('delivery'):
        # A shift from the pane transport: keep its send records rather than drop them.
        result['delivery'] = reader.retain(task['delivery'], task['id'])
    return result


def extract(view, *, previous=None, reader=None):
    reader = reader or Reader(view)
    result = dict(schema=SCHEMA, shift_id=view.shift.name, repo=str(view.repo), status='partial',
                  grant=None, actual_window=unknown('no retained host closeout'), tasks=[], actions=[],
                  direction=None, blockers=[], next_owner='developer', not_started=None, driver_notes=None, last_activity=None, metrics={}, delta={}, inputs=[], issues=[],
                  limits=['Only what the kept files claim, and whether those files are intact. No signature, commit, '
                          'test or code quality was checked again.',
                          'Writing this report ran no command, agent or network call. Nothing is assumed to have been '
                          'pushed or delivered.',
                          'The toolkit finishing does not show that a seat stopped. Times of receipts are given only '
                          'where they were kept.',
                          "A seat's log holds what that seat's CLI printed, which for some CLIs is only its final "
                          'message. sortie-kit does not record what a seat ran.'])
    state, prior, closed, summarized = None, None, None, False
    try:
        grant_raw = reader.read('grant.json', optional=True)
        grant = reader.document('grant.json', optional=True) if grant_raw is not None else reader.document('grant.draft.json')
        if grant:
            # The signer writes grant.json before the signature exists, so the bytes alone show no signing.
            provenance = ('unsigned preparation only' if grant_raw is None
                          else 'retained grant; signature not reverified' if reader.read('grant.json.sig', optional=True) is not None
                          else UNSIGNED)
            result['grant'] = dict(sha256=receipts.digest(grant_raw) if grant_raw is not None else None,
                                  provenance=provenance,
                                  **{k: grant.get(k) for k in ('repo', 'start', 'until', 'direction_sha256', 'seats',
                                                               'review_policy', 'allowed_actions')})
            if 'review_limit_seconds' in grant:  # a grant with a reviewer seat
                result['grant']['review_limit_seconds'] = grant['review_limit_seconds']
        asked = reader.read('direction.md', optional=True)
        if asked is not None:
            text = asked.decode('utf-8', 'replace')
            result['direction'] = reader.retain(dict(
                sha256=receipts.digest(asked), text=text[:DIRECTION_SHOWN], shortened=len(text) > DIRECTION_SHOWN,
                matches_grant=bool(grant) and receipts.digest(asked) == grant.get('direction_sha256')), 'direction.md')
        closed = reader.document('closeout.json', optional=True)
        if closed:
            if (closed.get('version') == 1 and closed.get('source') in ('foreground host', 'admitted start failed')
                    and timestamp(closed.get('start')) and timestamp(closed.get('end'))
                    and timestamp(closed['end']) >= timestamp(closed['start'])
                    and type(closed.get('elapsed_seconds')) is int and closed['elapsed_seconds'] >= 0
                    and grant_raw and closed.get('grant_sha256') == receipts.digest(grant_raw)
                    and closed.get('direction_sha256') == grant.get('direction_sha256')):
                result['actual_window'] = reader.retain(closed, 'closeout.json') or unknown('closeout details omitted')
            else:
                reader.issue('closeout.json', 'malformed or differently bound host observation')
        state = reader.document('driver-state.json')
        if state:
            if (state.get('version') != 1 or not isinstance(state.get('tasks'), dict) or len(state['tasks']) > 128
                    or not isinstance(state.get('commands'), dict) or len(state['commands']) > 512
                    or not grant_raw or state.get('grant_sha256') != receipts.digest(grant_raw)
                    or state.get('direction_sha256') != grant.get('direction_sha256')):
                raise Refused('driver state malformed or differently bound to retained grant')
            if closed and closed.get('grant_sha256') != state['grant_sha256']:
                raise Refused('closeout grant pin differs from retained state/grant')
            direction = reader.read('direction.md')
            if direction is None or receipts.digest(direction) != state['direction_sha256']:
                raise Refused('direction missing or changed')
            result['next_owner'] = state.get('next_owner') or 'developer'
            result['reason'] = state.get('reason')
            result['last_activity'] = reader.retain(last_activity(state), 'driver-state.json')
            try:
                # The driver's own list of what the direction held and it never briefed.
                # None: no list was given, or what is kept is not one.
                result['not_started'] = (reader.retain(channels.unstarted(state['not_started']), 'driver-state.json')
                                         if state.get('not_started') is not None else None)
            except (Refused, KeyError, TypeError):
                reader.issue('driver-state.json', "the driver's list of what was not started is malformed; not shown")
            try:
                # The driver's notes, its own words. A state from before they existed has
                # none. Checked here and kept after the tasks, so that at the bound on
                # details the notes are what is left out and never a task.
                noted = channels.notes(state.get('notes', []))
                if any(n['task_id'] is not None and n['task_id'] not in state['tasks'] for n in noted):
                    raise Refused('a note names a task the state does not hold')
            except (Refused, KeyError, TypeError):
                noted = None
                reader.issue('driver-state.json', "the driver's notes are malformed; not shown")
            # The ledger is written at the first consumed receipt. Its absence is
            # damage only when the state records a receipt it should account for.
            recorded = any(isinstance(t, dict) and t.get('receipts') for t in state['tasks'].values())
            ledger_doc = reader.document('receipts/.consumed.json', optional=not recorded) if state['tasks'] else None
            ledger = ledger_doc.get('entries', []) if ledger_doc else []
            if not isinstance(ledger, list) or len(ledger) > 4096 or any(not isinstance(e, dict) for e in ledger):
                raise Refused('invalid consumed ledger')
            count = 0
            for task in state['tasks'].values():
                reader.guard()
                if not isinstance(task, dict) or not isinstance(task.get('receipts'), list):
                    raise Refused('invalid retained task')
                count += len(task.get('receipts', []))
                if count > MAX_RECEIPTS:
                    raise Refused('receipt extraction count limit reached')
                summary = task_summary(reader, task, view.shift.name, ledger, hosted(get(grant, 'review_policy')))
                # Data retained by receipt_summary is already charged; charge task fields too.
                fields = {k: v for k, v in summary.items() if k not in ('receipts', 'turns', 'delivery')}
                if reader.retain(fields, 'driver-state.json') is None:
                    raise Refused(TASKS_LEFT_OUT)
                result['tasks'].append(summary)
            summarized = True  # every task of the driver's record is in the list
            if noted is not None:
                # No notes are not a detail that can be left out.
                result['driver_notes'] = reader.retain(noted, 'driver-state.json') if noted else []
            for identity, command in state['commands'].items():
                item = reader.retain(dict(id=identity, record=command), 'driver-state.json')
                if item is not None:
                    result['actions'].append(item)
            if state.get('pending'):
                reader.issue('driver-state.json', 'unresolved command intent')
            if state.get('turn'):
                for summary in result['tasks']:
                    if summary['id'] == get(state['turn'], 'task_id'):
                        summary['turn_running'] = True  # started, with no recorded end
                reader.issue('driver-state.json', 'a builder turn is recorded as started with no recorded end; '
                                                  'whether its process still runs is unknown')
            if get(state.get('review'), 'running'):
                reader.issue('driver-state.json', 'a review is recorded as started with no recorded end; '
                                                  'whether its process still runs is unknown')
        for name in ('preparation-error.txt', 'STOP'):
            if reader.read(name, optional=True) is not None:
                reader.issue(name, 'retained failure/withdrawal evidence; see input hash')
        if previous:
            if previous in ('report.json', 'closeout.json', 'driver-state.json'):
                raise Refused('prior report must be a distinct retained input')
            prior = reader.document(previous)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        reader.issue('report extraction', exc)
    # What the host saw of the repository at its start and end (closeout `repo`):
    # rows that are new at the end are what the shift left uncommitted.
    seen = closed.get('repo') if isinstance(closed, dict) and isinstance(closed.get('repo'), dict) else None
    if seen is not None and result['actual_window'].get('repo') is not None:
        start, end = seen.get('start'), seen.get('end')
        known, began = repo_state.usable(end), repo_state.usable(start)
        rows = end['changes'] if known else []
        whole = lambda look: look.get('truncated') is False
        # Rows the start also had were there before. For the others, `since_start`
        # says whether they are known to be new: not without a usable look at the
        # start, and not when that look was cut, since a path it left out can show
        # up at the end.
        fresh = repo_state.new(start, end) if known and began else rows
        result['leftovers'] = dict(new=fresh, before=len(rows) - len(fresh), known=known,
                                   since_start=began and whole(start),
                                   complete=known and whole(end) and (not began or whole(start)))
        linked = end.get('worktrees') if known else None
        if isinstance(linked, list) and linked:
            result['leftovers']['worktrees'] = [str(path)[:400] for path in linked[:20]]
    # Only where the reviews are the host's: an earlier shift's reviews were the builder's reports.
    # Without every task of the driver's record there is no saying how many: not known, which is not none.
    result['open_findings'] = (None if not hosted(get(result['grant'], 'review_policy'))
                               else open_findings(result['tasks']) if summarized else dict(OPEN_UNKNOWN, items=[]))
    result['issues'] = reader.issues
    result['inputs'] = list(reader.inputs.values())
    result['status'] = 'partial' if reader.issues else 'evidence summarized'
    result['blockers'] = [dict(task=t['id'], stage=t.get('stage'), reason=t.get('reason') or t['acceptance'],
                               next_owner=t.get('next_owner') or 'developer')
                          for t in result['tasks'] if t['acceptance'] != ACCEPTED]
    if reader.issues:
        result['blockers'].append(dict(task=None, next_owner='developer', reason=LIMIT_BLOCKER if only_limits(reader.issues)
                                       else 'missing/damaged/incomplete evidence; inspect issues'))
    complete = not reader.issues and state is not None
    result['metrics']['tasks'] = metric(len(result['tasks']), ['driver-state.json']) if complete else unknown('incomplete task evidence')
    accepted = sum(t['acceptance'] == 'local acceptance recorded; retained review matches candidate' for t in result['tasks'])
    result['metrics']['accepted_tasks'] = metric(accepted, ['driver-state.json', 'receipts/.consumed.json']) if complete else unknown('incomplete acceptance evidence')
    eligible = (isinstance(prior, dict) and prior.get('schema') == SCHEMA and prior.get('repo') == result['repo']
                and prior.get('shift_id') != result['shift_id'] and isinstance(prior.get('grant'), dict)
                and timestamp(prior['grant'].get('start')) and result['grant'] and timestamp(result['grant'].get('start'))
                and timestamp(prior['grant']['start']) < timestamp(result['grant']['start']))
    result['delta'] = delta(result['metrics'], prior.get('metrics') if eligible else None,
                            'prior/current coverage unavailable' if eligible else 'no eligible prior shift report; first-shift delta unavailable')
    result['prior'] = dict(path=previous, eligible=bool(eligible), provenance='retained comparison only')
    return result


IDLE = 300  # seconds at the end of a shift with no command recorded and nothing run that the Outcome names


def last_activity(state):
    """The host's own stamps of what it last did for the driver: `command`, when the last
    driver command was recorded; `work`, when a builder turn or a review attempt the host
    ran last ended; `running`, whether one is recorded as started with no end; `commands`,
    how many are on record. A time is None when there is none, and `command` also when a
    record has no time or an unreadable one, as in a state kept by a host from before the
    time was recorded: which command was the last is then not known. `work_known` is
    False when the kept state does not tell when everything the host ran ended: a turn
    or an attempt with no readable end, a turn or a review kept as lost, a review with
    no attempt, or something that is not a list where the turns or reviews belong."""
    def latest(stamps):
        stamps = list(stamps)
        times = [timestamp(stamp) for stamp in stamps]
        return max(zip(times, stamps))[1] if times and all(times) else None
    known, ended = [True], []

    def rows(value):  # a kept state may hold anything where a list belongs
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            known[0] = False
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    def end(row):
        if timestamp(row.get('ended')) is None or row.get('outcome') == 'lost':
            known[0] = False
        else:
            ended.append(row['ended'])
    commands = [get(record, 'at') for record in state['commands'].values()]
    for task in state['tasks'].values():
        if not isinstance(task, dict):
            known[0] = False
            continue
        # A task kept by a host from before turns or reviews were recorded has no such key.
        for row in rows(task.get('turns', [])):
            end(row)
        for review in rows(task.get('reviews', [])):
            attempts = rows(review.get('attempts'))
            if not attempts or review.get('outcome') == 'lost':
                known[0] = False  # what ran last for it has no recorded end
            for item in attempts:
                end(item)
    return dict(command=latest(commands), commands=len(commands), work=latest(ended), work_known=known[0],
                running=bool(state.get('turn') or get(state.get('review'), 'running')))


def idle_words(seen, window):
    """The Outcome line for a shift that ended IDLE seconds or more after its last driver
    command and after the last end of a builder turn or review the host ran: when the
    command was recorded and how long before the end. It gives the two stamps and what
    is on record between them. It gives no reason, because the host knows none: a driver
    that gave up, one that waited for a person, and one whose process hung all look like
    this. The stamps are of the wall clock, so a machine that slept is said. Nothing is
    said when a time is not known."""
    end = timestamp(get(window, 'end'))
    if not isinstance(seen, dict) or not end or seen.get('running') is not False:
        return []
    command, work = timestamp(seen.get('command')), timestamp(seen.get('work'))
    known = seen.get('work_known') is True
    # The seconds the host counted, on a clock that stands still while the machine sleeps:
    # a shift that ran for less than IDLE had no such time, whatever its two stamps say.
    counted = window.get('elapsed_seconds')
    if type(counted) is not int or counted < IDLE or (command is None and seen.get('commands') != 0):
        return []
    not_known = 'When the last builder turn or review the host ran ended is not known from the kept state.'
    if command is None:
        # An end on record is the earliest the host stopped working, also when another
        # end is not known: that one can only be later.
        if work and int((end - work).total_seconds()) < IDLE:
            return []
        line = '- **No driver command was recorded in the ' + seconds_words(counted) + ' this shift ran'
        return [line + ('.** ' + not_known if not known
                        else '.** The last builder turn or review the host ran ended at ' + plain(seen['work'], 40) + '.' if work
                        else '**, and no builder turn and no review is on record.') + ' The host does not know why.']
    # A command is stamped in whole seconds and the end of a turn or a review is not.
    work = work.replace(microsecond=0) if work else None
    since = int((end - command).total_seconds())
    idle = int((end - max(filter(None, (command, work)))).total_seconds())
    if idle < IDLE:
        return []
    line = ('- **Last driver command: recorded at ' + plain(seen['command'], 40) + ', ' + seconds_words(since)
            + ' before the end.** ')
    line += (not_known if not known
             else 'The last builder turn or review the host ran ended at ' + plain(seen['work'], 40) + '; in the '
                  + seconds_words(idle) + ' after that no command was recorded and no turn or review ended.'
             if work and work > command else 'No builder turn and no review ended in that time.')
    began = timestamp(get(window, 'start'))
    slept = (end - began).total_seconds() - counted if began else 0
    if slept > SLEPT:  # the same comparison as the "Ran for" line makes
        slept = int((end - began).total_seconds()) - counted
        line += (' Both times are of the wall clock, and for ' + seconds_words(slept) + ' of this shift the machine '
                 'was asleep or its clock was changed: how much of the ' + seconds_words(idle if known else since)
                 + ' the host was running is not known.')
    return [line + ' The host does not know why no command followed.']


SLEPT = 60  # seconds the two stamps of a closeout may be further apart than the time the host counted
OPEN_KEPT = 256  # findings left open that the report lists; the rest are counted


OPEN_UNKNOWN = dict(count=None, items=[], more=0, unread=0)  # the tasks could not be read in full


def open_findings(tasks):
    """Findings that a review which ended in ACCEPT left open, and that no later review marked resolved.

    An ACCEPT moves the task on whatever findings came with it, and nobody is
    asked to act on those, so the report lists them. They are of two kinds: the
    findings the accepting review itself made, and the earlier findings it was
    asked about and answered as not resolved, which an ACCEPT may do for a minor
    one (`earlier` is true for those; their place and claim are taken from the
    earlier receipt of the same task and stage that made them, or else the
    reviewer's reason stands for the claim).

    One counts as resolved when any later review of the same task and stage,
    whose last attempt completed, was asked about it by the host (that review's
    recorded `prior`) and answered that it is resolved. When a later review that
    also ended in ACCEPT answered that it is not, the finding is listed once,
    under that later review. A later review that makes a finding with the same
    id speaks of another finding from then on. The host hands none of an
    ACCEPT's findings on today, so none of this happens to them; the rule keeps
    the list right if it ever does.

    `tasks` are the task summaries of `extract`. Returns `count`: how many there
    are, `items`: those listed (at most OPEN_KEPT), `more`: how many the receipts
    hold beyond the listed, and `unread`: accepted reviews whose receipt could
    not be read, so that their findings are not known."""
    items, total, unread = [], 0, 0
    listed = lambda node, name: [x for x in get(node, name) or [] if isinstance(x, dict)] if isinstance(get(node, name), list) else []
    readable = lambda row: row.get('integrity') == 'matched' and isinstance(row.get('data'), dict)

    def final(row):
        attempts = listed(row['data'], 'attempts')
        return attempts[-1] if attempts else {}

    def answers(row):
        """What a review that completed answered to the earlier findings the host asked it about."""
        if get(final(row), 'status') != 'complete':
            return {}
        asked = {get(p, 'id') for p in listed(row['data'], 'prior')}
        return {get(p, 'id'): get(p, 'resolved') for p in listed(final(row), 'prior')
                if get(p, 'id') in asked and type(get(p, 'resolved')) is bool}

    def later(identity, after):
        """True: a later review resolved it. False: a later ACCEPT lists it. None: neither."""
        for row in after:
            if not readable(row):
                continue
            answer = answers(row).get(identity)
            if answer is True or (answer is False and get(row, 'data', 'outcome') == 'ACCEPT'):
                return answer
            if any(get(f, 'id') == identity for f in listed(final(row), 'findings')):
                return None  # the id is another finding's from here on
        return None

    for task in tasks:
        if not isinstance(task, dict):
            continue
        # The host's own review receipts, and any review receipt that could not be read:
        # whose it was is then not known, and in such a shift every review is the host's.
        rows = [r for r in (task.get('receipts') if isinstance(task.get('receipts'), list) else [])
                if isinstance(r, dict) and get(r, 'reference', 'stage') in ('plan_review', 'review')
                and (r.get('role') == 'reviewer' or not readable(r))]
        numbers = {get(r, 'receipt', 'path'): get(r, 'review')
                   for r in (task.get('reviews') if isinstance(task.get('reviews'), list) else []) if isinstance(r, dict)}
        for at, row in enumerate(rows):
            ref = row['reference']
            if not readable(row):
                # The driver's record says how it ended: `satisfied` is an ACCEPT.
                unread += ref.get('status') == 'satisfied'
                continue
            if get(row, 'data', 'outcome') != 'ACCEPT':
                continue
            last = final(row)
            same = lambda others: [r for r in others if get(r, 'reference', 'stage') == ref.get('stage')]
            before, after = same(rows[:at]), same(rows[at + 1:])
            open_here = lambda identity: not isinstance(identity, str) or later(identity, after) is None
            found = [dict(id=f.get('id'), severity=f.get('severity'), location=f.get('location'), claim=f.get('claim'),
                          earlier=False) for f in listed(last, 'findings') if open_here(f.get('id'))]
            severity = {get(p, 'id'): get(p, 'severity') for p in listed(row['data'], 'prior')}
            for identity, resolved in answers(row).items():
                if resolved is False and open_here(identity):
                    # The receipt that made it: the nearest earlier one of this task and stage.
                    was = next((f for r in reversed(before) if readable(r) for f in listed(final(r), 'findings')
                                if get(f, 'id') == identity), None)
                    reason = next((get(p, 'reason') for p in listed(last, 'prior') if get(p, 'id') == identity), None)
                    found.append(dict(id=identity, severity=severity.get(identity), location=get(was, 'location'),
                                      claim=get(was, 'claim') if was else reason, earlier=True))
            count = last.get('findings_count') if type(last.get('findings_count')) is int else 0
            # What the receipt holds beyond the findings the report keeps of one review.
            total += len(found) + max(0, count - len(listed(last, 'findings')))
            items += [dict(task=task.get('id'), review=numbers.get(ref.get('path')), stage=ref.get('stage'),
                           round=ref.get('round'), receipt=ref.get('path'), **f) for f in found]
    return dict(count=total, items=items[:OPEN_KEPT], more=total - min(len(items), OPEN_KEPT), unread=int(unread))


def open_words(found):
    """(the Outcome line, the section) for the findings left open, or (None, []) for a
    shift whose reviews were not the host's."""
    if not isinstance(found, dict):
        return None, []
    head = ['## Findings left open on accepted work', '']
    line = '- Findings left open on accepted work: '
    if type(found.get('count')) is not int:
        return (line + "not known, because the tasks could not be read in full or were left out of this report.",
                head + ["Not known: the driver's record of the tasks could not be read in full, or the tasks were "
                        'left out of this report at its output limit, so the reviews were not gone through. See '
                        '"Problems with the evidence".', ''])
    number = lambda name: found.get(name) if type(found.get(name)) is int and found.get(name) > 0 else 0
    items = [f for f in found.get('items') or [] if isinstance(f, dict)] if isinstance(found.get('items'), list) else []
    more, unread = number('more'), number('unread')
    total = len(items) + more
    lost = (count_words(unread, 'accepted review') + ', whose receipt could not be read') if unread else ''
    if total:
        line += str(total) + '. They are listed under that heading below.' + (' Not known for ' + lost + '.' if lost else '')
    else:
        line += 'not known for ' + lost + '; none in the others.' if lost else 'none.'
    out = head
    if total:
        out += ['Each is a finding that a review which ended in ACCEPT left open, and that no later review marked '
                'resolved. The task went on all the same, and nobody was asked to act on it.', '']
    stage = {'plan_review': 'of the plan', 'review': 'of the candidate'}
    carried = lambda f: ', raised by an earlier review and not resolved in this one' if f.get('earlier') is True else ''
    out += ['- ' + plain(f.get('task'), 60) + ', ' + ('review ' + plain(f.get('review')) if f.get('review') is not None
                                                     else 'a review') + ' '
            + stage.get(f.get('stage'), plain(f.get('stage'))) + ' (round ' + plain(f.get('round')) + '), '
            + plain(f.get('id'), 40) + ' (' + plain(f.get('severity'), 20) + carried(f) + ')'
            + (' at ' + plain(f.get('location'), 200) if f.get('location') is not None else '') + ': '
            + plain(f.get('claim'), 300) for f in items]
    if more:
        out.append('- And ' + str(more) + ' more that are not repeated here; the review receipts have them.')
    if lost:
        out.append('- Not known for ' + lost + '; see "Problems with the evidence".')
    if not total and not lost:
        out.append('None: no review that ended in ACCEPT left a finding open.')
    return line, out + ['']


def wake_words(window):
    """The Outcome line on the host's hold against idle sleep, from the closeout's
    `wake`; no line for a closeout without one (an earlier shift, or no hold given)."""
    if 'wake' not in window:
        return []
    kept = window.get('wake')
    if not isinstance(kept, dict) or not isinstance(kept.get('means'), str) or not isinstance(kept.get('asked'), bool):
        return ["- Kept awake: not known; the closeout's entry on it could not be read."]
    means, sleep = plain(kept['means'], 200), 'the machine could idle-sleep, and the signed window ran on.'
    if not kept['asked'] or kept['means'].startswith('none'):
        return ['- ' + ('Kept awake: no' if not kept['asked'] else '**Kept awake: no**') + ' (' + means + '). The'
                + sleep[3:]]
    rows = lambda name: [r for r in kept.get(name) or [] if isinstance(r, dict)] if isinstance(kept.get(name), list) else []
    refusals, gaps = rows('refusals'), rows('gaps')
    counted = lambda name, seen: kept.get(name) if type(kept.get(name)) is int and kept.get(name) >= len(seen) else len(seen)
    # Nothing refused and no lapse: both lists are there and empty, and neither count says otherwise.
    nothing = all(kept.get(name) == [] and isinstance(kept.get(name), list)
                  and (kept.get(name + '_count') is None or (type(kept.get(name + '_count')) is int and kept[name + '_count'] == 0))
                  for name in ('refusals', 'gaps'))
    if kept.get('whole') is True and kept.get('held') is True and nothing:
        return ['- Kept awake: for the whole shift, by ' + means + '.']
    line = '- **Kept awake: NOT for the whole shift**, by ' + means + '.'
    line += ' Held from ' + plain(kept.get('held_from'), 40) + '.' if isinstance(kept.get('held_from'), str) else ' It was never held.'
    times = lambda n: str(n) + ' time' + ('' if n == 1 else 's')
    if refusals:
        said = ['at ' + plain(r.get('at'), 40) + ', ' + plain(r.get('what'), 200)
                + (' returned ' + str(r['code']) if type(r.get('code')) is int else '') for r in refusals]
        rest = counted('refusals_count', refusals) - len(refusals)
        line += (' Refused ' + times(counted('refusals_count', refusals)) + ': ' + '; '.join(said)
                 + ('; and ' + str(rest) + ' more' if rest else '') + '.')
    if gaps:
        said = ['from ' + plain(g.get('from'), 40) + ' to ' + plain(g.get('to'), 40) for g in gaps]
        rest = counted('gaps_count', gaps) - len(gaps)
        line += (' Lapsed ' + times(counted('gaps_count', gaps)) + ', when the host did not renew it in time (it was '
                 'held up, or the machine slept anyway): ' + '; '.join(said) + ('; and ' + str(rest) + ' more' if rest else '') + '.')
    if kept.get('held') is None and 'held' in kept:
        line += ' Whether it was held when this was written is not known.'  # the system could not be asked
    elif kept.get('held') is not True:
        line += ' It was not held when this was written.'
    return [line + ' While it was not held ' + sleep]


ACCEPTED = 'local acceptance recorded; retained review matches candidate'
UNBACKED = 'local acceptance recorded but uncorroborated'
UNSIGNED = 'retained grant without a signature file; not shown to be signed'
STAGE_WORDS = {
    'ack': "waiting for the builder's acknowledgment", 'plan': 'waiting for a plan',
    'plan_review': 'waiting for the review of the plan', 'build': 'waiting for the build',
    'review': 'waiting for the review of the candidate', 'repair_needed': 'a repair was needed',
    'accepted': 'accepted but not closed', 'closed': 'accepted and closed', 'blocked': 'blocked'}
PHASE_WORDS = {'ack': 'acknowledgment', 'plan': 'plan', 'plan_review': 'review of the plan',
               'build': 'build', 'review': 'review of the candidate', 'stop': 'seat stop'}


def plain(value, limit=TEXT_SHOWN):
    """Retained text as one inert line of Markdown.

    Whatever a seat wrote is data here: characters that are not printable
    (controls, line breaks, direction marks) become spaces, and the characters
    Markdown or HTML would act on are escaped, so the text can add no heading,
    link, image, emphasis or tag. Long text is cut; report.json has all of it.
    """
    text = value if isinstance(value, str) else receipts.encoded(value).decode().strip()
    text = ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())
    cut, text = len(text) > limit, text[:limit]
    if cut and ' ' in text[limit // 2:]:
        text = text[:text.rindex(' ')]  # end on a whole word where one ends in the second half
    # Pipes and tildes anywhere: a table row needs no leading pipe, and ~~ strikes text through.
    text = re.sub(r'[\\`*\[\]|~]', lambda m: '\\' + m.group(), text)
    # An underscore inside a word starts no emphasis; file and field names stay readable.
    text = re.sub(r'(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])', r'\\_', text)
    # The text may follow a list marker or sit in a quote, where a leading marker would
    # open a heading, list, quote or underline of its own.
    text = re.sub(r'^(\d{1,9})([.)])(?=\s|$)', r'\1\\\2', text)
    text = re.sub(r'^[#>+=-]', lambda m: '\\' + m.group(), text)
    return html.escape(text, quote=False) + (' (shortened; report.json has the rest)' if cut else '')


def get(node, *keys):
    """A nested value from retained JSON of any shape, or None."""
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def seconds_words(seconds):
    if type(seconds) is not int or seconds < 0:
        return 'an unknown time'
    hours, minutes, rest = seconds // 3600, seconds % 3600 // 60, seconds % 60
    parts = [str(n) + unit for n, unit in ((hours, 'h'), (minutes, 'm'), (rest, 's')) if n]
    return ' '.join(parts[:2]) or '0s'


def target_words(target):
    kind = get(target, 'kind')
    if kind == 'commit':
        return 'commit ' + plain(get(target, 'commit'))
    if kind == 'plan':
        return 'plan ' + plain(get(target, 'artifact', 'path'))
    return 'nothing recorded' if target is None else plain(target)


def asked_for_changes(item):
    """Words for a returned review that did nothing but ask for changes, or None.

    Only that case is an ordinary repair round: a review the validator blocked,
    every run of the review tool complete with exit status 0, and a verdict that
    is not ACCEPT or a finding left open. A duplicate, a failed review run, a
    blocked ACCEPT with nothing open, and a candidate review whose claimed
    fixes are not each reproduced (the validator blocks that for its own
    reasons) are not said to be one."""
    ref, data = item.get('reference') if isinstance(item.get('reference'), dict) else {}, item.get('data')
    if ref.get('stage') not in ('plan_review', 'review') or ref.get('status') != 'blocked' or not isinstance(data, dict):
        return None
    runs = data.get('invocations') if isinstance(data.get('invocations'), list) else []
    if not runs or any(get(run, 'status') != 'complete' or get(run, 'exit_status') != 0 for run in runs):
        return None
    findings = data.get('findings') if isinstance(data.get('findings'), list) else []
    still = sum(get(finding, 'disposition') == 'open' for finding in findings)
    if data.get('verdict') not in ('ACCEPT', 'CHANGES') or (data['verdict'] == 'ACCEPT' and not still):
        return None
    if ref.get('stage') == 'review':
        # As the validator reads them: every finding called fixed has a reproduction
        # that ran as expected and that the reviewer says reproduced it.
        ran = lambda run: (isinstance(run, dict) and run.get('outcome') == 'matched' and type(run.get('exit_status')) is int
                           and run.get('exit_status') == run.get('expected_exit'))
        reps = data.get('reproductions') if isinstance(data.get('reproductions'), list) else []
        fixed = {get(finding, 'id') for finding in findings if get(finding, 'disposition') == 'fixed'}
        if fixed != {get(r, 'finding_id') for r in reps} or any(
                not ran(get(r, 'candidate')) or get(r, 'assessment', 'status') != 'reproduced'
                or (get(r, 'baseline', 'kind') == 'run' and not ran(get(r, 'baseline', 'run'))) for r in reps):
            return None
    return 'verdict ' + plain(data.get('verdict')) + (', ' + str(still) + ' finding' + ('' if still == 1 else 's')
                                                      + ' still open' if still else '')


def hosted(policy):
    """True for the review policy of a grant with a reviewer seat: the host ran the
    reviews of that shift itself. An earlier grant names a review tool instead."""
    return isinstance(policy, dict) and 'review_choice' in policy and 'review_tool' not in policy


def family_words(policy):
    """The sentence for a review by the builder's own model family, or '' for any other."""
    if not hosted(policy) or policy.get('same_family_review') is not True:
        return ''
    from .signing import SAME
    why = SAME.get(policy.get('review_choice')) or 'for a reason the grant does not give'
    return ("same-family review: the reviewer is of the builder's own family (" + plain(policy.get('builder_family'))
            + '), ' + why)


def count_words(number, noun):
    return str(number) + ' ' + noun + ('' if number == 1 else 's')


def attempt_words(attempt):
    """One run of the reviewer, as the host recorded how it ended."""
    line = 'Attempt ' + plain(get(attempt, 'attempt')) + ': ' + plain(get(attempt, 'status'))
    if get(attempt, 'status') == 'complete':
        return line + ', ' + plain(get(attempt, 'verdict')) + '.'
    ended = plain(get(attempt, 'ended_by'))
    if get(attempt, 'exit_status') is not None:
        ended += ' with status ' + plain(get(attempt, 'exit_status'))
    if get(attempt, 'elapsed_seconds') is not None:
        ended += ', after ' + seconds_words(get(attempt, 'elapsed_seconds'))
    reasons = [r for r in get(attempt, 'reasons') or [] if isinstance(r, str)]
    line += ' (' + ended + ')' + (': ' + plain('; '.join(reasons), 400).rstrip('.') if reasons else '') + '.'
    if get(attempt, 'repository') == 'changed':
        line += " **the repository or the shift's receipts changed while this attempt ran**."
    return line


def review_words(review, kept):
    """The lines for one review the host ran: its outcome, what it found, and every
    attempt when there was more than one or one failed. `kept` is the task's
    receipts as read, for the reviewer's own words where the receipt is intact."""
    stage, outcome = get(review, 'stage'), get(review, 'outcome')
    line = ('Review ' + plain(get(review, 'review')) + ', '
            + {'plan_review': 'of the plan', 'review': 'of the candidate'}.get(stage, plain(stage))
            + ', round ' + plain(get(review, 'round')) + ': ' + plain(outcome) + ' on ' + target_words(get(review, 'target')))
    receipt = next((item for item in kept if get(item, 'reference', 'path') == get(review, 'receipt', 'path')
                    and get(review, 'receipt', 'path') is not None), None)
    data = receipt.get('data') if receipt and isinstance(receipt.get('data'), dict) else None
    attempts = [a for a in (get(data, 'attempts') if data else get(review, 'attempts')) or [] if isinstance(a, dict)]
    last = attempts[-1] if attempts else {}
    extra = []
    if outcome in ('ACCEPT', 'CHANGES'):
        count = get(last, 'findings_count') if data else len(get(review, 'findings') or [])
        line += '; ' + (count_words(count, 'finding') if count else 'no findings')
        settled = [plain(get(p, 'id')) for p in get(last, 'prior') or [] if get(p, 'resolved') is True]
        left = [plain(get(p, 'id')) for p in get(last, 'prior') or [] if get(p, 'resolved') is False]
        line += '; earlier findings resolved: ' + ', '.join(settled) if settled else ''
        line += '; earlier findings not resolved: ' + ', '.join(left) if left else ''
        line += '; ' + count_words(len(attempts), 'attempt') if len(attempts) > 1 else ''
        line += '.'
        if data:
            extra += [plain(get(f, 'id')) + ' (' + plain(get(f, 'severity')) + ') at ' + plain(get(f, 'location'), 200)
                      + ': ' + plain(get(f, 'claim'), 300) for f in get(last, 'findings') or []]
        else:
            extra += [plain(get(f, 'id')) + ' (' + plain(get(f, 'severity')) + ')' for f in get(review, 'findings') or []]
    elif outcome == 'FAILED':
        line += (': the reviewer gave nothing the host could take, in ' + count_words(len(attempts), 'attempt')
                 + '. No verdict was taken and no repair round was used.')
    elif outcome == 'lost':
        line += (': it was recorded as running by an earlier host process. Nothing is known of its end and nothing '
                 'was taken from it.')
    else:
        line += '.'
    if len(attempts) > 1 or any(get(a, 'status') != 'complete' for a in attempts):
        extra += [attempt_words(a) for a in attempts]
    if get(review, 'receipt', 'path'):
        line += ' Receipt: ' + plain(get(review, 'receipt', 'path')) + '.'
        if receipt is None or receipt.get('integrity') != 'matched':
            line += (' **Problem: the retained receipt is missing, changed, damaged or too large to repeat; see '
                     '"Problems with the evidence".**')
    else:
        line += ' No receipt of it is on record.'
    return [line] + ['  - ' + item for item in extra]


def unreviewed_words(task):
    """For a task left at a review stage: that its work was handed in and not reviewed, and why."""
    stage = task.get('stage')
    if stage not in ('plan_review', 'review'):
        return None
    target = task.get('candidate') if stage == 'review' else task.get('plan')
    reviews = [r for r in task.get('reviews') or [] if isinstance(r, dict)]
    last = reviews[-1] if reviews else None
    lead = '- Handed in and not reviewed: '
    if last and get(last, 'target') == target and get(last, 'round') == task.get('round'):
        if get(last, 'outcome') == 'FAILED':
            return (lead + 'the review of ' + target_words(target) + ' failed and was not run again, because the host '
                    'was stopping or the review no longer fitted in the working time left.')
        if get(last, 'outcome') == 'lost':
            return lead + 'the review of ' + target_words(target) + ' was lost with an earlier host process.'
    return (lead + 'the host started no review of ' + target_words(target) + '. In wind-down a review is started only '
            'when its whole signed limit fits in the working time left.')


def followed(item, kept):
    """The first later round whose receipt of the same stage satisfied the phase, is
    intact and is in the consumed ledger; None when the kept files show none."""
    ref = item.get('reference') if isinstance(item.get('reference'), dict) else {}
    rounds = [get(other, 'reference', 'round') for other in kept
              if get(other, 'reference', 'stage') == ref.get('stage') and get(other, 'reference', 'status') == 'satisfied'
              and other.get('integrity') == 'matched' and other.get('consumed') is True
              and type(get(other, 'reference', 'round')) is int and type(ref.get('round')) is int
              and get(other, 'reference', 'round') > ref['round']]
    return min(rounds) if rounds else None


def receipt_words(item, kept=()):
    """One line on one receipt the driver consumed: what the builder reported, attributed.
    `kept` is every receipt of the task, for what followed one that was returned."""
    ref, data = item.get('reference') if isinstance(item.get('reference'), dict) else {}, item.get('data')
    line = PHASE_WORDS.get(ref.get('stage'), plain(ref.get('stage'))) + ', round ' + plain(ref.get('round'))
    stage = ref.get('stage')
    changed = [c for c in item.get('changed') or [] if isinstance(c, str)]
    marked = set()
    if not isinstance(data, dict):
        # Nothing was read from it, so nothing is said about what it reported.
        return line + (': details unavailable. **Problem: the retained file is missing, changed, damaged or too '
                       'large to repeat; see "Problems with the evidence".**')
    if stage in ('plan_review', 'review'):
        findings = data.get('findings') if isinstance(data.get('findings'), list) else []
        runs = [i for i in data.get('invocations') or [] if isinstance(i, dict)]
        line += ': verdict ' + plain(data.get('verdict')) + ' on ' + target_words(data.get('target'))
        if runs:
            line += ', from ' + ', '.join(sorted({plain(i.get('tool')) + ' with model ' + plain(i.get('model'))
                                                 for i in runs}))
        line += '; ' + (str(len(findings)) + ' finding' + ('' if len(findings) == 1 else 's') if findings
                        else 'no findings')
        for finding in findings[:16]:
            line += '\n    - ' + plain(get(finding, 'id')) + ' (' + plain(get(finding, 'disposition')) + '): ' \
                    + plain(get(finding, 'reason'), 300)
    elif stage == 'build':
        checks = [c for c in data.get('checks') or [] if isinstance(c, dict)]
        passed = sum(c.get('exit_status') == c.get('expected_exit') for c in checks)
        line += ': commit ' + plain(data.get('commit')) + '; '
        line += (str(len(checks)) + ' check' + ('' if len(checks) == 1 else 's') + ' reported, ' + str(passed)
                 + ' with the expected exit status' if checks
                 else 'no checks reported: ' + plain(data.get('no_checks_reason')))
        for check in checks[:16]:
            line += '\n    - ' + plain(check.get('command'), 200) + ': exit ' + plain(check.get('exit_status')) \
                    + ', expected ' + plain(check.get('expected_exit'))
            output = get(check, 'output', 'path')
            if isinstance(output, str) and output in changed:
                # Each check whose kept output is gone is marked, not the last one for all.
                marked.add(output)
                line += ' **Problem: ' + plain(output) + ' is missing or no longer the bytes the receipt gives.**'
    elif stage == 'plan':
        line += ': ' + plain(get(data, 'plan', 'path')) + '. ' + plain(data.get('summary'), 300)
    elif stage in ('ack', 'stop'):
        line += ': ' + plain(data.get('status')) + '. ' + plain(data.get('reason'), 300)
    answers = [a for a in data.get('answers') or [] if isinstance(a, dict)] if stage in ('plan', 'build') else []
    if answers:
        # The builder's own answers; the next review is what checks them.
        line += "\n    - answers to the review's findings: " + ', '.join(
            plain(a.get('id')) + ' ' + plain(a.get('disposition')) for a in answers[:16])
    for copy in [k for k in item.get('kept') or [] if isinstance(k, dict)]:
        # `consumed` is an entry that satisfied the phase. Without one, when the copy was
        # made and for which receipt is not said: a returned receipt has copies of its own.
        line += ('\n    - ' + plain(copy.get('path'), PATH) + ' was written over or removed after the host took this '
                 "receipt; the host's copy from then is " + plain(copy.get('kept')) + ' and holds the bytes the receipt '
                 'gives.' if item.get('consumed') else
                 '\n    - ' + plain(copy.get('path'), PATH) + ' does not hold the bytes the receipt gives; the host holds '
                 'those bytes as ' + plain(copy.get('kept')) + '.')
    notes = []
    rest = [c for c in changed if c not in marked]
    if rest:
        notes.append(', '.join(plain(c) for c in rest) + (' are' if len(rest) > 1 else ' is')
                     + ' missing or no longer the bytes the receipt gives')
    elif item.get('integrity') != 'matched' and not changed:
        notes.append('the retained file is missing, changed or damaged')
    # `consumed` means a ledger entry that satisfied the phase. A receipt the driver
    # returned (a CHANGES review, say) has no such entry and is not missing one.
    returned = ref.get('status') not in (None, 'satisfied')  # the ledger keeps such a one as not satisfying
    if not item.get('consumed') and not returned:
        notes.append('no matching entry in the consumed ledger')
    asked = asked_for_changes(item) if returned else None
    if asked:
        # An ordinary round of the work, said as that, with what the kept files show followed.
        later, phase = followed(item, kept), PHASE_WORDS.get(stage, plain(stage))
        line += ('\n    - This review asked for changes (' + asked + '), so it did not advance the task. That is '
                 'what a repair round is for; it is not a refused command. '
                 + ('Round ' + str(later) + ' followed, and its ' + phase + ' satisfied the phase.' if later is not None
                    else 'No later round with a ' + phase + ' that satisfied the phase is recorded.'))
    elif returned:
        line += ('\n    - The driver returned this receipt (' + plain(ref.get('status'))
                 + '): it did not satisfy its phase and did not advance the task.')
    # On the receipt's own line: after its last sub-line it reads as being about that one.
    first, cut, below = line.partition('\n')
    return first + (' **Problem: ' + '; '.join(notes) + '.**' if notes else '') + cut + below


def turn_words(turn):
    if not isinstance(turn, dict):
        return 'unreadable turn record'
    exit_status = turn.get('exit')
    line = 'Turn ' + plain(turn.get('turn')) + ': ' + plain(turn.get('outcome'))
    line += ' with status ' + plain(exit_status) if exit_status is not None else ''
    if turn.get('elapsed_seconds') is not None:
        line += ', after ' + seconds_words(turn.get('elapsed_seconds'))
    if turn.get('receipts_changed') is not None:
        line += '; the receipts folder ' + ('changed' if turn['receipts_changed'] else 'did not change')
    # A turn that did not start is no process, so there is none to confirm stopped.
    if turn.get('reaped') is False and not str(turn.get('outcome')).startswith('not started'):
        line += '; **its process was not confirmed stopped**'
    return line + '. Log: ' + plain(turn.get('log'))


def turns_seen(task):
    """(seen to end, started) for the builder turns of a task. A row that says the
    turn did not start is no process; a turn still recorded as running is one
    that was started and not seen to end."""
    rows = [t for t in (task.get('turns') if isinstance(task.get('turns'), list) else [])
            if not (isinstance(t, dict) and str(t.get('outcome')).startswith('not started'))]
    seen = sum(isinstance(t, dict) and t.get('reaped') is True and t.get('ended') is not None for t in rows)
    return seen, len(rows) + (1 if task.get('turn_running') else 0)


def stop_words(task, headless):
    if str(task.get('cessation')).startswith('builder seat stop reported'):
        return ('the builder reported its seat stopped, in a receipt. That is its own statement, '
                'not something the host observed.')
    seen, started = turns_seen(task)
    if not headless or not started:
        return 'no stop receipt from the builder, so whether its seat stopped is not known.'
    # A headless builder is nothing but the processes the host started for it.
    if seen == started:
        return ('the builder ran headless. The host started ' + ('its one turn for this task and saw that process end'
                                                                 if started == 1 else 'each of its ' + str(started)
                                                                 + ' turns for this task and saw each process end')
                + ' (turns seen to end: ' + str(seen) + ' of ' + str(started) + ').')
    return ('the builder ran headless. Turns seen to end: ' + str(seen) + ' of ' + str(started) + '; whether the other'
            + ('' if started - seen == 1 else 's') + ' stopped is not known.')


def unbacked_words(task):
    """Which receipts of the task's last round do not back its acceptance, each named
    with what is wrong with it, and whether the review the acceptance names holds: a
    review receipt is called whole only when it is also the ACCEPT of the candidate.
    '' when the kept receipts show nothing wrong and the acceptance is unbacked for
    another reason."""
    wrong, whole = [], []
    for item in task.get('receipts') or []:
        ref = item.get('reference') if isinstance(item, dict) and isinstance(item.get('reference'), dict) else None
        if ref is None or ref.get('round') != task.get('round') or ref.get('stage') not in ('build', 'review'):
            continue
        if ref.get('status') not in (None, 'satisfied'):
            continue  # a receipt the driver returned backs nothing and is not what an acceptance rests on
        name = 'the ' + ref['stage'] + ' receipt ' + plain(ref.get('path'))
        changed = [c for c in item.get('changed') or [] if isinstance(c, str)]
        if changed:
            count = ('at least ' if len(changed) >= NAMED else '') + str(len(changed))  # the list stops at NAMED
            wrong.append(name + ' names ' + (count + ' files that are' if len(changed) > 1 else 'a file that is')
                         + ' missing or no longer the bytes it gives (' + ', '.join(plain(c) for c in changed) + ')')
        elif item.get('integrity') != 'matched':
            wrong.append(name + ' is missing, changed, could not be read or was too large to repeat')
        elif not item.get('consumed'):
            wrong.append(name + ' has no matching entry in the consumed ledger')
        elif ref['stage'] == 'review':
            whole.append(name)
    if not wrong:
        return ''
    line = '; '.join(wrong) + '.'
    # A review receipt already named as failing is not said a second time.
    named = any(w.startswith('the review receipt ') for w in wrong)
    if task.get('review_matched') is False and not named:
        line += ' The review receipt the acceptance names is not a kept, consumed ACCEPT of that candidate either.'
    elif whole and task.get('review_matched'):
        line += ' ' + whole[-1][:1].upper() + whole[-1][1:] + ' is intact and in the consumed ledger.'
    return line[:1].lower() + line[1:]


def task_words(task, headless=False, policy=None):
    """`policy` is the kept grant's review policy: with a reviewer seat in it the
    reviews are the host's and are worded as that."""
    accepted, recorded = task.get('acceptance') == ACCEPTED, task.get('acceptance') == UNBACKED
    host_reviews = hosted(policy)
    lines = ['### ' + plain(task.get('id')) + ' (' + plain(task.get('kind')) + '): '
             + ('accepted locally' if accepted else 'acceptance recorded, not backed by the kept evidence' if recorded
                else 'not accepted'), '']
    stage = task.get('stage')
    if accepted and host_reviews:
        lines.append("- Result: accepted locally. The host's review of " + target_words(task.get('candidate'))
                     + ' ended in ACCEPT, and the review receipt the host wrote for it is kept, intact and in the '
                     'consumed ledger. Nothing here shows it was pushed or merged.')
    elif recorded and host_reviews:
        lines.append('- Result: the task is recorded as accepted at ' + target_words(task.get('candidate'))
                     + ', but the kept evidence does not back it: '
                     + (unbacked_words(task) or 'a build or review receipt is missing, changed, not in the consumed '
                        "ledger, or is not the host's ACCEPT of that candidate.")
                     + ' Treat it as not accepted until someone has looked.')
    elif accepted:
        lines.append('- Result: the driver recorded acceptance of ' + target_words(task.get('candidate'))
                     + ', and the consumed review kept in the shift is an ACCEPT for exactly that candidate. '
                     'This is local acceptance; nothing here shows it was pushed or merged.')
    elif recorded:
        lines.append('- Result: the driver recorded acceptance of ' + target_words(task.get('candidate'))
                     + ', but the kept evidence does not back it: '
                     + (unbacked_words(task) or 'a build or review receipt is missing, changed, not in the consumed '
                        'ledger, or is not an ACCEPT of that candidate.')
                     + ' Treat it as not accepted until someone has looked.')
    else:
        lines.append('- Result: not accepted (' + plain(task.get('acceptance')) + '). It stopped at: '
                     + STAGE_WORDS.get(stage, plain(stage)) + '.')
        if task.get('candidate') is not None:
            lines.append('- Last candidate: ' + target_words(task.get('candidate')) + '.')
    if get(task, 'plan', 'artifact', 'path'):
        lines.append('- Plan: ' + plain(get(task, 'plan', 'artifact', 'path')))
    if task.get('round'):
        lines.append('- Repair rounds used: ' + plain(task.get('round')) + ' of 3.')
    if task.get('reason'):
        lines.append('- Last reason recorded on the task: ' + plain(task.get('reason')))
    receipts_kept = [r for r in task.get('receipts') or [] if isinstance(r, dict)]
    # A review receipt of the host's is not something the builder handed in.
    own = lambda item: host_reviews and get(item, 'reference', 'stage') in ('plan_review', 'review')
    handed = [r for r in receipts_kept if not own(r)]
    if handed:
        lines.append('- What the builder handed in, as the builder reported it:')
        lines += ['  - ' + receipt_words(item, receipts_kept) for item in handed]
    else:
        lines.append('- The builder handed in nothing the driver consumed.')
    if host_reviews:
        waiting = unreviewed_words(task)
        if waiting:
            lines.append(waiting)
        reviews = [r for r in task.get('reviews') or [] if isinstance(r, dict)]
        if reviews:
            lines.append('- Reviews, started by the host with the signed reviewer (' + plain(get(policy, 'review_agent'))
                         + ', model ' + plain(policy.get('review_model')) + ', family ' + plain(policy.get('review_family')) + '):')
            for review in reviews:
                first, *below = review_words(review, receipts_kept)
                lines += ['  - ' + first] + ['  ' + line for line in below]
    turns = task.get('turns') if isinstance(task.get('turns'), list) else []
    if turns:
        lines.append('- Builder turns: ' + str(len(turns)) + '.')
        lines += ['  - ' + turn_words(turn) for turn in turns]
    else:
        lines.append('- Builder turns: none recorded.')
    lines.append('- Builder stop: ' + stop_words(task, headless))
    return lines + ['']


def seat_words(role, seat, repo):
    if not isinstance(seat, dict):
        return ['- ' + role.capitalize() + ': not recorded.']
    lines = ['- ' + role.capitalize() + ': ' + plain(seat.get('agent')) + ', model ' + plain(seat.get('model'))
             + ', ' + plain(seat.get('mode')) + '.']
    try:
        from . import agents
        agents.seat_checked(role, seat)
        if isinstance(repo, str) and repo:
            lines.append('  - Command in the grant: ' + plain(shlex.join(agents.shown_command(seat, repo)), 1200))
        if role in ('builder', 'reviewer'):
            lines += ['  - ' + plain(line) for line in agents.reach(role, seat)]
    except (ValueError, KeyError, TypeError):
        pass  # an older or damaged seat record: the line above is all that is said
    return lines


def undone_words(blocker):
    """One blocker for "Not done": where the task stopped first, then any reason
    still recorded for it."""
    task, reason = get(blocker, 'task'), get(blocker, 'reason')
    if not task:
        return plain(reason, 1000).rstrip('.') + '.'
    stage = get(blocker, 'stage')
    line = 'Task ' + plain(task) + ': not accepted.'
    if stage is not None:
        line += ' It stopped at: ' + STAGE_WORDS.get(stage, plain(stage)) + '.'
    if reason and reason != 'acceptance missing':
        line += ' Last recorded reason: ' + plain(reason, 1000).rstrip('.') + '.'
    return line


def owner_words(blocker):
    """Who a "Not done" entry was with. For a task that is the owner recorded on the
    task while the shift ran, which is not the Outcome's "Next": that one says who
    has everything now that the shift is over."""
    owner = plain(get(blocker, 'next_owner'))
    if not get(blocker, 'task'):
        return ' Next owner: ' + owner + '.'
    return (' While the shift ran, the task was recorded as waiting on: ' + owner + '. The shift is over; '
            'who has it now is the "Next" line under Outcome.')


def worktree_words(leftovers):
    """Lines for "Not done" on linked working trees still registered under .sortie/."""
    linked = leftovers.get('worktrees') if isinstance(leftovers, dict) else None
    linked = [path for path in linked if isinstance(path, str)] if isinstance(linked, list) else []
    if not linked:
        return []
    return (['- Git working trees under .sortie/ are still registered in the repository (' + str(len(linked))
             + '; `git worktree list` shows them). A builder makes them for its checks and receipts point into '
             'them, so the host removes none. When you no longer need the evidence, remove '
             '.sortie/ and then run `git worktree prune` in the repository:']
            + ['  - ' + plain(path, 400) for path in linked])


def leftover_words(leftovers):
    """Lines for "Not done" on what was uncommitted in the repository at the end."""
    if not isinstance(leftovers, dict):
        return []
    if not leftovers.get('known'):
        return ['- What is uncommitted in the repository could not be read when the shift ended. Run git status '
                'there before relying on the working tree.']
    rows = [r for r in leftovers.get('new') or [] if isinstance(r, dict)]
    before = leftovers.get('before') if type(leftovers.get('before')) is int else 0
    since, complete = leftovers.get('since_start') is not False, leftovers.get('complete') is not False
    cut = ('The look at the repository was cut at a limit of time or size, so this is not the whole picture: '
           'run git status there.')
    already = (' ' + str(before) + ' more ' + ('was' if before == 1 else 'were') + ' already there when the shift '
               'began and ' + ('is' if before == 1 else 'are') + ' not listed.' if before else '')
    if not rows and not complete:
        return ['- Nothing new was seen in the part of the repository listing that was read. ' + cut]
    if not rows:
        return ['- Nothing uncommitted was seen in the repository when the shift ended'
                + (' that was not there when it began.' + already if before else '.')]
    lines = ['- ' + ('Left uncommitted in the repository, new since the shift began (' + str(len(rows)) + ').' if since
                     else 'Uncommitted in the repository when the shift ended (' + str(len(rows)) + '); whether they '
                     'were already there when the shift began is not known, because the look at the start failed or was cut.')
             + ' A turn that was stopped or gave up can leave half a change here; nothing below is part of an '
             'accepted commit:']
    lines += ['  - ' + plain(get(r, 'path'), 400) + ' (' + plain(get(r, 'state')) + ')' for r in rows[:50]]
    if len(rows) > 50:
        lines.append('  - and ' + str(len(rows) - 50) + ' more; report.json has them.')
    if already:
        lines.append('  - ' + already.strip())
    if not complete:
        lines.append('  - ' + cut)
    return lines


def head_words(seen):
    """One Outcome line on the repository's HEAD at the start and end of the host."""
    if not isinstance(seen, dict):
        return []
    start, end = (get(seen, key, 'head') for key in ('start', 'end'))
    known = [isinstance(get(seen, key), dict) and 'error' not in get(seen, key) for key in ('start', 'end')]
    if not all(known):
        return ['- Repository HEAD: ' + ' and '.join(
            ('when the shift began ' if key == 'start' else 'when it ended ')
            + (plain(get(seen, key, 'head') or 'no commit yet') if ok else 'it could not be read')
            for key, ok in zip(('start', 'end'), known)) + '.']
    if start == end:
        return ['- Repository HEAD: ' + plain(start or 'no commit yet') + ', the same when the shift began and when it ended.']
    return ['- Repository HEAD: ' + plain(start or 'no commit yet') + ' when the shift began, '
            + plain(end or 'no commit yet') + ' when it ended.']


def unclean_words(seen):
    """One Outcome line when the look at the start saw uncommitted paths."""
    rows = get(seen, 'start', 'changes') if isinstance(seen, dict) else None
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    # One path can be kept in two states (staged, and deleted from disk): it is one path.
    rows = list({str(get(r, 'path')): r for r in rows}.values())
    if not rows:
        return []
    names = ', '.join(plain(get(r, 'path'), 200) for r in rows[:10])
    return ['- The repository was not clean when the shift began: ' + str(len(rows))
            + (' or more' if get(seen, 'start', 'truncated') else '') + ' uncommitted path'
            + (' was' if len(rows) == 1 else 's were') + ' seen then (' + names
            + (', and ' + str(len(rows) - 10) + ' more' if len(rows) > 10 else '')
            + '). ' + held_words(seen, [str(get(r, 'path')) for r in rows])]


def held_words(seen, paths):
    """Whether a commit made during the shift holds the paths that were uncommitted
    at its start: from the host's look (closeout `repo.held`), and a guess said
    as a guess only when there was no look, it failed, or it was not finished."""
    held, between = seen.get('held'), ' between the HEAD at the start and the HEAD at the end '
    may = 'A commit made during the shift may hold them'
    if 'held' not in seen:
        return may + '.'  # a closeout from before the host looked
    if isinstance(held, dict) and isinstance(held.get('error'), str):
        return may + ': the look for that failed (' + plain(held['error'], 200) + ').'
    sound = (isinstance(held, dict) and isinstance(held.get('paths'), list) and type(held.get('complete')) is bool
             and all(isinstance(path, str) for path in held['paths']))
    found = [path for path in paths if path in held['paths']] if sound else []
    whole = sound and held['complete'] and get(seen, 'start', 'truncated') is False
    rest, one = len(paths) - len(found), len(paths) == 1
    if not found:
        return ('No commit' + between + 'holds ' + ('it.' if one else 'any of them.') if whole
                else may + ': the look for that was not finished.')
    if not rest and whole:
        return (('A commit' + between + 'holds it.') if one else
                'Commits' + between + 'hold ' + ('both' if len(paths) == 2 else 'all') + ' of them.')
    names = ', '.join(plain(path, 200) for path in found[:10]) + (', and ' + str(len(found) - 10) + ' more' if len(found) > 10 else '')
    other = 'the other' + ('' if rest == 1 else 's')
    return ('Commits' + between + 'hold ' + str(len(found)) + ' of them (' + names + ')'
            + (' and not ' + other + '.' if whole else '; whether they hold ' + (other if rest else 'more')
               + ' is not known: the look was not finished.'))


CHECK = 'ps -A -o pid,pgid,stat,command'  # shows every process with its group id


def unstarted(doc):
    """What the driver named with `finish` as never briefed: a list, empty when it
    said the direction held no more, or None when it gave no list."""
    try:
        return channels.unstarted(doc.get('not_started'))
    except (Refused, KeyError, TypeError):
        return None


def driver_notes(doc):
    """The notes the driver recorded with `note`: a list, or None when what is kept
    of them is not one or was left out of the report."""
    try:
        return channels.notes(doc.get('driver_notes'))
    except (Refused, KeyError, TypeError):
        return None


def notes_words(kept):
    """(the Outcome line, the section) for the driver's notes. The section is given
    only when there is something to list."""
    line = '- Notes the driver left: '
    try:
        notes = channels.notes(kept)
    except (Refused, KeyError, TypeError):
        return line + 'not known; they could not be read or were left out of this report.', []
    if not notes:
        return line + 'none.', []
    decisions = sum(n['kind'] == 'decision' for n in notes)
    parts = ([count_words(decisions, 'decision')] if decisions else []) + (
        [count_words(len(notes) - decisions, 'question')] if len(notes) > decisions else [])
    out = ['## Notes from the driver', '',
           "Each is the driver's own statement, recorded while the shift ran, under the kind the driver gave it: a "
           'decision it says it made on its own, or a question it says it could not settle. The host kept the text '
           'as written and checked none of it, also not whether a decision was within the direction. A note has no '
           'answer in the shift: the command that records it gives the driver nothing back but that it was kept.', '']
    for note in notes:
        out.append('- ' + ('Decision' if note['kind'] == 'decision' else 'Open question') + ', '
                   + ('task ' + plain(note['task_id'], 64) if note['task_id'] is not None else 'no task named')
                   + ', ' + plain(note['at'], 40) + ': ' + plain(note['text'], channels.NOTE_TEXT))
    return line + str(len(notes)) + ' (' + ', '.join(parts) + ', as the driver named them). They are listed under "Notes from the driver" below.', out + ['']


def tasks_words(doc, inert=plain):
    """How many of the briefed tasks were accepted locally, and which at what commit:
    one sentence. The host counts what the driver briefed and cannot read a
    direction, so the sentence says whose count it is and what the driver stated
    about the rest."""
    tasks = [t for t in doc.get('tasks') or [] if isinstance(t, dict)]
    left = unstarted(doc)
    rest = ('; whether the direction held more is not known to the host' if left is None
            else '; the driver names ' + str(len(left)) + (' more' if tasks else '') + ' as not started' if left
            else '; the driver says the direction held ' + ('no more' if tasks else 'nothing to brief')) + '.'
    if not tasks:
        return 'none briefed' + rest
    accepted = [t for t in tasks if t.get('acceptance') == ACCEPTED]
    unbacked = sum(t.get('acceptance') == UNBACKED for t in tasks)

    def named(task):
        commit = get(task, 'candidate', 'commit')
        return inert(str(task.get('id')), 60) + (' at ' + inert(commit[:7], 7) if isinstance(commit, str) and commit else '')
    names = ', '.join(named(t) for t in accepted[:5]) + (', and ' + str(len(accepted) - 5) + ' more' if len(accepted) > 5 else '')
    return (str(len(accepted)) + ' accepted locally of ' + str(len(tasks)) + ' briefed' + (' (' + names + ')' if accepted else '')
            + ('; ' + str(unbacked) + ' more recorded as accepted without backing evidence' if unbacked else '') + rest)


def shutdown_seen(window, doc=None):
    """What the closeout says of the host's own shutdown: (state, problems, group ids).

    State is True when every process the host started was seen to stop, the terminal
    was given back and cleanup finished; False when any of that is not so; None when
    the closeout was written before the host stopped anything, or there is none.
    The driver line is read as well as the `shutdown` entry, so a closeout whose
    entry says less than its driver line is not read as all clear. A closeout
    written before that entry existed is read by its driver line, its cleanup
    value, the note the earlier host put in the reason and, given the report
    `doc`, the builder turns recorded with an unconfirmed stop. An entry of the
    wrong shape is unknown, never confirmed. Group ids are kept only when they
    are plain numbers.
    """
    processes = window.get('processes') if isinstance(window.get('processes'), dict) else None
    if not processes:
        return None, [], []
    cleanup, driver = (v if isinstance(v, str) else '' for v in (processes.get('cleanup'), processes.get('driver')))
    if not cleanup.startswith(('done', 'incomplete')):
        return None, [], []
    derived = []
    if 'not observed' in driver or 'not confirmed' in driver:
        derived.append('the driver process was not seen to stop (' + driver[:300] + ')')
    if 'not restored' in driver:
        derived.append('the terminal could not be restored')
    if cleanup.startswith('incomplete'):
        derived.append('cleanup was ' + cleanup[:500])
    kept = processes.get('shutdown')
    if 'shutdown' not in processes:
        # An earlier host: it said so at the end of the reason, and in the turn's outcome.
        reason = window.get('reason') if isinstance(window.get('reason'), str) else ''
        turns = [turn for task in (doc or {}).get('tasks') or [] if isinstance(task, dict)
                 for turn in (task.get('turns') if isinstance(task.get('turns'), list) else []) if isinstance(turn, dict)]
        if any('stop not confirmed' in str(turn.get('outcome')) for turn in turns):
            derived.append('a builder turn was not seen to stop')
        elif 'a started process could not be confirmed stopped' in reason and not derived:
            derived.append('a started process could not be confirmed stopped')
        return not derived, derived, []
    listed = lambda name: kept.get(name) if isinstance(kept, dict) and isinstance(kept.get(name), list) else []
    problems = [p for p in listed('problems') if isinstance(p, str) and p.strip()][:8]
    groups = [g for g in listed('groups') if type(g) is int and 1 < g < 2 ** 31][:8]
    # The whole entry as the host writes it, or it is not an entry: a confirmation
    # is taken only from one that lists no problem and no group left to look for.
    whole = (isinstance(kept, dict) and isinstance(kept.get('confirmed'), bool)
             and all(isinstance(kept.get(name), list) for name in ('problems', 'groups'))
             and len(problems) == len(kept['problems']) and len(groups) == len(kept['groups'])
             and not (kept['confirmed'] and (problems or groups)))
    if not whole and not (isinstance(kept, dict) and kept.get('confirmed') is False):
        return (False, derived, []) if derived else (None, ["The closeout's shutdown entry could not be read."], [])
    if whole and kept['confirmed'] and not derived:
        return True, [], []
    return False, problems or derived or ['the closeout says so and does not say what'], groups


def shutdown_words(window, inert=plain, doc=None):
    """The Shutdown line in parts: (label, what is wrong, lead-in to a command, the command).

    The command is built here from numbers only; no kept text reaches it.
    """
    state, problems, groups = shutdown_seen(window, doc)
    if state:
        return 'confirmed', '', '', ''
    if state is None:
        early = isinstance(window.get('processes'), dict)
        return 'not recorded', (problems[0] if problems else 'The host had not stopped its processes when this was written; it writes the report '
                                'again after that.' if early else 'The host kept no record of stopping its processes.'), '', ''
    said = '; '.join(inert(p, 400) for p in problems)
    said = said[:1].upper() + said[1:] + '.'
    if all(p.startswith(('the terminal could not be restored', 'cleanup was ')) for p in problems):
        return 'NOT CLEAN', said, '', ''  # every process was seen to stop
    if groups:
        return ('NOT CONFIRMED', said, 'Check for leftover processes; no line printed means none are left: ',
                CHECK + " | awk '" + ' || '.join('$2 == ' + str(g) for g in groups) + "'")
    return 'NOT CONFIRMED', said, 'Check for leftover processes with: ', CHECK


RETURNED = 'receipt did not satisfy phase: blocked; '
ASKED = ('review requests changes', 'open review findings')  # the validator's words for a review asking for changes


def review_return(response):
    """True for the host's answer to a `consume` of a review that only asked for
    changes: `blocked`, the task left at `repair_needed`, and no other reason."""
    detail = get(response, 'detail')
    return (get(response, 'status') == 'blocked' and get(response, 'state') == 'repair_needed'
            and isinstance(detail, str) and detail.startswith(RETURNED)
            and all(reason in ASKED for reason in detail[len(RETURNED):].split('; ')))


def markdown(doc, sha):
    """report.md: the same retained facts as report.json, for a person to read."""
    template = files('sortie_kit').joinpath('data/templates/closing-report.md').read_text()
    grant = doc.get('grant') if isinstance(doc.get('grant'), dict) else {}
    window = doc.get('actual_window') if isinstance(doc.get('actual_window'), dict) else {}
    tasks = [t for t in doc.get('tasks') or [] if isinstance(t, dict)]
    issues, blockers = doc.get('issues') or [], doc.get('blockers') or []
    out = [template.replace('$SHIFT', plain(doc.get('shift_id'))).replace('$HASH', sha), '## Outcome', '']
    # Two facts a reader must not have to put together: what the shift produced, and
    # whether the host saw everything it started stop. Neither is folded into the other.
    label, wrong, lead, command = shutdown_words(window, doc=doc)
    unclear = 'the shutdown was ' + label.lower() if wrong and label != 'not recorded' else ''
    out.append('- Tasks: ' + tasks_words(doc))
    policy = grant.get('review_policy') if isinstance(grant.get('review_policy'), dict) else None
    if family_words(policy):
        out.append('- Reviews: ' + family_words(policy) + '.')
    out.append('- Shutdown: confirmed.' if label == 'confirmed' else '- Shutdown: not recorded. ' + wrong if not unclear
               else '- **Shutdown: ' + label + '.** ' + wrong + (' ' + lead + '`' + command + '`' if command else ''))
    status = ('no problem was found with the evidence that was read' if doc.get('status') == 'evidence summarized'
              # A bound of the report is said as that: the kept files are as they were.
              else 'partial. This report reached its own size limit and left some details out; see "Problems with '
                   'the evidence". Nothing that was read was found missing or damaged' if only_limits(issues)
              else 'partial. Some evidence is missing or damaged; see "Problems with the evidence"') + '.'
    out.append('- Status: ' + (unclear + '; see the line above. ' + status[:1].upper() + status[1:] if unclear else status))
    reason = window.get('reason') or doc.get('reason')
    out.append('- How it ended: ' + (plain(reason, 2000) if reason else 'not recorded.'))
    if 'elapsed_seconds' in window:
        out.append('- Ran for ' + seconds_words(window.get('elapsed_seconds')) + ', from ' + plain(window.get('start'))
                   + ' to ' + plain(window.get('end')) + '.')
        # The host counts on a clock that stands still while the machine sleeps; the two
        # stamps are of the wall clock. More than a minute between them is said.
        began, ended, counted = timestamp(window.get('start')), timestamp(window.get('end')), window.get('elapsed_seconds')
        if began and ended and type(counted) is int and (ended - began).total_seconds() - counted > SLEPT:
            apart = int((ended - began).total_seconds())
            out[-1] += (' **Those two times are ' + seconds_words(apart) + ' apart: for the other '
                        + seconds_words(apart - counted) + ' the machine was asleep or its clock was changed.**')
        out += idle_words(doc.get('last_activity'), window)
    else:
        out.append('- How long it ran is unknown: the host recorded no closeout.')
    if grant.get('until'):
        out.append('- Window in the kept grant: ' + plain(grant.get('start')) + ' to ' + plain(grant.get('until')) + '.')
        if grant.get('provenance') == UNSIGNED:
            out.append('- **No signature file is kept beside grant.json**: nothing here shows this grant was ever signed.')
    elif grant:
        out.append('- No signed grant: this shift was prepared and never signed.')
    out += wake_words(window)
    left_open, open_section = open_words(doc.get('open_findings'))
    out += [left_open] if left_open else []
    noted, notes_section = notes_words(doc.get('driver_notes'))
    out.append(noted)
    out += head_words(window.get('repo'))
    out += unclean_words(window.get('repo'))
    out.append('- Next: ' + plain(doc.get('next_owner')) + '.')
    out += ['', '## What was asked', '']
    asked = doc.get('direction') if isinstance(doc.get('direction'), dict) else None
    if asked:
        out.append("The direction, from the shift's own copy (direction.md in the shift folder, made from the file "
                   'given at the start; SHA256 ' + plain(asked.get('sha256')) + ')'
                   + ('' if asked.get('matches_grant') else ', **which does not match the kept grant**') + ':')
        out.append('')
        out += ['> ' + plain(line, 400) for line in str(asked.get('text')).splitlines()[:40] if line.strip()]
        if asked.get('shortened'):
            out += ['', 'Shortened here; direction.md in the shift has the full text.']
    else:
        out.append('The direction file could not be read.')
    out += ['', '## Tasks', '']
    headless = get(grant, 'seats', 'builder', 'mode') == 'headless'
    shown = dict(policy, review_agent=get(grant, 'seats', 'reviewer', 'agent')) if hosted(policy) else policy
    for task in tasks:
        out += task_words(task, headless, shown)
    if not tasks:
        out += ['No task was briefed.', '']
    out += open_section + notes_section
    out += ['## Not done', '']
    undone = ['- ' + undone_words(b) + owner_words(b) for b in blockers]
    left = unstarted(doc)
    # The driver's statement, not something the host saw: it cannot read a direction.
    undone += ['- Not started, as the driver stated it: ' + plain(item['name'], 200) + ': '
               + plain(item['reason'], 500).rstrip('.') + '.' for item in left or []]
    out += undone or ['No briefed task is recorded as left undone. ' + (
        'The host counts only the tasks the driver briefed; it does not know whether the direction held more.'
        if left is None else 'The driver stated that nothing in the direction was left unstarted.')]
    out += leftover_words(doc.get('leftovers')) + worktree_words(doc.get('leftovers'))
    actions = [a for a in doc.get('actions') or [] if isinstance(a, dict)]
    # A review that asked for changes is answered `blocked` too. It is a round of the
    # work and is counted apart from what the host refused.
    reviews = [a for a in actions if review_return(get(a, 'record', 'response'))]
    refused = [a for a in actions if get(a, 'record', 'response', 'status') in ('blocked', 'invalid') and a not in reviews]
    recorded = str(len(actions)) + ' command result' + (' is' if len(actions) == 1 else 's are') + ' recorded'
    out += ['', '## Driver commands', '']
    if reviews:
        out.append(recorded + '. ' + str(len(reviews)) + ' of them ' + ('is a review' if len(reviews) == 1 else 'are reviews')
                   + ' that asked for changes, which the host answers `blocked` so that a repair can follow; see the task:')
        out += ['- ' + plain(a.get('id')) + ': ' + plain(get(a, 'record', 'response', 'detail')) for a in reviews]
        rest = len(actions) - len(reviews)
        out += ['', 'Of the other ' + str(rest) + ', ' + (str(len(refused)) + (' was' if len(refused) == 1 else ' were')
                                                          + ' blocked or invalid:' if refused else 'none was blocked or invalid.')]
    else:
        out.append(recorded + '; ' + (str(len(refused)) + ' of them blocked or invalid:' if refused
                                      else 'none of them blocked or invalid.'))
    out += ['- ' + plain(a.get('id')) + ': ' + plain(get(a, 'record', 'response', 'status')) + ': '
            + plain(get(a, 'record', 'response', 'detail')) for a in refused]
    out += ['', 'An answer the host gave before it could record the command is not counted here.']
    out += ['', '## Problems with the evidence', '']
    out += ['- ' + plain(get(i, 'path')) + ': ' + plain(get(i, 'reason')) for i in issues] or [
        'None found in the kept files. The ' + unclear[4:] + '; see Outcome.' if unclear else 'None found.']
    out += ['', '## What the kept grant says about the seats', '']
    for role in ('driver', 'builder'):
        out += seat_words(role, get(grant, 'seats', role), grant.get('repo'))
    if hosted(policy):
        from .signing import CHOSEN
        # The reviewer is started in a directory the host makes for each run of a review.
        place = (grant.get('repo') + '/.sortie/shifts/' + str(doc.get('shift_id')) + '/reviews/REVIEW'
                 if isinstance(grant.get('repo'), str) and grant.get('repo') else None)
        out += seat_words('reviewer', get(grant, 'seats', 'reviewer'), place)
        limit = grant.get('review_limit_seconds')
        out.append('- How the reviewer was chosen: ' + plain(CHOSEN.get(policy.get('review_choice'), policy.get('review_choice')))
                   + '. REVIEW stands for the directory the host makes for each run of a review; one run may take at most '
                   + seconds_words(limit) + '.')
        if family_words(policy):
            out.append('- This is a ' + family_words(policy) + '.')
    elif policy:
        out.append('- Review choice in the grant: ' + plain(policy.get('review_tool')) + ' with model '
                   + plain(policy.get('review_model')) + '.')
    out.append("- These are the starts the kept grant describes, not a record that they happened. This report did not "
               "check the grant's signature again; sortie verify does. A seat started this way "
               "runs under the developer's own configuration of its agent CLI; the toolkit sets nothing beyond the "
               'command line and makes no statement about what a seat was able to reach.')
    out += ['', '## What the host saw of its processes', '']
    processes = window.get('processes') if isinstance(window.get('processes'), dict) else None
    if processes:
        # "Cleanup: done" is never printed beside a stop that was not confirmed.
        out.append('- Driver process: ' + plain(processes.get('driver') or 'not recorded') + '. '
                   + (label.capitalize() + ': ' + wrong[:1].lower() + wrong[1:] if unclear
                      else 'Cleanup: ' + plain(processes.get('cleanup')) + '.')
                   + ' Builder processes are the turns listed under each task'
                   + ('; the builder ran headless, so those processes are all there was of it. For the driver, a '
                      'process ending is not the seat saying it stopped.' if headless
                      else '. A process ending is not a seat saying it stopped.'))
    elif window.get('source'):
        out.append('- The closeout records no process observation.')
    else:
        out.append('- The host recorded no closeout, so this report has no observation of either seat being started '
                   'or stopped, apart from any builder turns listed under a task.')
    prior = doc.get('prior') if isinstance(doc.get('prior'), dict) else {}
    if prior.get('eligible'):
        out += ['', '## Compared with the earlier report ' + plain(prior.get('path')), '']
        out += ['- ' + plain(name) + ': ' + plain(get(cell, 'value')) for name, cell in sorted((doc.get('delta') or {}).items())]
    out += ['', '## Limits', '']
    out += ['- ' + plain(limit) for limit in doc.get('limits') or []]
    out += ['- report.json has the full detail, including the hash of every file read.', '']
    return '\n'.join(out).encode()


def rendered(doc, sha):
    """The readable report, or a short one saying it could not be written. Retained
    data of an unexpected shape must not cost the shift its report."""
    try:
        return markdown(doc, sha)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError) as exc:
        template = files('sortie_kit').joinpath('data/templates/closing-report.md').read_text()
        return (template.replace('$SHIFT', plain(doc.get('shift_id'))).replace('$HASH', sha)
                + '\n## Outcome\n\n- Status: ' + plain(doc.get('status')) + '.\n- Next: ' + plain(doc.get('next_owner'))
                + '.\n- The readable summary could not be written (' + plain(type(exc).__name__)
                + '); read report.json.\n').encode()


def write(view, *, previous=None):
    reader = Reader(view)
    doc = extract(view, previous=previous, reader=reader)
    raw = receipts.encoded(doc)
    text = rendered(doc, receipts.digest(raw))
    if max(len(raw), len(text)) > receipts.DOCUMENT_LIMIT and DETAIL_SMALL < DETAIL_LIMIT:
        # Too large with all the details the bound allows: once more with fewer, which
        # lists what it left out, before everything is dropped for the short form. The
        # second pass goes over the bytes the first one read.
        doc = extract(view, previous=previous, reader=reader.again(DETAIL_SMALL))
        raw = receipts.encoded(doc)
        text = rendered(doc, receipts.digest(raw))
    if max(len(raw), len(text)) > receipts.DOCUMENT_LIMIT:
        # Preserve a small, explicit damaged closeout rather than exceed the write bound.
        doc.update(status='partial', grant=unknown('output limit'), actual_window=unknown('output limit'),
                   tasks=[], actions=[], direction=None, next_owner='developer', inputs=[],
                   issues=[dict(path='report', reason='output limit exceeded; retained details omitted')],
                   blockers=[dict(task=None, reason='output limit; inspect retained evidence', next_owner='developer')],
                   metrics={k: unknown('output limit') for k in doc['metrics']},
                   delta={k: unknown('output limit') for k in doc['delta']})
        doc.pop('reason', None)
        doc['not_started'] = doc['driver_notes'] = doc['last_activity'] = None
        if doc.get('open_findings') is not None:
            doc['open_findings'] = dict(OPEN_UNKNOWN, items=[])  # the tasks are dropped, and a list of their findings with them
        raw = receipts.encoded(doc)
        text = rendered(doc, receipts.digest(raw))
    view.atomic('report.json', raw, replace=True)
    view.atomic('report.md', text, replace=True)
    return doc


def finalize(shift, seconds, *, previous=None, closeout=None):
    # Local developer inspection is not admission and cannot revive a shift.
    with channels.locked(shift, 'driver.lock', seconds=min(5, seconds), local_only=True) as view:
        if closeout is not None:
            view.atomic('closeout.json', receipts.encoded(closeout), replace=True)
        return write(view, previous=previous)
