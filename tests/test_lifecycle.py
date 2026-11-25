import contextlib
import copy
import datetime as dt
import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sortie_kit import channels, cli, receipts
from sortie_kit.bounds import Budget, Sources
from sortie_kit.builder_verification import BuilderHost, queue_check, run_command
from sortie_kit.host import run_shift
from sortie_kit.lifecycle import Driver
from sortie_kit.layout import Elapsed, Refused
from sortie_kit.receipt_schema import asset, decode
import test_receipts
from support import Clock, FakeSpawn, replaced_while_read


class LifecycleFixture(test_receipts.ReceiptTests):
    """Reuse receipt fixtures without inheriting their test methods below."""
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.value = dict(seats={r: {"agent": "fixture", "runtime": "codex"} for r in ("driver", "builder", "reviewer")},
                          review_policy=dict(builder_model="example-builder-model", builder_family="example-builder-family",
                                             review_model="example-review-model", review_family="example-review-family",
                                             same_family_review=False, review_choice="picked-other-family"),
                          review_limit_seconds=600,
                          allowed_actions=["local-work", "spawn-driver", "spawn-builder", "spawn-reviewer"],
                          finalization_seconds=10,
                          direction_sha256=hashlib.sha256((self.shift / "direction.md").read_bytes()).hexdigest(),
                          start=self.clock.utc().isoformat(),
                          until=(self.clock.utc() + dt.timedelta(seconds=1000)).isoformat())
        raw = receipts.encoded(self.value)
        (self.shift / "grant.json").write_bytes(raw)
        self.denial = ""
        budget = Budget(1000, clock=self.clock.now)
        self.admission = SimpleNamespace(shift=self.shift, raw=raw, value=self.value, now=self.clock.utc,
                                         budget=budget, trust=SimpleNamespace(repo=self.target),
                                         guard=lambda: self.denial or budget.guard(),
                                         seat_start=lambda role, prompt, cwd=None: ["fixture-agent", "run", role, prompt])
        self.spawn = FakeSpawn()
        self.driver = Driver(self.admission, self.spawn, seconds=1000, wind_down=10)
        self.counter = 0

    def session_time_by_the_clock(self):
        """While the block runs, the time of every session and lock is counted by the
        fixture's clock, which moves only when the test moves it. Without this a lock has
        its seconds on the machine's clock, and what a test sees then depends on how long
        the machine took. Not for a test in which a lock is held by someone else: the wait
        for it would not end."""
        return patch("sortie_kit.receipts.Budget", lambda seconds: Budget(seconds, clock=self.clock.now))

    def state(self):
        return decode((self.shift / "driver-state.json").read_bytes())

    def task_state(self):
        state = self.state()
        return state["tasks"][state["active"]]

    def action(self, op, **data):
        self.counter += 1
        cmd = dict(version=1, id="command-" + str(self.counter), op=op)
        if op not in ("brief", "finish", "note"):
            task = self.task_state()
            cmd.update(task_id=task["id"], request_id=task["request_id"])
        cmd.update(data)
        return self.driver.apply(cmd)

    def end_turn(self, code=0):
        """The builder process of the running turn exits; the host records it."""
        self.spawn.child.code = code
        self.driver.poll_turn()
        self.assertIsNone(self.state()["turn"])

    def begin(self, kind="feature", name="task", running=False):
        result = self.action("brief", task_id=name, kind=kind, brief=self.artifact("briefs/task.md"), next_owner="builder")
        self.assertEqual(result["status"], "recorded", result)
        if not running:
            self.end_turn()
        return self.task_state()

    def phase_receipt(self, *, target=None, answers=None):
        task = self.task_state()
        expected = self.driver.expected(task, target)
        value = decode(asset(expected["phase"] + "-v3.json"))
        for key in receipts.COMMON:
            value[key] = expected[key]
        value["receipt_id"] = "receipt-" + task["request_id"]
        data = value["data"]
        if expected["phase"] in ("plan", "build"):
            data["answers"] = answers if answers is not None else [
                dict(id=key, disposition="fixed", reason="Done as the finding says.", evidence=None)
                for key in expected["required_findings"]]
        if expected["phase"] == "plan":
            data["plan"] = target["artifact"]
        elif expected["phase"] == "build":
            data["commit"] = target["commit"]
            data["checks"][0]["output"] = self.artifact("logs/tests.log", "tests pass\n")
        else:
            data["evidence"] = [self.artifact("logs/ack.txt", "acknowledged\n")]
        path = "receipts/" + value["receipt_id"] + ".json"
        (self.shift / path).write_bytes(receipts.encoded(value))
        return value, path

    def review_output(self, *, changes=False, new="N", resolved=True):
        """What a reviewer that keeps the contract prints for the review that runs now."""
        review = self.state()["review"]
        findings = [self.finding(new + str(review["review"]))] if changes else []
        record = self.record(review["target"], "CHANGES" if changes else "ACCEPT", findings,
                             [dict(id=item["id"], resolved=resolved, reason="Checked against the patch.")
                              for item in review["prior"]])
        return self.said(record, review["running"]["marker"])

    def end_review(self, text=None, *, code=0, changes=False, **kwargs):
        """The reviewer process of the running attempt prints `text` and exits; the
        host reads its output back, checks it and records what it gave."""
        self.prints((self.review_output(changes=changes, **kwargs) if text is None else text).encode())
        self.spawn.child.code = code
        self.driver.poll_turn()
        return self.state()

    def prints(self, data, name="stdout"):
        """The reviewer process that runs now prints `data`: it is what the host reads from
        the pipe it holds, and the host itself puts it into the log."""
        import tempfile
        stream = tempfile.TemporaryFile()
        stream.write(data)
        stream.seek(0)
        setattr(self.spawn.child, name, stream)
        self.addCleanup(stream.close)

    def consume_phase(self, *, changes=False):
        """Hand in the receipt the stage waits for; at a review stage, end the review
        the host runs. Either way the answer is what the driver would be told."""
        task = self.task_state()
        if task["stage"] in ("plan_review", "review"):
            if self.state()["review"] is None:
                started = self.action("continue")
                self.assertEqual(started["status"], "recorded", started)
            last = self.end_review(changes=changes)["last_review"]
            return dict(status="advanced" if last["outcome"] == "ACCEPT" else "blocked", detail=last["detail"])
        target = ({"kind": "plan", "artifact": self.artifact("briefs/plan.md", "Plan\n")} if task["stage"] == "plan"
                  else {"kind": "commit", "commit": self.candidate} if task["stage"] == "build" else None)
        _, path = self.phase_receipt(target=target)
        return self.action("consume", receipt=path, **({"target": target} if target else {}))

    def reach_review(self, kind="defect"):
        """Up to a review of the candidate that the host has started and that still runs."""
        self.begin(kind)
        while self.task_state()["stage"] != "review":
            result = self.consume_phase()
            self.assertEqual(result["status"], "advanced", result)


# Inherit fixture helpers, not the unrelated receipt suite a second time.
for _name in tuple(vars(test_receipts.ReceiptTests)):
    if _name.startswith('test_'):
        setattr(LifecycleFixture, _name, None)


class LifecycleTests(LifecycleFixture):
    def test_signed_models_reach_context_and_driver_cannot_widen_review(self):
        self.begin()
        context = decode((self.shift / "current-context.json").read_bytes())
        signed = dict(agent="fixture", model="example-review-model", family="example-review-family",
                      same_family=False, choice="picked-other-family")
        self.assertEqual((context["version"], context["reviewer"], context["findings"]), (3, signed, None))
        self.assertEqual(context["builder"], dict(model="example-builder-model", family="example-builder-family"))
        self.assertIsNone(context["expected"]["review"])  # acknowledgment is not a review phase
        task = self.task_state()
        task.update(stage="review", candidate={"kind": "commit", "commit": self.candidate})
        expected = self.driver.review_expected(task)
        self.assertEqual((expected["version"], expected["role"], expected["implementer_family"], expected["review"]),
                         (3, "reviewer", "example-builder-family", signed))
        self.assertNotIn("fallback", expected)
        result = self.driver.apply(dict(version=1, id="widen", op="brief", task_id="second", kind="feature",
                                        brief=self.artifact("briefs/task.md"), next_owner="builder",
                                        fallback=dict(tool="review", entrypoint="/x/review/SKILL.md", model="m")))
        self.assertEqual(result["status"], "invalid", result)
        self.assertIn("unknown coordination fields", result["detail"])

    def test_complete_feature_and_defect_with_separate_phase_ids(self):
        before = (self.target / "source.txt").read_bytes()
        with patch('sortie_kit.builder_verification.run_command', side_effect=AssertionError('driver must never execute')):
            for kind in ('feature', 'defect'):
                self.begin(kind, kind)
                requests = []
                while self.task_state()['stage'] != 'accepted':
                    requests.append(self.task_state()['request_id'])
                    result = self.consume_phase()
                    self.assertEqual(result['status'], 'advanced', result)
                self.assertEqual(len(requests), len(set(requests)))
                self.assertEqual(self.action('close')['status'], 'closed')
        self.assertEqual((self.target / 'source.txt').read_bytes(), before)
        self.driver.finish('finished selected work')
        report = (self.shift / 'report.md').read_text()
        self.assertIn("The host's review of commit", report)
        self.assertIn('ended in ACCEPT', report)
        self.assertNotIn('not backed by the kept evidence', report)
        self.assertIn('no stop receipt from the builder', report)

    def test_receipt_after_acceptance_does_not_reopen_the_task(self):
        self.reach_review()
        self.assertEqual(self.consume_phase()['status'], 'advanced')
        prior = self.task_state()
        result = self.action('consume', receipt='receipts/missing.json')
        self.assertEqual(result['status'], 'invalid')
        self.assertEqual(self.task_state(), prior)
        self.assertEqual(self.action('close')['status'], 'closed')

    def test_one_task_and_stable_identity_do_not_reset_round(self):
        self.begin()
        self.assertEqual(self.action('brief', task_id='another', kind='defect', brief=self.artifact('briefs/other.md'), next_owner='builder')['status'], 'invalid')
        self.assertEqual((len(self.state()['tasks']), self.state()['mode'], len(self.spawn.started)), (1, 'active', 1))

    def test_shared_repair_cap_and_builder_returned_changes(self):
        self.reach_review()
        for round_ in range(4):
            self.assertEqual(self.task_state()['round'], round_)
            self.assertEqual(self.consume_phase(changes=True)['status'], 'blocked')
            repaired = self.action('repair', reason='Address F1', next_owner='builder')
            if round_ == 3:
                self.assertEqual(repaired['status'], 'blocked')
                break
            self.assertEqual(repaired['status'], 'advanced')
            self.assertEqual(self.task_state()['required']['candidate'], [dict(id='N' + str(round_ + 1), severity='major')])
            self.assertEqual(self.consume_phase()['status'], 'advanced')
        self.assertIsNone(self.state()['active'])
        self.assertEqual(self.state()['tasks']['task']['round'], 3)
        self.assertIn('exhausted', self.state()['tasks']['task']['reason'])
        started = len(self.spawn.started)
        self.assertEqual(self.action('brief', task_id='task', kind='defect', brief=self.artifact('briefs/task.md'), next_owner='builder')['status'], 'invalid')
        # The same identity is refused and keeps its count; nothing was started for it.
        self.assertEqual((self.state()['tasks']['task']['round'], self.state()['tasks']['task']['stage'], len(self.spawn.started)),
                         (3, 'blocked', started))

    def test_missing_receipt_and_diagnostic_never_advance(self):
        self.begin()
        # A path with no file is the driver's to correct: no repair round is spent on it.
        self.assertEqual(self.action('consume', receipt='receipts/missing.json')['status'], 'invalid')
        self.assertEqual((self.task_state()['stage'], self.task_state()['round']), ('ack', 0))
        _, path = self.phase_receipt()
        fake = dict(status='diagnostic', satisfies_phase=False, recorded=False, sha256=hashlib.sha256((self.shift/path).read_bytes()).hexdigest(), reasons=[])
        with patch('sortie_kit.lifecycle.receipts.validate_receipt', return_value=fake):
            self.assertEqual(self.action('consume', receipt=path)['status'], 'blocked')
        self.assertEqual(self.task_state()['stage'], 'repair_needed')

    def replaced_expected_review(self, policy):
        # A builder may write briefs/. Replacing the expected context the host published
        # for its own review receipt, before the check reads it, must not substitute
        # the signed reviewer: the review then backs nothing.
        self.reach_review()
        real, seen = receipts.validate_receipt, []

        def replace(shift, receipt, expected_path, **kwargs):
            target = self.shift / expected_path
            forged = decode(target.read_bytes())
            forged["review"] = policy
            target.write_bytes(receipts.encoded(forged))
            seen.append(receipt)
            return real(shift, receipt, expected_path, **kwargs)

        with patch('sortie_kit.lifecycle.receipts.validate_receipt', side_effect=replace):
            state = self.end_review()
        self.assertEqual(len(seen), 1)
        self.assertEqual((state['last_review']['outcome'], state['active'], state['review']), ('FAILED', None, None))
        self.assertIn('expected context changed', state['last_review']['detail'])
        task = state['tasks']['task']
        self.assertEqual(task['stage'], 'blocked')
        self.assertIsNone(task['accepted'])
        self.assertNotIn('review', [r['stage'] for r in task['receipts']])
        ledger = decode((self.shift / 'receipts/.consumed.json').read_bytes())
        self.assertFalse([e for e in ledger['entries'] if e['receipt_id'].startswith('review-')])

    def test_replaced_expected_context_cannot_name_another_reviewer(self):
        self.replaced_expected_review(dict(agent="fixture", model="unsigned-model", family="unsigned-family",
                                           same_family=False, choice="given"))

    def test_replaced_expected_context_cannot_grant_same_family_review(self):
        self.replaced_expected_review(dict(agent="fixture", model="unsigned-model", family="example-builder-family",
                                           same_family=True, choice="picked-same-family"))

    def test_plan_repair_does_not_require_plan_findings_in_candidate(self):
        self.begin()
        self.consume_phase(); self.consume_phase()
        self.assertEqual(self.consume_phase(changes=True)['status'], 'blocked')
        self.action('repair', reason='Fix plan', next_owner='builder')
        self.assertEqual(self.task_state()['round'], 1)
        self.consume_phase(); self.consume_phase()
        self.assertEqual(self.task_state()['required']['plan'], [])
        self.assertEqual(self.task_state()['required']['candidate'], [])

    def test_target_swap_and_phase_command_mismatch_refuse(self):
        self.begin('defect')
        _, path = self.phase_receipt()
        result = self.action('consume', receipt=path, target={'kind':'commit','commit':self.base})
        self.assertEqual(result['status'], 'invalid')
        self.assertIn('cannot be rebound', result['detail'])
        self.assertEqual(self.task_state()['stage'], 'ack')
        while self.task_state()['stage'] != 'review':
            self.consume_phase()
        # The review's receipt is the host's own: the driver hands none in and binds no target.
        result = self.action('consume', receipt=path, target={'kind':'commit','commit':self.base})
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('a review is running', result['detail'])
        self.assertEqual((self.task_state()['stage'], self.task_state()['candidate']),
                         ('review', {'kind': 'commit', 'commit': self.candidate}))

    def test_each_recorded_command_keeps_the_hosts_time_and_a_state_without_one_still_loads(self):
        self.clock.sleep(61.7)
        self.begin()
        record = self.state()["commands"]["command-1"]
        self.assertEqual(sorted(record), ["at", "digest", "response"])
        self.assertEqual(record["at"], "2030-01-01T00:01:01+00:00")
        # A state kept by a host from before the time was recorded has none.
        state = self.state()
        del state["commands"]["command-1"]["at"]
        (self.shift / "driver-state.json").write_bytes(receipts.encoded(state))
        self.assertEqual(self.action("note", kind="decision", text="went on")["status"], "recorded")
        self.assertEqual(sorted(self.state()["commands"]["command-1"]), ["digest", "response"])
        for bad in (7, "", "yesterday", "2030-01-01T00:01:01", "9999-12-31T23:59:59-23:59", "2" * 41):
            with self.subTest(bad=bad):
                state["commands"]["command-1"]["at"] = bad
                (self.shift / "driver-state.json").write_bytes(receipts.encoded(state))
                answer = self.action("note", kind="decision", text="went on again")
                self.assertEqual((answer["status"], answer["detail"]), ("invalid", "invalid command record"))

    def test_pending_intent_and_command_replay_fail_closed(self):
        command=dict(version=1,id='stable',op='brief',task_id='task',kind='feature',brief=self.artifact('briefs/task.md'),next_owner='builder')
        self.assertEqual(self.driver.apply(command)['status'], 'recorded')
        self.assertEqual(self.driver.apply(command)['status'], 'replayed')
        self.assertEqual(len(self.spawn.started),1)
        self.end_turn()
        command['kind']='defect'
        self.assertEqual(self.driver.apply(command)['status'],'invalid')
        state=self.state();state['pending']={'id':'crashed','digest':'0'*64,'op':'consume'}
        (self.shift/'driver-state.json').write_bytes(receipts.encoded(state))
        self.assertEqual(self.action('continue')['status'],'blocked')
        self.assertEqual(len(self.spawn.started),1)
        self.assertIn('unresolved', (self.shift/'report.md').read_text())

    def test_expiry_after_consumption_blocks_and_finalizes_without_starting_agents(self):
        self.begin()
        _,path=self.phase_receipt()
        real=receipts.validate_receipt
        def expire(*args,**kwargs):
            result=real(*args,**kwargs);self.clock.sleep(1001);return result
        with patch('sortie_kit.lifecycle.receipts.validate_receipt',side_effect=expire):
            self.assertEqual(self.action('consume',receipt=path)['status'],'blocked')
        before=len(self.spawn.started)
        self.driver.finish('expired')
        self.assertEqual(len(self.spawn.started),before)
        self.assertIn('acceptance missing',(self.shift/'report.md').read_text())

    def test_finalization_obeys_signed_bound_after_expiry(self):
        self.begin()
        self.clock.sleep(1011)
        self.driver.finish('too late')
        self.assertFalse((self.shift/'report.md').exists())

    def test_finish_command_rechecks_window_after_lock_wait(self):
        self.begin()
        before = (self.shift / 'driver-state.json').read_bytes()
        self.clock.sleep(1009.75)
        actual_lock, waits = channels.locked, []
        @contextlib.contextmanager
        def delayed_lock(*args, **kwargs):
            waits.append(kwargs.get('seconds', 10))
            with actual_lock(*args, **kwargs) as view:
                self.clock.sleep(1)
                yield view
        with patch('sortie_kit.lifecycle.channels.locked', side_effect=delayed_lock):
            result = self.action('finish', reason='done', next_owner='operator')
        self.assertEqual(result['status'], 'invalid', result)
        self.assertEqual((self.shift / 'driver-state.json').read_bytes(), before)
        self.assertFalse((self.shift / 'report.md').exists())
        self.assertLessEqual(waits[0], .25)

    def test_expired_finalization_never_publishes_completed_command(self):
        self.begin()
        self.clock.sleep(1009)
        dispatch = self.driver.dispatch
        def expire_after_dispatch(*args):
            result = dispatch(*args)
            self.clock.sleep(2)
            return result
        with patch.object(self.driver, 'dispatch', side_effect=expire_after_dispatch):
            result = self.action('finish', reason='done', next_owner='operator')
        # Not `invalid`: the intent is on file and the next command will block on it,
        # so the driver is told to hand off, not to correct and resend.
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('could not be recorded', result['detail'])
        self.assertIsNotNone(self.state()['pending'])
        self.assertEqual(self.state()['mode'], 'active')
        self.assertFalse((self.shift / 'report.md').exists())

    def test_expired_host_can_finalize_within_the_signed_window(self):
        self.clock.sleep(1001)
        code, why = run_shift(self.admission, self.spawn, seconds=1, wind_down=.1)
        self.assertEqual(code, 3)
        self.assertEqual(self.spawn.started, [])
        self.assertTrue((self.shift/'report.md').exists())

    def default_driver(self, seconds):
        self.admission.budget.end = self.clock.now() + seconds
        self.value['start'] = self.clock.utc().isoformat()
        self.value['until'] = (self.clock.utc() + dt.timedelta(seconds=seconds)).isoformat()
        self.admission.raw = receipts.encoded(self.value)
        (self.shift / 'grant.json').write_bytes(self.admission.raw)
        self.driver = Driver(self.admission, self.spawn, seconds=seconds)

    def test_short_shift_winds_down_for_its_last_third(self):
        self.default_driver(600)
        self.assertEqual(self.driver.wind_down, 200)
        self.assertEqual(self.driver.active_seconds(), 400)
        result = self.action('brief', task_id='short', kind='defect',
                             brief=self.artifact('briefs/task.md'), next_owner='builder')
        self.assertEqual(result['status'], 'recorded', result)
        self.assertEqual(self.task_state()['turns'], [])
        self.assertEqual(self.state()['turn']['limit_seconds'], 590)  # through wind-down, to ten seconds before the end
        self.end_turn()
        self.clock.sleep(400)
        result = self.action('continue')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('wind-down', result['detail'])

    def note(self, **data):
        return self.action('note', **dict(dict(kind='decision', text='I chose the first reading.'), **data))

    def test_a_note_of_the_drivers_is_kept_and_changes_nothing_else(self):
        # Before any task, with no task named.
        result = self.note(kind='question', text='Does "the prior window" mean the same number of days?')
        self.assertEqual(result['status'], 'recorded', result)
        self.assertIn('a note gets no answer in the shift', result['detail'])
        self.assertEqual(self.spawn.started, [])
        self.action('brief', task_id='task', kind='feature', brief=self.artifact('briefs/task.md'), next_owner='builder')
        before = self.task_state()
        # While a builder turn runs, about that task.
        self.clock.sleep(7)
        result = self.note(task_id='task')
        self.assertEqual(result['status'], 'recorded', result)
        self.assertEqual(self.task_state(), before)
        state = self.state()
        # The time is the host's, written as every time the report shows is.
        import datetime
        first = datetime.datetime.fromisoformat(state['notes'][0]['at'])
        self.assertEqual((first.utcoffset(), first.microsecond), (datetime.timedelta(0), 0))
        self.assertEqual(state['notes'], [
            dict(kind='question', text='Does "the prior window" mean the same number of days?', task_id=None, at=first.isoformat()),
            dict(kind='decision', text='I chose the first reading.', task_id='task',
                 at=(first + datetime.timedelta(seconds=7)).isoformat())])
        self.assertEqual((state['mode'], state['active'], len(self.spawn.started)), ('active', 'task', 1))
        # The same command again repeats nothing.
        again = dict(version=1, id='command-' + str(self.counter), op='note', kind='decision', text='I chose the first reading.', task_id='task')
        self.assertEqual(self.driver.apply(again)['status'], 'replayed')
        self.assertEqual(len(self.state()['notes']), 2)
        # The same id with other words is refused, as for every command.
        self.assertEqual(self.driver.apply(dict(again, text='I chose the second reading.'))['status'], 'invalid')
        self.assertEqual(len(self.state()['notes']), 2)
        # A task_id of null is a note about no task, as when it is left out.
        self.assertEqual(self.note(text='About no task.', task_id=None)['status'], 'recorded')
        self.assertEqual(self.state()['notes'][2]['task_id'], None)

    def test_a_note_that_is_not_one_is_invalid_and_keeps_nothing(self):
        self.action('brief', task_id='task', kind='feature', brief=self.artifact('briefs/task.md'), next_owner='builder')
        for bad in (dict(kind='remark'), dict(text=' '), dict(text='x' * 1001), dict(text=7), dict(task_id='never-briefed'),
                    dict(task_id=7), dict(request_id='0' * 32)):
            with self.subTest(bad=bad):
                result = self.note(**bad)
                self.assertEqual(result['status'], 'invalid', result)
                self.assertEqual(self.state()['notes'], [])
        self.assertEqual(self.task_state()['stage'], 'ack')

    def test_notes_are_taken_in_wind_down_and_stop_at_thirty_two(self):
        self.default_driver(600)
        self.clock.sleep(400)
        self.assertLessEqual(self.driver.active_seconds(), 0)
        for number in range(32):
            self.assertEqual(self.note(text='note %d' % number)['status'], 'recorded')
        result = self.note(text='one too many')
        # Not `invalid`: that answer asks for the command again under a new id.
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('32', result['detail'])
        self.assertNotIn('send it again', result['detail'])
        self.assertEqual([n['text'] for n in self.state()['notes']], ['note %d' % n for n in range(32)])
        self.assertEqual(self.action('finish', reason='done', next_owner='developer')['status'], 'closed')

    def test_a_state_from_a_host_without_notes_loads_and_one_with_bad_notes_does_not(self):
        self.note()
        state = self.state(); del state['notes']
        (self.shift / 'driver-state.json').write_bytes(receipts.encoded(state))
        self.assertEqual(self.note(text='after')['status'], 'recorded')
        self.assertEqual([n['text'] for n in self.state()['notes']], ['after'])
        state = self.state(); state['notes'][0]['kind'] = 'remark'
        (self.shift / 'driver-state.json').write_bytes(receipts.encoded(state))
        self.assertNotEqual(self.note(text='more')['status'], 'recorded')
        # As every task a state names, the task of a note is one of the state's.
        state['notes'][0].update(kind='decision', task_id='never-briefed')
        (self.shift / 'driver-state.json').write_bytes(receipts.encoded(state))
        self.assertNotEqual(self.note(text='more')['status'], 'recorded')
        state['notes'][0]['task_id'] = None
        good = state['notes'][0]['at']
        # A time that cannot be brought to UTC is a state that does not load, not an error of the host's.
        for at in ('0001-01-01T00:00:00+00:01', '9999-12-31T23:59:59-00:01'):
            state['notes'][0]['at'] = at
            (self.shift / 'driver-state.json').write_bytes(receipts.encoded(state))
            self.assertIn(self.note(text='more')['status'], ('invalid', 'blocked'), at)
        state['notes'][0]['at'] = good
        (self.shift / 'driver-state.json').write_bytes(receipts.encoded(state))
        self.assertEqual(self.note(text='more')['status'], 'recorded')

    def test_wind_down_is_twenty_minutes_or_a_third_of_a_shorter_window(self):
        from sortie_kit.bounds import wind_down
        self.assertEqual([wind_down(s) for s in (30, 600, 2700, 3600, 14400)], [10, 200, 900, 1200, 1200])
        with self.assertRaises(ValueError):
            wind_down(0)

    def test_hour_shift_defaults_to_wind_down_at_forty_minutes(self):
        self.default_driver(3600)
        self.clock.sleep(2399)
        self.driver.require_live(new_work=True)
        self.clock.sleep(1)
        result = self.action('brief', task_id='late', kind='defect',
                             brief=self.artifact('briefs/task.md'), next_owner='builder')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('wind-down', result['detail'])
        self.assertEqual(self.driver.active_seconds(), 0)
        self.assertEqual(self.spawn.started, [])
        self.assertEqual(self.state()['tasks'], {})

    def test_default_wind_down_also_refuses_new_repairs(self):
        self.default_driver(3600)
        self.reach_review()
        self.assertEqual(self.consume_phase(changes=True)['status'], 'blocked')
        self.clock.sleep(2400)
        result = self.action('repair', reason='Fix F1', next_owner='builder')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('wind-down', result['detail'])
        self.assertEqual(self.task_state()['round'], 0)

    def test_host_lock_wait_reserves_finalization_and_writes_report(self):
        # Hold a real flock until the host leaves live work for finalization.
        # A fresh ten-second live wait exhausts the signed finalization window.
        ready, release = threading.Event(), threading.Event()
        def holder():
            with (self.shift / 'driver.lock').open('wb') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                ready.set()
                release.wait(15)
        worker = threading.Thread(target=holder)
        worker.start()
        try:
            self.assertTrue(ready.wait(2))
            self.admission.now = lambda: dt.datetime.now(dt.timezone.utc)
            self.value.update(finalization_seconds=2,
                              until=(self.admission.now() + dt.timedelta(seconds=1)).isoformat())
            self.admission.budget = Budget(1)
            self.admission.guard = lambda: self.admission.budget.guard()
            self.admission.raw = receipts.encoded(self.value)
            (self.shift / 'grant.json').write_bytes(self.admission.raw)
            started, finishing = time.monotonic(), []
            original_finish = Driver.finish
            def finish(driver, reason):
                finishing.append(time.monotonic() - started)
                release.set()
                return original_finish(driver, reason)
            with patch.object(Driver, 'finish', new=finish):
                code, why = run_shift(self.admission, self.spawn, seconds=1, wind_down=.1)
            self.assertTrue((self.shift / 'report.md').exists(), (code, why, finishing))
            self.assertLess(finishing[0], 1.5)
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(self.spawn.started, [])
        finally:
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_host_lock_checks_withdrawal_during_wait_and_after_acquisition(self):
        original_flock, original_load, original_finish = fcntl.flock, Driver.load, Driver.finish
        for blocked_once in (True, False):
            with self.subTest(blocked_once=blocked_once):
                self.denial = ''
                finalizing, attempts, live_reads = [], [], []
                def flock(fd, flags):
                    if not attempts:
                        attempts.append(True)
                        self.denial = 'authority withdrawn during lock acquisition'
                        if blocked_once:
                            raise BlockingIOError()
                    return original_flock(fd, flags)
                def load(driver, view):
                    if not finalizing:
                        live_reads.append(True)
                    return original_load(driver, view)
                def finish(driver, reason):
                    finalizing.append(True)
                    return original_finish(driver, reason)
                with patch('sortie_kit.channels.fcntl.flock', side_effect=flock), \
                     patch.object(Driver, 'load', new=load), patch.object(Driver, 'finish', new=finish):
                    code, why = run_shift(self.admission, self.spawn, seconds=10, wind_down=.1)
                self.assertEqual(live_reads, [])
                self.assertEqual(code, 3)
                self.assertIn('withdrawn', why)
                self.assertTrue((self.shift / 'report.md').exists())
                self.assertEqual(self.spawn.started, [])

    def test_a_command_file_replaced_while_the_host_reads_it_is_read_again_and_answered(self):
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        with replaced_while_read(self.shift / "driver-command.json") as done:
            self.driver.poll()
        self.assertEqual(done, [1])
        answer = decode((self.shift / "driver-response.json").read_bytes())
        self.assertEqual((answer["id"], answer["status"]), ("first", "recorded"))

    def test_a_command_file_that_never_holds_still_does_not_end_the_shift(self):
        """The driver writes its command file whenever it likes. A reading of it that is cut
        across every time is no reading: nothing is answered, the pass ends, and the next
        pass reads again. Before, the first such reading ended the shift with code 3."""
        command = dict(version=1, id="first", op="finish", reason="done", next_owner="developer")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        # The host's clock is the fixture's and moves only by the host's own sleep. Each
        # reading has its time on the machine's clock, so the steps are whole seconds:
        # the last pass still has ten of them, and none rests on a fraction of one.
        with replaced_while_read(self.shift / "driver-command.json", times=None) as done:
            code, why = run_shift(self.admission, self.spawn, seconds=40, wind_down=20, poll=10,
                                  sleep=self.clock.sleep)
            self.assertEqual((code, why), (0, "working time over: stopped before the signed deadline"))
            self.assertEqual(len(done), 3 * receipts.SETTLE_TRIES)  # three passes, each read to the end
            del done[:]
            self.driver.poll()
            self.assertEqual(len(done), receipts.SETTLE_TRIES)
            # Nor when the time for the reading runs out while it is made again: it runs
            # out at the pause before the second read, whatever the machine's clock says.
            del done[:]
            real_guard, real_sleep, out = receipts.Evidence.guard, time.sleep, []
            def guard(view):
                if out:
                    raise Elapsed("duration elapsed")
                real_guard(view)
            def sleep(seconds):
                if seconds == receipts.SETTLE_PAUSE:  # the pause before the file is read again
                    out.append(seconds)
                else:
                    real_sleep(seconds)
            with patch.object(receipts.Evidence, "guard", guard), patch("sortie_kit.receipts.time.sleep", sleep):
                self.driver.poll()
            self.assertEqual((done, len(out)), ([1], 1))
        self.assertFalse((self.shift / "driver-response.json").exists())

    def reading_out_of_time(self, passed, **window):
        """The shift's end when the host's first reading of the command file takes `passed`
        seconds by the clock that the host and, here, the reading are both counted by; and
        the time that reading had been given."""
        command = dict(version=1, id="first", op="finish", reason="done", next_owner="developer")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        read, given = receipts.Evidence.read, []
        def slow(view, path, *args, **kwargs):
            if path == "driver-command.json" and not given:
                given.append(view.budget.remaining())
                self.clock.sleep(passed)
            return read(view, path, *args, **kwargs)
        with self.session_time_by_the_clock(), patch.object(receipts.Evidence, "read", slow):
            result = run_shift(self.admission, self.spawn, poll=10, sleep=self.clock.sleep, **window)
        self.assertFalse((self.shift / "driver-response.json").exists())  # the command was not read
        self.assertTrue((self.shift / "report.md").exists())
        return result, given

    def test_a_reading_of_the_command_file_that_runs_out_as_the_working_time_does_is_the_ordinary_end(self):
        """A reading that ran out of its time before anything had changed under it is not
        taken as no reading: it leaves the host's loop. It is given no more than the working
        time left, so when it has used that up the shift ends as it would have anyway."""
        result, given = self.reading_out_of_time(8, seconds=14, wind_down=12)  # 14 s less the 6 s of the stop
        self.assertEqual((result, given), ((0, "working time over: stopped before the signed deadline"), [8]))

    def test_a_reading_of_the_command_file_that_runs_out_with_working_time_left_ends_the_shift_with_code_3(self):
        """With working time left a reading has ten seconds. One that used them up is a
        file system or a Git that does not answer, and that is no ordinary end."""
        result, given = self.reading_out_of_time(10, seconds=40, wind_down=20)
        self.assertEqual((result, given), ((3, "duration elapsed"), [10]))

    def test_poll_partial_input_and_host_wind_down(self):
        (self.shift/'driver-command.json').write_text('{')
        self.driver.poll()
        self.assertEqual(decode((self.shift/'driver-response.json').read_bytes())['status'],'invalid')
        self.assertEqual(self.state()['tasks'],{})  # initial state is published before any answer
        # Whole seconds by the fixture's clock: no pass is given a fraction of one on the machine's.
        code,why=run_shift(self.admission,self.spawn,seconds=40,wind_down=20,poll=10,sleep=self.clock.sleep)
        self.assertEqual(code,0)
        self.assertIn('working time over',why)
        self.assertEqual(self.spawn.started,[])
        self.assertTrue((self.shift/'report.md').exists())

    def test_seat_stop_evidence_is_separate_from_toolkit(self):
        self.begin()
        task=self.task_state()
        value=decode(asset('stop-v3.json'))
        expected=self.driver.expected(task,None)
        for key in receipts.COMMON:value[key]=expected[key]
        value.update(phase='stop',receipt_id='seat-stopped')
        value['data'].update(status='stopped',scope='seat',evidence=[self.artifact('logs/stop.txt','seat stopped\n')])
        path='receipts/seat-stopped.json';(self.shift/path).write_bytes(receipts.encoded(value))
        self.assertEqual(self.action('stop-evidence',receipt=path)['status'],'recorded')
        self.assertIsNotNone(self.task_state()['cessation'])
        self.assertIsNone(self.task_state()['accepted'])


class TurnTests(LifecycleFixture):
    """Headless builder turns: one owned process per turn, recorded, never acceptance."""
    def turns(self):
        return self.state()["tasks"]["task"]["turns"]

    def test_brief_starts_turn_one_with_signed_argv_repo_cwd_and_reduced_environment(self):
        base = dict(HOME="/developer/home", PATH=os.environ["PATH"], GIT_DIR="/elsewhere", GIT_CONFIG_COUNT="2",
                    SSH_AUTH_SOCK="/agent.sock", SSH_ASKPASS="/ask", SUDO_ASKPASS="/ask", TMPDIR="/developer/own")
        with patch.dict(os.environ, base, clear=True):
            task = self.begin(running=True)
        self.assertEqual(len(self.spawn.started), 1)
        start = self.spawn.started[0]
        self.assertEqual(start["argv"][:3], ["fixture-agent", "run", "builder"])
        prompt = start["argv"][3]
        for part in (str(self.shift), task["brief"]["path"], task["brief"]["sha256"], "current-context.json"):
            self.assertIn(part, prompt)
        self.assertEqual(start["cwd"], str(self.target))
        scratch = str(self.shift / "scratch")
        self.assertEqual(start["env"], dict(HOME="/developer/home", PATH=os.environ["PATH"], TMPDIR=scratch, TMP=scratch,
                                            TEMP=scratch, GIT_TERMINAL_PROMPT="0"))
        turn = self.state()["turn"]
        self.assertEqual((turn["task_id"], turn["turn"], turn["limit_seconds"]), ("task", 1, 995))
        self.assertEqual((self.shift / turn["log"]).read_bytes(), b"")
        # A started turn is neither acknowledgment nor progress.
        self.assertEqual((task["stage"], task["acknowledged"], task["turns"]), ("ack", False, []))

    def test_every_builder_prompt_names_the_shift_and_the_exact_toolkit_command(self):
        import shlex
        from sortie_kit import launcher, lifecycle
        tool = lifecycle.toolkit()
        from sortie_kit import entry
        self.assertEqual(shlex.split(tool), [sys.executable, "-P", os.path.abspath(entry.__file__)])
        self.begin()
        self.assertEqual(self.action("continue")["status"], "recorded")
        first, again = (start["argv"][3] for start in self.spawn.started)
        self.assertIn("Task brief ", first)
        self.assertIn("continue task task at stage ack round 0", again)
        for prompt in (first, again):
            lines = prompt.splitlines()
            self.assertIn("shift: " + str(self.shift), lines)
            self.assertIn("toolkit: " + tool, lines)
            self.assertIn("TOOLKIT", prompt)
        # The driver is given the same command, spelled the same way.
        driver = launcher.driver_prompt(self.shift, {"direction_sha256": "d" * 64, "until": "2030-01-01T00:00:00+00:00"})
        self.assertIn("toolkit: " + tool, driver.splitlines())

    def test_the_toolkit_command_runs_the_package_the_host_loaded_and_no_other(self):
        # A seat runs the command from the target repository, with the
        # developer's PYTHONPATH. Another sortie_kit in either place must not be the one that runs.
        import shlex
        import sortie_kit
        from sortie_kit import lifecycle
        for name in ("in the checkout", "on the path"):
            package = self.root / name / "sortie_kit"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "__main__.py").write_text("print('another toolkit')\nraise SystemExit(7)\n")
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            [str(self.root / "on the path")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]))
        done = subprocess.run(lifecycle.toolkit() + " --version", shell=True, cwd=self.root / "in the checkout",
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, sortie_kit.__version__), done)
        done = subprocess.run(shlex.split(lifecycle.toolkit()) + ["--version"], cwd=self.root / "in the checkout",
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, sortie_kit.__version__), done)

    def test_a_stop_receipt_written_as_builder_md_says_is_one_the_driver_can_record(self):
        # The builder's recipe, with the stop context the host publishes.
        self.begin()
        context = decode((self.shift / "current-context.json").read_bytes())
        expected = context["stop_expected"]
        self.assertEqual((expected["phase"], expected["stop_scope"], expected["request_id"]),
                         ("stop", "seat", context["request_id"]))
        value = decode(asset("stop-v3.json"))
        for key in receipts.COMMON:
            value[key] = expected[key]
        value["receipt_id"] = "seat-stop-" + expected["request_id"]
        value["data"].update(status="stopped", scope="seat", evidence=[self.artifact("logs/stop.txt", "seat stopped\n")])
        (self.shift / "logs/expected-stop.json").write_text(json.dumps(expected))
        (self.shift / "logs/draft-stop.json").write_text(json.dumps(value))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["receipt", "write", "--shift", str(self.shift),
                                       "--expected", "logs/expected-stop.json", "--from", "logs/draft-stop.json"]), 0)
        self.assertEqual(self.action("stop-evidence", receipt=json.loads(out.getvalue())["path"])["status"], "recorded")
        self.assertIsNotNone(self.task_state()["cessation"])
        from importlib.resources import files
        self.assertIn("`stop_expected`", files("sortie_kit").joinpath("data", "templates", "builder.md").read_text())

    def test_no_seat_instruction_tells_a_seat_to_run_a_bare_sortie(self):
        from importlib.resources import files
        templates = files("sortie_kit").joinpath("data", "templates")
        for name in ("builder.md", "driver.md", "tiger.md"):
            text = templates.joinpath(name).read_text()
            self.assertNotRegex(text, r"(?<![\w/.-])sortie (receipt|queue-check|wait|report|start|stop|verify|instructions|trust)\b",
                                name)
        builder = templates.joinpath("builder.md").read_text()
        for part in ("toolkit:", "TOOLKIT receipt template PHASE",
                     "TOOLKIT receipt write --shift SHIFT --expected logs/"):
            self.assertIn(part, builder)
        for gone in ("check-review", "agy", "skill"):
            self.assertNotIn(gone, builder)
        self.assertNotIn("atomic writer", builder)

    def test_exit_is_recorded_with_its_status_and_never_advances_the_task(self):
        before = self.begin(running=True)
        self.end_turn(3)
        row, = self.turns()
        self.assertEqual((row["turn"], row["outcome"], row["exit"], row["reaped"]), (1, "exit", 3, True))
        self.assertEqual(row["output_dropped_bytes"], 0)
        task = self.task_state()
        self.assertEqual((task["stage"], task["request_id"], task["accepted"]),
                         ("ack", before["request_id"], None))

    def test_a_turn_records_how_long_it_ran_and_whether_a_receipt_appeared(self):
        self.begin(running=True)
        self.clock.sleep(30)
        (self.shift / "receipts/.consumed.json").write_text("{}")  # the host's own ledger is not a receipt
        self.end_turn(0)
        row, = self.turns()
        self.assertEqual((row["limit_seconds"], row["elapsed_seconds"], row["receipts_changed"]), (995, 30, False))
        self.assertNotIn("seconds", row)  # that name said nothing about budget or duration
        self.assertEqual(self.action("continue")["status"], "recorded")
        self.clock.sleep(2)
        (self.shift / "receipts/ack.json").write_text("{}")
        self.end_turn(0)
        row = self.turns()[1]
        self.assertEqual((row["elapsed_seconds"], row["receipts_changed"]), (2, True))

    def test_what_a_turn_wrote_is_unknown_when_the_receipts_cannot_be_listed(self):
        self.begin(running=True)
        (self.shift / "receipts").rename(self.shift / "receipts-moved")
        self.end_turn(0)
        row, = self.turns()
        # The turn's end is recorded all the same; only the observation is missing.
        self.assertEqual((row["outcome"], row["exit"], row["receipts_changed"]), ("exit", 0, None))

    def test_the_receipts_listing_is_bounded_by_entries_seen_and_by_time(self):
        from sortie_kit import channels
        from sortie_kit.lifecycle import receipt_marks
        (self.shift / "receipts/ack.json").write_text("{}")
        with channels.locked(self.shift, "driver.lock", seconds=5, local_only=True) as view:
            self.assertEqual(list(receipt_marks(view)), ["ack.json"])
            self.assertIsNone(receipt_marks(view, seconds=0))  # no time left: unknown, not a partial listing
            for n in range(1100):  # ignored names are still entries the host has to walk
                (self.shift / "receipts" / (".tmp-" + str(n))).touch()
            self.assertIsNone(receipt_marks(view))
        self.begin(running=True)
        self.end_turn(0)
        self.assertIsNone(self.turns()[0]["receipts_changed"])

    def test_recording_a_turns_end_comes_before_the_optional_receipts_listing(self):
        from sortie_kit import lifecycle
        self.begin(running=True)
        self.spawn.child.code = 0
        calls = []
        def listing(view, seconds=0.25, **kwargs):
            calls.append(seconds)
            return {}
        # Under half a second is left to write the turn's end: no listing competes with it.
        # The lock's time is counted by the fixture's clock, so it is 0.4 s however long the write takes.
        with patch.object(lifecycle, "receipt_marks", listing), self.session_time_by_the_clock(), \
                patch.object(self.driver, "final_seconds", lambda: 0.4):
            self.driver.poll_turn()
        self.assertEqual(calls, [])
        row, = self.turns()
        self.assertEqual((row["outcome"], row["exit"], row["receipts_changed"]), ("exit", 0, None))
        # With time to spare the listing gets only what is left above that reserve.
        self.assertEqual(self.action("continue")["status"], "recorded")
        self.spawn.child.code = 0
        with patch.object(lifecycle, "receipt_marks", listing), self.session_time_by_the_clock(), \
                patch.object(self.driver, "final_seconds", lambda: 0.6):
            self.driver.poll_turn()
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0], 0.1)
        self.assertIsNotNone(self.turns()[1]["ended"])

    def test_a_stop_noted_while_the_receipts_are_listed_starts_no_builder(self):
        noted = []
        def listing(view, **kwargs):
            noted.append(15)  # a stop signal arrives during the listing
            return {}
        self.driver.halt = lambda: bool(noted)
        with patch("sortie_kit.lifecycle.receipt_marks", listing):
            result = self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                                 next_owner="builder")
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(self.spawn.started, [])

    def test_elapsed_time_ends_when_the_host_sees_the_turn_over_not_after_its_cleanup(self):
        self.begin(running=True)
        self.clock.sleep(7)
        self.spawn.child.code = 0
        from sortie_kit import tick_supervise
        def slow_stop(child, seconds):
            self.clock.sleep(50)  # stopping the group and draining output take their own time
            return True
        with patch.object(tick_supervise, "stop_group", slow_stop):
            self.driver.poll_turn()
        self.assertEqual(self.turns()[0]["elapsed_seconds"], 7)

    def test_a_driver_named_owner_gives_way_when_the_host_could_not_clean_up(self):
        def shift_with(close):
            self.setUp()
            command = dict(version=1, id="end", op="finish", reason="done", next_owner="release-team")
            (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
            seat = SimpleNamespace(ended=lambda: False, close=lambda: close)
            run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=1, sleep=self.clock.sleep,
                      driver_seat=seat)
            return self.state()["next_owner"]
        self.assertEqual(shift_with("stopped by the host"), "release-team")
        self.assertEqual(shift_with("stop attempted; exit not observed"), "developer")
        self.assertEqual(shift_with("stopped by the host; terminal not restored"), "developer")

    def test_the_drivers_named_next_owner_survives_the_hosts_closing(self):
        self.begin()
        self.assertEqual(self.action("finish", reason="builder cannot write", next_owner="developer")["status"], "closed")
        self.assertTrue(self.driver.finish("builder cannot write"))
        state = self.state()
        self.assertEqual((state["mode"], state["next_owner"], state["reason"]),
                         ("closed", "developer", "builder cannot write"))
        self.assertEqual(decode((self.shift / "report.json").read_bytes())["next_owner"], "developer")

    def test_finish_keeps_what_the_driver_names_as_not_started_and_the_report_says_whose_count_it_is(self):
        self.begin()
        left = [dict(name="pad", reason="the window ended before it could be briefed"),
                dict(name="wrap", reason="depends on pad")]
        result = self.action("finish", reason="task is unfinished; pad and wrap were not started",
                             next_owner="developer", not_started=left)
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(self.state()["not_started"], left)
        self.assertTrue(self.driver.finish("driver finished"))
        self.assertEqual(self.state()["not_started"], left)
        self.assertEqual(decode((self.shift / "report.json").read_bytes())["not_started"], left)
        text = (self.shift / "report.md").read_text()
        self.assertIn("- Tasks: 0 accepted locally of 1 briefed; the driver names 2 more as not started.\n", text)
        undone = text.split("## Not done")[1].split("\n## ")[0]
        self.assertIn("- Not started, as the driver stated it: pad: the window ended before it could be briefed.\n", undone)
        self.assertIn("- Not started, as the driver stated it: wrap: depends on pad.\n", undone)
        self.assertNotIn("left undone", undone)

    def test_a_finish_without_the_list_leaves_it_unknown_and_an_empty_list_is_the_drivers_statement(self):
        self.assertEqual(self.action("finish", reason="done", next_owner="developer", not_started=[])["status"], "closed")
        self.assertEqual(self.state()["not_started"], [])
        self.setUp()
        self.assertEqual(self.action("finish", reason="done", next_owner="developer")["status"], "closed")
        self.assertIsNone(self.state()["not_started"])

    def test_a_not_started_list_of_the_wrong_shape_is_refused_and_nothing_changes(self):
        self.begin()
        before = self.state()
        item = dict(name="pad", reason="no time")
        for name, value in (("not a list", "pad"), ("an item that is not an object", ["pad"]),
                            ("an item without a reason", [dict(name="pad")]),
                            ("an item without a name", [dict(reason="no time")]),
                            ("a reason that is not text", [dict(item, reason=3)]),
                            ("an item with an unknown field", [dict(item, extra=1)]),
                            ("a blank name", [dict(item, name=" ")]), ("a blank reason", [dict(item, reason="")]),
                            ("a name that is too long", [dict(item, name="n" * 201)]),
                            ("a reason that is too long", [dict(item, reason="r" * 501)]),
                            ("a name that is not text", [dict(item, name=3)]),
                            ("too many items", [item] * 33), ("null", None)):
            with self.subTest(name=name):
                result = self.action("finish", reason="done", next_owner="developer", not_started=value)
                self.assertEqual(result["status"], "invalid", result)
                self.assertIn("Nothing changed", result["detail"])
                # The refusal is recorded as a command result; nothing else in the state moves.
                state, key = self.state(), "command-" + str(self.counter)
                recorded = state["commands"].pop(key)
                self.assertEqual(recorded["response"]["status"], "invalid")
                self.assertEqual(state, before)
                before["commands"][key] = recorded
        longest = [dict(name="n" * 200, reason="r" * 500)] * 32
        self.assertEqual(self.action("finish", reason="done", next_owner="developer",
                                     not_started=longest)["status"], "closed")
        self.assertEqual(self.state()["not_started"], longest)

    def test_a_state_written_before_not_started_existed_still_loads_and_one_with_a_bad_list_does_not(self):
        self.begin()
        state = self.state()
        del state["not_started"]
        (self.shift / "driver-state.json").write_bytes(receipts.encoded(state))
        self.assertEqual(self.action("finish", reason="done", next_owner="developer")["status"], "closed")
        self.assertIsNone(self.state()["not_started"])
        self.assertTrue(self.driver.finish("driver finished"))
        self.assertIn("- Tasks: 0 accepted locally of 1 briefed; whether the direction held more is not known to "
                      "the host.\n", (self.shift / "report.md").read_text())
        state = self.state()
        state["not_started"] = [dict(name="pad")]
        (self.shift / "driver-state.json").write_bytes(receipts.encoded(state))
        with self.driver.coordination(live=False) as view:
            with self.assertRaises(Refused):
                self.driver.load(view)

    def test_a_shift_the_host_ends_is_handed_to_the_developer(self):
        self.begin()
        self.assertTrue(self.driver.finish("driver exited"))
        self.assertEqual((self.state()["mode"], self.state()["next_owner"]), ("closed", "developer"))

    def test_signal_death_is_recorded_as_abnormal(self):
        self.begin(running=True)
        self.end_turn(-9)
        self.assertEqual((self.turns()[0]["outcome"], self.turns()[0]["exit"]), ("signal", -9))

    def test_running_turn_is_left_alone_until_its_limit_then_stopped_as_timeout(self):
        self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                    next_owner="builder", turn_seconds=5)
        self.assertEqual(self.state()["turn"]["limit_seconds"], 5)
        self.clock.sleep(4)
        self.driver.poll_turn()
        self.assertEqual(self.spawn.child.signals, [])
        self.assertIsNotNone(self.state()["turn"])
        self.clock.sleep(1)
        self.driver.poll_turn()
        self.assertEqual(self.spawn.child.signals, ["terminate"])
        row, = self.turns()
        self.assertEqual((row["outcome"], row["exit"], row["reaped"]), ("timeout", -15, True))
        self.assertIsNone(self.state()["turn"])

    def test_builder_that_ignores_the_stop_is_killed(self):
        self.spawn.stubborn = True
        self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                    next_owner="builder", turn_seconds=1)
        self.clock.sleep(1)
        self.driver.poll_turn()
        self.assertEqual(self.spawn.child.signals, ["terminate", "kill"])
        self.assertEqual((self.turns()[0]["outcome"], self.turns()[0]["exit"]), ("timeout", -9))

    def test_stop_looks_again_after_a_transient_group_denial(self):
        from sortie_kit.tick_supervise import stop_group
        child = FakeSpawn()(["x"], cwd=".", env={})
        looks = []
        def group_alive():
            looks.append(1)
            if len(looks) in (2, 3):
                raise PermissionError("group leader is exiting")
            return child.code is None
        child.group_alive = group_alive
        self.assertTrue(stop_group(child, 1.0))
        self.assertEqual((child.signals, child.code, len(looks)), (["terminate"], -15, 4))
        stuck = FakeSpawn()(["x"], cwd=".", env={})
        stuck.group_alive = lambda: True  # a descendant outlives the reaped leader
        self.assertFalse(stop_group(stuck, 0.2))
        self.assertEqual(stuck.signals, ["terminate", "kill"])

    def test_turn_seconds_must_be_a_bounded_whole_number_and_changes_nothing_when_refused(self):
        for bad in (0, -1, 7201, 1.5, True, "60", None):
            with self.subTest(turn_seconds=bad):
                result = self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                                     next_owner="builder", turn_seconds=bad)
                self.assertEqual(result["status"], "invalid", result)
                self.assertFalse((self.shift / "driver-state.json").exists())
        self.assertEqual(self.spawn.started, [])

    def test_turn_length_never_reaches_past_the_moment_work_stops(self):
        self.clock.sleep(980)
        self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                    next_owner="builder", turn_seconds=7200)
        self.assertEqual(self.state()["turn"]["limit_seconds"], 15)

    def test_continue_is_refused_while_a_turn_runs_and_starts_the_next_one_after(self):
        task = self.begin(running=True)
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("already running", result["detail"])
        self.assertEqual(len(self.spawn.started), 1)
        self.end_turn()
        result = self.action("continue", turn_seconds=60)
        self.assertEqual(result["status"], "recorded", result)
        self.assertEqual(len(self.spawn.started), 2)
        prompt = self.spawn.started[1]["argv"][3]
        for part in ("continue task task", "stage ack", "round 0", "Do not restart"):
            self.assertIn(part, prompt)
        after = self.task_state()
        self.assertEqual((after["request_id"], after["stage"], after["round"]), (task["request_id"], "ack", 0))
        self.assertEqual((self.state()["turn"]["turn"], self.state()["turn"]["limit_seconds"]), (2, 60))

    def test_continue_starts_no_turn_while_what_the_last_turn_handed_in_is_not_consumed(self):
        """A `continue` that replaced the `consume` cost a builder turn with nothing to do."""
        self.begin(running=True)
        _, path = self.phase_receipt()
        self.end_turn()
        row, = self.turns()
        self.assertEqual((row["receipts_changed"], row["request_id"]), (True, self.task_state()["request_id"]))
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        for part in ("changed receipts/", "consume", '"again": true', "No turn was started"):
            self.assertIn(part, result["detail"])
        for bad in (False, 1, "true", None):
            result = self.action("continue", again=bad)
            self.assertEqual(result["status"], "invalid", result)
        self.assertEqual((len(self.spawn.started), len(self.turns()), self.state()["mode"]), (1, 1, "active"))
        self.assertEqual(self.action("consume", receipt=path)["status"], "advanced")
        # The phase is a new one: its first turn is started as before.
        self.assertEqual(self.action("continue")["status"], "recorded")
        self.assertEqual(len(self.spawn.started), 2)

    def test_continue_again_starts_the_turn_and_a_running_turn_is_still_answered_as_running(self):
        self.begin(running=True)
        (self.shift / "receipts/not-the-receipt.txt").write_text("a note the builder left\n")
        self.end_turn()
        self.assertEqual(self.action("continue")["status"], "blocked")
        result = self.action("continue", again=True)
        self.assertEqual(result["status"], "recorded", result)
        self.assertEqual(len(self.spawn.started), 2)
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("already running", result["detail"])
        self.end_turn()
        # Turn 2 changed nothing, so the next one needs no `again`.
        self.assertIs(self.turns()[1]["receipts_changed"], False)
        self.assertEqual(self.action("continue")["status"], "recorded")

    def test_a_turn_row_from_an_earlier_host_without_a_request_id_does_not_refuse_continue(self):
        self.begin(running=True)
        self.phase_receipt()
        self.end_turn()
        state = self.state()
        del state["tasks"]["task"]["turns"][0]["request_id"]
        (self.shift / "driver-state.json").write_bytes(receipts.encoded(state))
        self.assertEqual(self.action("continue")["status"], "recorded")

    def test_continue_is_refused_in_wind_down_without_closing_the_lifecycle(self):
        self.begin()
        self.clock.sleep(991)
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("wind-down", result["detail"])
        self.assertEqual(len(self.spawn.started), 1)
        self.assertEqual(self.state()["mode"], "active")

    def test_continue_needs_an_open_builder_phase(self):
        self.reach_review()
        self.assertEqual(self.consume_phase()["status"], "advanced")
        started = len(self.spawn.started)
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("accepted", result["detail"])
        self.assertEqual(len(self.spawn.started), started)

    def test_thirty_third_turn_of_a_task_is_refused(self):
        self.begin()
        for _ in range(31):
            self.assertEqual(self.action("continue")["status"], "recorded")
            self.end_turn()
        self.assertEqual(len(self.turns()), 32)
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("turn limit", result["detail"])
        self.assertEqual(len(self.spawn.started), 32)
        self.assertEqual(self.state()["mode"], "active")

    def test_consume_repair_and_close_wait_for_the_running_turn(self):
        self.begin(running=True)
        before = self.task_state()
        _, path = self.phase_receipt()
        for op, data in (("consume", dict(receipt=path)), ("repair", dict(reason="r", next_owner="builder")),
                         ("close", {})):
            result = self.action(op, **data)
            self.assertEqual(result["status"], "blocked", (op, result))
            self.assertIn("turn is running", result["detail"])
        self.assertEqual(self.task_state(), before)
        self.assertEqual(self.state()["mode"], "active")
        self.end_turn()
        self.assertEqual(self.action("consume", receipt=path)["status"], "advanced")

    def test_start_failure_is_recorded_on_the_task_and_leaves_no_running_turn(self):
        self.spawn.failure = FileNotFoundError("fixture-agent is gone")
        result = self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                             next_owner="builder")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("did not start", result["detail"])
        row, = self.turns()
        self.assertTrue(row["outcome"].startswith("not started: "), row)
        self.assertEqual((row["exit"], row["reaped"]), (None, False))
        self.assertIsNone(self.state()["turn"])
        self.assertEqual(self.state()["mode"], "active")
        self.spawn.failure = None
        self.assertEqual(self.action("continue")["status"], "recorded")

    def test_refused_seat_start_never_spawns(self):
        def refuse(role, prompt):
            raise Refused("builder executable changed since signing")
        self.admission.seat_start = refuse
        result = self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                             next_owner="builder")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("changed since signing", result["detail"])
        self.assertEqual(self.spawn.started, [])
        self.assertIsNone(self.state()["turn"])

    def test_turn_left_by_an_earlier_host_is_recorded_as_lost_never_as_stopped(self):
        self.begin(running=True)
        self.driver = Driver(self.admission, self.spawn, seconds=900, wind_down=10)
        self.driver.poll_turn()  # nothing owned here: no signal, no record
        self.assertIsNotNone(self.state()["turn"])
        self.driver.finish("host restarted")
        row, = self.turns()
        self.assertEqual((row["outcome"], row["exit"], row["reaped"]), ("lost", None, False))
        self.assertEqual(self.spawn.child.signals, [])
        self.assertIn("lost", (self.shift / "report.md").read_text())

    def test_builder_output_is_kept_up_to_a_bound_and_the_rest_is_counted(self):
        reader, writer = os.pipe()
        self.spawn.stdout = os.fdopen(reader, "rb", buffering=0)
        os.write(writer, b"0123456789abcdefghijklmno")
        os.close(writer)
        with patch("sortie_kit.lifecycle.TURN_LOG_LIMIT", 10):
            self.begin()
        row, = self.turns()
        self.assertEqual((self.shift / row["log"]).read_bytes(), b"0123456789")
        self.assertEqual(row["output_dropped_bytes"], 15)
        self.assertTrue(self.spawn.child.stdout.closed)

    def test_builder_cannot_redirect_its_turn_log_through_a_link(self):
        reader, writer = os.pipe()
        self.spawn.stdout = os.fdopen(reader, "rb", buffering=0)
        self.addCleanup(self.spawn.stdout.close)
        self.begin(running=True)
        outside = self.root / "outside.txt"
        outside.write_text("untouched")
        log = self.shift / self.state()["turn"]["log"]
        log.unlink(); log.symlink_to(outside)
        os.write(writer, b"output"); os.close(writer)
        self.spawn.child.code = 0
        self.driver.poll_turn()
        # The host kept its own descriptor on the file it created; the link is never opened.
        self.assertEqual(outside.read_text(), "untouched")
        row, = self.turns()
        self.assertEqual((row["outcome"], row["output_dropped_bytes"]), ("exit", 0))

    def test_a_pipe_put_at_the_log_path_cannot_stall_the_host(self):
        reader, writer = os.pipe()
        self.spawn.stdout = os.fdopen(reader, "rb", buffering=0)
        self.addCleanup(self.spawn.stdout.close)
        self.begin(running=True)
        log = self.shift / self.state()["turn"]["log"]
        log.unlink(); os.mkfifo(log)  # opening this for writing would wait for a reader
        os.write(writer, b"output"); os.close(writer)
        self.spawn.child.code = 0
        began = time.monotonic()
        self.driver.poll_turn()
        self.assertLess(time.monotonic() - began, 1)
        self.assertEqual(self.turns()[0]["outcome"], "exit")

    def test_endless_output_is_read_in_bounded_steps_and_the_stop_comes_before_the_drain(self):
        self.spawn.stdout = open("/dev/zero", "rb", buffering=0)  # always readable, never ends
        self.addCleanup(self.spawn.stdout.close)
        self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                    next_owner="builder", turn_seconds=5)
        with patch("sortie_kit.lifecycle.TURN_LOG_LIMIT", 100), patch("sortie_kit.lifecycle.PUMP_READS", 3):
            self.driver.poll_turn()
            self.assertEqual(self.driver.turn_bytes + self.driver.turn_dropped, 3 * 65536)
            self.assertEqual(self.spawn.child.signals, [])
            self.clock.sleep(5)
            order = []
            pump = self.driver.pump
            def pumping():
                order.append(list(self.spawn.child.signals))
                pump()
            with patch.object(self.driver, "pump", side_effect=pumping):
                self.driver.poll_turn()
        self.assertEqual(order, [["terminate"]])
        row, = self.turns()
        self.assertEqual((row["outcome"], row["output_dropped_bytes"]), ("timeout", 6 * 65536 - 100))
        self.assertEqual((self.shift / row["log"]).stat().st_size, 100)

    def test_unconfirmed_stop_keeps_the_turn_and_refuses_another_until_the_group_is_gone(self):
        self.spawn.lingering = True
        self.begin(running=True)
        self.spawn.child.code = 0
        self.driver.poll_turn()
        self.assertEqual(self.spawn.child.signals, ["terminate", "kill"])
        self.assertIsNotNone(self.state()["turn"])  # still recorded as running
        self.assertEqual(self.turns(), [])
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("could not be confirmed stopped", result["detail"])
        self.assertEqual(len(self.spawn.started), 1)
        self.spawn.lingering = False
        self.driver.poll_turn()
        row, = self.turns()
        self.assertEqual((row["outcome"], row["exit"], row["reaped"]), ("exit", 0, True))
        self.assertEqual(self.action("continue")["status"], "recorded")

    def test_host_ending_with_an_unconfirmed_builder_stop_is_not_a_clean_ending(self):
        self.spawn.lingering = True
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=10,
                              sleep=self.clock.sleep)
        self.assertEqual(code, 3)
        # The reason stays what ended the shift; what the host could not confirm is kept apart.
        self.assertEqual(why, "working time over: stopped before the signed deadline")
        row, = self.turns()
        self.assertEqual((row["outcome"], row["reaped"]),
                         ("stopped: working time over: stopped before the signed deadline; stop not confirmed", False))
        doc = decode((self.shift / "report.json").read_bytes())
        window = doc["actual_window"]
        self.assertEqual((window["reason"], window["processes"]["cleanup"], doc["next_owner"]), (why, "done", "developer"))
        self.assertEqual(window["processes"]["shutdown"],
                         dict(confirmed=False, groups=[], problems=["a builder turn was not seen to stop"]))
        text = (self.shift / "report.md").read_text()
        self.assertIn("- **Shutdown: NOT CONFIRMED.** A builder turn was not seen to stop.", text)
        self.assertIn("- How it ended: " + why + "\n", text)
        for clear in ("Cleanup: done", "- Status: no problem was found", "\nNone found.\n", "Shutdown: confirmed"):
            self.assertNotIn(clear, text)

    def driver_stop(self, said, later=None, group=4242):
        """Run a shift whose driver seat ended, with what its close and its later look said."""
        seat = SimpleNamespace(ended=lambda: True, close=lambda: said, group=group)
        if later is not None:
            seat.recheck = lambda: later
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, driver_seat=seat)
        doc = decode((self.shift / "report.json").read_bytes())
        return code, why, doc["actual_window"], (self.shift / "report.md").read_text()

    def test_a_driver_not_seen_to_exit_is_said_apart_from_the_reason_with_the_check_to_run(self):
        said = ("stop signaled; it was exiting and had not finished when the host stopped waiting; "
                "its process group was not observed gone")
        code, why, window, text = self.driver_stop(said, later=said)
        self.assertEqual((code, why, window["reason"]), (3, "driver exited", "driver exited"))
        self.assertEqual(window["processes"]["shutdown"], dict(
            confirmed=False, groups=[4242], problems=["the driver process was not seen to stop (" + said + ")"]))
        outcome = text.split("## Outcome")[1].split("##")[0].strip().splitlines()
        self.assertEqual(outcome[0], "- Tasks: none briefed; whether the direction held more is not known to the host.")
        self.assertEqual(outcome[1], "- **Shutdown: NOT CONFIRMED.** The driver process was not seen to stop (" + said
                         + "). Check for leftover processes; no line printed means none are left: "
                         "`ps -A -o pid,pgid,stat,command | awk '$2 == 4242'`")
        self.assertEqual(outcome[2], "- Status: the shutdown was not confirmed; see the line above. No problem was "
                                     "found with the evidence that was read.")
        self.assertIn("\nNone found in the kept files. The shutdown was not confirmed; see Outcome.\n", text)
        self.assertNotIn("Cleanup: done", text)
        self.assertIn("Not confirmed: the driver process was not seen to stop", text)

    def test_a_driver_seen_ended_at_the_later_look_is_a_confirmed_shutdown(self):
        later = "stop signaled; seen ended after the host stopped waiting"
        code, why, window, text = self.driver_stop("stop signaled; exit not observed; its process group was "
                                                   "not observed gone", later=later)
        self.assertEqual((code, why), (0, "driver exited"))
        self.assertEqual(window["processes"]["driver"], later)
        self.assertEqual(window["processes"]["shutdown"], dict(confirmed=True, groups=[], problems=[]))
        outcome = text.split("## Outcome")[1].split("##")[0].strip().splitlines()
        self.assertEqual(outcome[:3], ["- Tasks: none briefed; whether the direction held more is not known to "
                                       "the host.", "- Shutdown: confirmed.",
                                       "- Status: no problem was found with the evidence that was read."])
        self.assertIn("Driver process: " + later + ". Cleanup: done.", text)

    def test_a_closeout_kept_from_before_the_shutdown_entry_is_still_not_read_as_all_clear(self):
        from sortie_kit import report
        self.driver_stop("exited on its own with status 0")
        raw = decode((self.shift / "closeout.json").read_bytes())
        del raw["processes"]["shutdown"]
        raw["processes"]["driver"] = "stop signaled; exit not observed; its process group was not observed gone"
        (self.shift / "closeout.json").write_bytes(receipts.encoded(raw))
        report.finalize(self.shift, 5)
        text = (self.shift / "report.md").read_text()
        self.assertIn("- **Shutdown: NOT CONFIRMED.** The driver process was not seen to stop (stop signaled; "
                      "exit not observed; its process group was not observed gone). Check for leftover processes "
                      "with: `ps -A -o pid,pgid,stat,command`", text)
        self.assertNotIn("Cleanup: done", text)
        self.assertNotIn("- Status: no problem was found", text)

    def test_a_close_that_raised_is_looked_at_again_and_its_terminal_finding_is_kept(self):
        def close():
            raise OSError("signal refused")
        seat = SimpleNamespace(ended=lambda: True, close=close, group=4242, unrestored=True,
                               recheck=lambda: "stop attempted; exit not observed; terminal not restored")
        status = {}
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, driver_seat=seat, status=status)
        window = decode((self.shift / "report.json").read_bytes())["actual_window"]
        self.assertEqual((code, why), (3, "driver exited"))
        self.assertEqual(window["processes"]["shutdown"]["problems"], [
            "the driver process was not seen to stop (stop attempted; exit not observed; terminal not restored)",
            "the terminal could not be restored"])
        # The caller is told what the report was told, also when the report is not written.
        self.assertEqual((status["driver"], status["shutdown"]),
                         (window["processes"]["driver"], window["processes"]["shutdown"]))
        seat.recheck = lambda: "stop signaled; seen ended after the host stopped waiting"
        seat.unrestored = False
        self.assertEqual(run_shift(self.admission, self.spawn, seconds=100, wind_down=10, driver_seat=seat)[0], 0)

    def test_an_older_closeout_with_an_unconfirmed_builder_stop_is_not_read_as_confirmed(self):
        from sortie_kit import report
        self.spawn.lingering = True
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=10, sleep=self.clock.sleep)
        raw = decode((self.shift / "closeout.json").read_bytes())
        self.assertEqual(raw["processes"]["cleanup"], "done")
        del raw["processes"]["shutdown"]  # as the earlier host wrote it: only the turn says so
        (self.shift / "closeout.json").write_bytes(receipts.encoded(raw))
        report.finalize(self.shift, 5)
        text = (self.shift / "report.md").read_text()
        self.assertIn("- **Shutdown: NOT CONFIRMED.** A builder turn was not seen to stop.", text)
        self.assertNotIn("Cleanup: done", text)
        window = dict(reason="x; a started process could not be confirmed stopped",
                      processes=dict(cleanup="done", driver="exited on its own with status 0"))
        self.assertEqual(report.shutdown_seen(window)[:2], (False, ["a started process could not be confirmed stopped"]))

    def test_a_malformed_shutdown_entry_is_unknown_not_confirmed(self):
        from sortie_kit import report
        self.driver_stop("exited on its own with status 0")
        raw = decode((self.shift / "closeout.json").read_bytes())
        whole = dict(confirmed=True, groups=[], problems=[])
        for entry in (dict(confirmed="yes"), None, dict(confirmed=True), dict(whole, problems="none"),
                      dict(whole, groups=[4242]), dict(whole, problems=[5]), dict(whole, groups=None), [], "confirmed"):
            with self.subTest(entry=entry):
                raw["processes"]["shutdown"] = entry
                (self.shift / "closeout.json").write_bytes(receipts.encoded(raw))
                report.finalize(self.shift, 5)
                text = (self.shift / "report.md").read_text()
                self.assertIn("- Shutdown: not recorded. The closeout's shutdown entry could not be read.", text)
                self.assertNotIn("Shutdown: confirmed", text)
        raw["processes"]["shutdown"] = whole
        (self.shift / "closeout.json").write_bytes(receipts.encoded(raw))
        report.finalize(self.shift, 5)
        self.assertIn("- Shutdown: confirmed.", (self.shift / "report.md").read_text())

    def test_a_hostile_shutdown_entry_adds_no_command_and_no_markup_to_the_report(self):
        from sortie_kit import report
        self.driver_stop("exited on its own with status 0")
        raw = decode((self.shift / "closeout.json").read_bytes())
        raw["processes"]["shutdown"] = dict(confirmed=False, groups=["1'; rm -rf ~; '", -3, True, 77],
                                            problems=["`x` <b> # h", 5])
        (self.shift / "closeout.json").write_bytes(receipts.encoded(raw))
        report.finalize(self.shift, 5)
        line = next(l for l in (self.shift / "report.md").read_text().splitlines() if "Shutdown:" in l)
        self.assertTrue(line.endswith("`ps -A -o pid,pgid,stat,command | awk '$2 == 77'`"), line)
        self.assertNotIn("rm -rf", line)
        self.assertNotIn("<b>", line)
        self.assertIn("\\`x\\`", line)

    def test_start_checks_that_cross_into_wind_down_start_nothing(self):
        def slow(role, prompt):
            self.clock.sleep(985)  # the pin and guard reads took until five seconds before wind-down
            return ["fixture-agent"]
        self.admission.seat_start = slow
        result = self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                             next_owner="builder", turn_seconds=60)
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(self.spawn.started, [])
        self.assertTrue(self.turns()[0]["outcome"].startswith("not started: "))
        self.assertIsNone(self.state()["turn"])

    def test_turn_limit_is_counted_from_before_the_start_checks(self):
        def slow(role, prompt):
            self.clock.sleep(20)
            return ["fixture-agent"]
        self.admission.seat_start = slow
        self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                    next_owner="builder", turn_seconds=60)
        self.assertEqual(len(self.spawn.started), 1)
        self.clock.sleep(39)
        self.driver.poll_turn()
        self.assertEqual(self.spawn.child.signals, [])
        self.clock.sleep(1)
        self.driver.poll_turn()
        self.assertEqual(self.turns()[0]["outcome"], "timeout")

    def test_an_end_the_host_saw_but_could_not_write_is_not_reported_as_lost(self):
        self.begin(running=True)
        self.spawn.child.code = 3
        with patch.object(self.driver, "coordination", side_effect=Refused("lock unavailable")), \
                self.assertRaises(Refused):
            self.driver.poll_turn()
        self.assertIsNotNone(self.state()["turn"])
        self.assertTrue(self.driver.finish("host stopped"))
        row, = self.turns()
        self.assertEqual((row["outcome"], row["exit"], row["reaped"]), ("exit", 3, True))

    def test_lost_turn_blocks_the_lifecycle_and_starts_no_second_builder(self):
        self.begin(running=True)
        self.driver = Driver(self.admission, self.spawn, seconds=900, wind_down=10)
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("may still run", result["detail"])
        self.assertEqual(len(self.spawn.started), 1)
        self.assertEqual(self.spawn.child.signals, [])
        state = self.state()
        self.assertEqual((state["mode"], state["next_owner"], state["turn"]), ("blocked", "developer", None))
        self.assertEqual(self.turns()[0]["outcome"], "lost")
        self.assertEqual((self.turns()[0]["receipts_changed"], self.turns()[0]["elapsed_seconds"]), (None, None))
        self.assertIn("may still run", (self.shift / "report.md").read_text())

    def test_a_drain_failure_after_an_unconfirmed_stop_is_recorded_and_not_a_clean_ending(self):
        self.spawn.lingering = True
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        def pump(driver):
            if self.spawn.child.signals:
                raise OSError("pipe read failed")
        with patch.object(Driver, "pump", pump):
            code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=10,
                                  sleep=self.clock.sleep)
        self.assertEqual((code, why), (3, "working time over: stopped before the signed deadline"))
        shutdown = decode((self.shift / "report.json").read_bytes())["actual_window"]["processes"]["shutdown"]
        self.assertEqual((shutdown["confirmed"], shutdown["problems"][0]), (False, "a builder turn was not seen to stop"))
        self.assertTrue(shutdown["problems"][1].startswith("cleanup was incomplete: "), shutdown)
        self.assertIn("pipe read failed", shutdown["problems"][1])
        row, = self.turns()
        self.assertEqual((row["outcome"], row["reaped"]),
                         ("stopped: working time over: stopped before the signed deadline; stop not confirmed", False))
        cleanup = decode((self.shift / "report.json").read_bytes())["actual_window"]["processes"]["cleanup"]
        self.assertTrue(cleanup.startswith("incomplete: "), cleanup)
        self.assertIn("pipe read failed", cleanup)

    def test_a_stop_that_raises_is_recorded_as_unconfirmed_and_both_descriptors_are_closed(self):
        read, write = os.pipe()
        self.addCleanup(os.close, write)
        self.spawn.stdout = os.fdopen(read, "rb", buffering=0)
        self.begin(running=True)
        fd = self.driver.turn_fd
        with patch("sortie_kit.tick_supervise.stop_group", side_effect=PermissionError("no signal")):
            self.driver.poll_turn()  # a live pass: nothing is recorded, the handle is kept
            self.assertEqual(self.turns(), [])
            self.assertIsNotNone(self.driver.child)
            self.spawn.child.code = 0
            self.driver.poll_turn(stop="stopped: host stopped")
        row, = self.turns()
        self.assertEqual((row["outcome"], row["reaped"]), ("exit; stop not confirmed", False))
        self.assertTrue(self.driver.unconfirmed)
        self.assertIn("no signal", "; ".join(self.driver.cleanup_errors))
        self.assertTrue(self.spawn.stdout.closed)
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_a_failed_drain_at_an_ordinary_turn_end_is_kept_for_the_closeout(self):
        self.begin(running=True)
        self.spawn.child.code = 0
        with patch.object(self.driver, "pump", side_effect=OSError("pipe read failed")):
            self.driver.poll_turn()
        row, = self.turns()
        self.assertEqual((row["outcome"], row["exit"], row["reaped"]), ("exit", 0, True))
        self.assertEqual(self.driver.cleanup_errors, ["builder output drain failed: pipe read failed"])
        self.driver.cleaned = True
        self.assertTrue(self.driver.finish("host stopped"))
        cleanup = decode((self.shift / "report.json").read_bytes())["actual_window"]["processes"]["cleanup"]
        self.assertEqual(cleanup, "incomplete: builder output drain failed: pipe read failed")

    def test_the_stop_latch_is_asked_after_everything_the_spawn_needs_is_prepared(self):
        noted = []
        def env(shift, **how):
            noted.append(15)  # a stop signal arrives while the environment is put together
            return {}
        self.driver.halt = lambda: bool(noted)
        with patch("sortie_kit.lifecycle.builder_env", env):
            result = self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                                 next_owner="builder")
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(self.spawn.started, [])

    def test_the_host_publishes_initial_state_before_it_answers_anything(self):
        (self.shift / "driver-command.json").write_text("{")
        self.assertFalse((self.shift / "driver-state.json").exists())
        self.driver.poll()
        self.assertEqual(decode((self.shift / "driver-response.json").read_bytes())["status"], "invalid")
        state = self.state()
        self.assertEqual((state["mode"], state["active"], state["turn"], state["tasks"]), ("active", None, None, {}))

    def test_the_host_tells_its_caller_whether_the_final_report_was_published(self):
        status = {}
        run_shift(self.admission, self.spawn, seconds=100, wind_down=10, interrupted=[15], status=status)
        self.assertEqual((status["published"], status["shutdown"]),
                         (True, dict(confirmed=True, groups=[], problems=[])))
        with patch.object(Driver, "finish", side_effect=OSError("disk full")):
            run_shift(self.admission, self.spawn, seconds=50, wind_down=10, interrupted=[15], status=status)
        self.assertEqual((status["published"], status["shutdown"]["confirmed"]), (False, True))

    def test_a_lost_turn_first_seen_at_the_ending_keeps_its_reason(self):
        self.begin(running=True)
        self.driver = Driver(self.admission, self.spawn, seconds=900, wind_down=10)
        self.assertTrue(self.driver.finish("host stopped"))
        state = self.state()
        self.assertEqual((state["mode"], state["next_owner"]), ("closed", "developer"))
        self.assertTrue(state["reason"].startswith("host stopped; "), state["reason"])
        self.assertIn("may still run", state["reason"])
        self.assertEqual(self.turns()[0]["outcome"], "lost")
        self.assertEqual(self.spawn.child.signals, [])

    def test_an_observed_end_survives_a_second_failed_write(self):
        self.begin(running=True)
        self.spawn.child.code = 3
        with patch.object(self.driver, "coordination", side_effect=Refused("lock unavailable")), \
                self.assertRaises(Refused):
            self.driver.poll_turn()
        with patch.object(self.driver, "save", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(ValueError, "disk full"):
            self.driver.finish("host stopped")
        self.assertIsNotNone(self.state()["turn"])
        self.assertTrue(self.driver.finish("host stopped"))
        row, = self.turns()
        self.assertEqual((row["outcome"], row["exit"], row["reaped"]), ("exit", 3, True))

    def test_an_unrestored_terminal_or_an_unwritten_final_report_is_not_a_clean_ending(self):
        seat = SimpleNamespace(ended=lambda: True,
                               close=lambda: "exited on its own with status 0; terminal not restored")
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, driver_seat=seat)
        self.assertEqual((code, why), (3, "driver exited"))
        window = decode((self.shift / "report.json").read_bytes())["actual_window"]
        self.assertEqual(window["reason"], why)
        self.assertEqual(window["processes"]["shutdown"],
                         dict(confirmed=False, groups=[], problems=["the terminal could not be restored"]))
        text = (self.shift / "report.md").read_text()
        # Every process was seen to stop: what is wrong is named as what it is.
        self.assertIn("- **Shutdown: NOT CLEAN.** The terminal could not be restored.\n", text)
        self.assertNotIn("leftover", text)
        for failure in (lambda reason: False, OSError("disk full")):
            with self.subTest(failure=failure), patch.object(Driver, "finish", side_effect=failure):
                code, why = run_shift(self.admission, self.spawn, seconds=50, wind_down=10, interrupted=[15])
                self.assertEqual((code, why), (3, "foreground host interrupted; the final report was not written"))

    def test_a_host_stop_noted_during_the_start_checks_starts_no_builder(self):
        noted = []
        def seat_start(role, prompt):
            noted.append(15)
            return ["fixture-agent"]
        self.admission.seat_start = seat_start
        self.driver.halt = lambda: bool(noted)
        result = self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                             next_owner="builder")
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(self.spawn.started, [])
        self.assertTrue(self.turns()[0]["outcome"].startswith("not started: "))

    def test_the_host_loop_gives_the_driver_its_stop_latch(self):
        seen = []
        poll = Driver.poll
        def polling(driver):
            seen.append(driver.halt())
            interrupted.append(15)
            seen.append(driver.halt())
            return poll(driver)
        interrupted = []
        with patch.object(Driver, "poll", polling):
            run_shift(self.admission, self.spawn, seconds=100, wind_down=10, interrupted=interrupted)
        self.assertEqual(seen, [False, True])

    def test_host_answers_a_queued_check_without_running_it_unless_checks_are_on(self):
        seen = {}
        class Recorded(BuilderHost):
            def __init__(inner, admission, driver, enabled=True):
                seen["enabled"] = enabled
                super().__init__(admission, driver, enabled)
        with patch("sortie_kit.host.BuilderHost", Recorded):
            run_shift(self.admission, self.spawn, seconds=2, wind_down=1, sleep=self.clock.sleep)
        self.assertEqual(seen, {"enabled": False})

    def test_interruption_noted_before_the_loop_ends_the_shift_with_a_report(self):
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, interrupted=[15])
        self.assertEqual((code, why), (3, "foreground host interrupted"))
        self.assertTrue((self.shift / "report.md").exists())

    def test_host_stops_the_running_turn_before_the_report(self):
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        order = []
        finish = Driver.finish
        def finishing(driver, reason):
            order.append(("finish", list(self.spawn.child.signals), self.state()["turn"]))
            return finish(driver, reason)
        with patch.object(Driver, "finish", new=finishing):
            code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=10,
                                  sleep=self.clock.sleep)
        self.assertEqual((code, why), (0, "working time over: stopped before the signed deadline"))
        self.assertEqual(decode((self.shift / "driver-response.json").read_bytes())["status"], "recorded")
        self.assertEqual(order, [("finish", ["terminate"], None)])
        row, = self.turns()
        self.assertEqual(row["outcome"], "stopped: working time over: stopped before the signed deadline")
        doc = decode((self.shift / "report.json").read_bytes())
        self.assertEqual(doc["tasks"][0]["turns"][0]["outcome"], row["outcome"])
        self.assertIn("stopped: working time over", (self.shift / "report.md").read_text())

    def test_driver_exit_ends_the_shift_and_stops_the_builder(self):
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        seat = SimpleNamespace(polls=0)
        def ended():
            seat.polls += 1
            return seat.polls > 2
        seat.ended = ended
        order = []
        def close():
            order.append((list(self.spawn.child.signals), (self.shift / "report.md").exists()))
            return "exited on its own with status 0"
        seat.close = close
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=1,
                              sleep=self.clock.sleep, driver_seat=seat)
        self.assertEqual((code, why), (0, "driver exited"))
        self.assertEqual(self.spawn.child.signals, ["terminate"])
        self.assertEqual(self.turns()[0]["outcome"], "stopped: driver exited")
        window = decode((self.shift / "report.json").read_bytes())["actual_window"]
        self.assertEqual(window["reason"], "driver exited")
        # The builder is stopped first, then the driver seat is closed, then the report is written.
        self.assertEqual(order, [(["terminate"], False)])
        self.assertEqual(window["processes"]["driver"], "exited on its own with status 0")
        self.assertIn("not a seat's own stop statement", window["processes"]["note"])


class BuilderTests(LifecycleFixture):
    def fixture_check(self):
        (self.target/'probe.py').write_text("print('baseline failure')\nraise SystemExit(1)\n")
        self.base=self.commit()
        (self.target/'probe.py').write_text("print('candidate success')\n")
        self.candidate=self.commit()
        self.begin('defect')
        self.assertEqual(self.consume_phase()['status'],'advanced')
        task=self.task_state()
        self.assertEqual(task['stage'],'build')
        return dict(version=1,id='reproduce-F1',task_id=task['id'],request_id=task['request_id'],round=task['round'],
                    finding_id='F1',candidate=self.candidate,baseline=self.base,baseline_reason=None,
                    review_request=self.artifact('logs/reviewer-request.txt','CHECKS: run probe.py on baseline and candidate; expect failure then success.\n'),
                    argv=[sys.executable,'probe.py'],timeout_seconds=5,baseline_exit=1,candidate_exit=0,
                    baseline_contains='baseline failure',candidate_contains='candidate success',harness=None,
                    harness_reason='Existing committed probe.py fixture.')

    def test_requested_check_runs_in_the_build_phase_and_is_never_acceptance(self):
        # The builder's own reproduction of a fix: asked for while it builds, before
        # any candidate is bound. The reviewer runs no code.
        spec=self.fixture_check()
        (self.shift/'briefs/check.json').write_bytes(receipts.encoded(spec))
        self.assertEqual(queue_check(self.shift,'briefs/check.json')['status'],'queued')
        self.assertFalse((self.shift/'builder-check-state.json').exists())
        host=BuilderHost(self.admission,self.driver);host.poll()
        reply=decode((self.shift/'builder-check-response.json').read_bytes())
        self.assertEqual(reply['status'],'recorded',reply)
        result=decode((self.shift/reply['evidence']['path']).read_bytes())
        self.assertEqual(result['baseline']['run']['exit_status'],1)
        self.assertEqual(result['candidate']['exit_status'],0)
        self.assertEqual(result['candidate']['outcome'],'matched')
        self.assertTrue((self.shift/result['candidate']['worktree']/'.sortie-home').is_dir())
        self.assertEqual((self.task_state()['round'],self.task_state()['stage'],self.task_state()['accepted']),(0,'build',None))
        self.action('progress',evidence=spec['review_request'])
        self.assertEqual(self.consume_phase()['status'],'advanced')
        self.assertEqual(self.consume_phase()['status'],'advanced')
        self.assertEqual((self.task_state()['stage'],self.task_state()['round']),('accepted',0))
        self.assertEqual(host.apply(spec)['status'],'replayed')

    def test_a_failed_check_is_kept_as_evidence_and_moves_nothing(self):
        spec=self.fixture_check();spec['candidate_contains']='required text absent'
        host=BuilderHost(self.admission,self.driver)
        reply=host.apply(spec)
        self.assertEqual(reply['status'],'blocked',reply)
        result=decode((self.shift/reply['evidence']['path']).read_bytes())
        self.assertEqual((result['status'],result['candidate']['outcome']=='matched'),('blocked',False))
        self.assertEqual((self.task_state()['stage'],self.task_state()['round']),('build',0))
        # Once the build is handed in and the host's review runs, no check is taken.
        self.assertEqual(self.consume_phase()['status'],'advanced')
        late=dict(spec,id='reproduce-late',request_id=self.task_state()['request_id'])
        self.assertEqual(host.apply(late)['status'],'invalid')

    def test_expiry_and_mismatched_candidate_prevent_any_execution(self):
        spec=self.fixture_check();host=BuilderHost(self.admission,self.driver)
        wrong=copy.deepcopy(spec);wrong['request_id']='another-request'
        with patch.object(host,'git',side_effect=AssertionError('no checkout')), patch('sortie_kit.builder_verification.run_command',side_effect=AssertionError('no artifact execution')):
            self.assertEqual(host.apply(wrong)['status'],'invalid')
            self.clock.sleep(1001)
            self.assertEqual(host.apply(spec)['status'],'blocked')
        self.assertFalse((self.shift/'scratch/check-reproduce-F1-baseline').exists())

    def test_checks_switched_off_answer_blocked_and_execute_nothing(self):
        spec = self.fixture_check()
        (self.shift / "briefs/check.json").write_bytes(receipts.encoded(spec))
        queue_check(self.shift, "briefs/check.json")
        host = BuilderHost(self.admission, self.driver, enabled=False)
        with patch.object(host, "git", side_effect=AssertionError("no checkout")), \
                patch("sortie_kit.builder_verification.run_command", side_effect=AssertionError("no execution")):
            host.poll()
        reply = decode((self.shift / "builder-check-response.json").read_bytes())
        self.assertEqual((reply["status"], reply["id"], reply["evidence"]), ("blocked", spec["id"], None))
        self.assertIn("nothing was executed", reply["detail"])
        self.assertFalse((self.shift / "builder-check-state.json").exists())
        self.assertFalse((self.shift / "scratch/check-reproduce-F1-baseline").exists())

    def test_a_check_request_replaced_while_the_host_reads_it_and_an_answer_replaced_while_the_builder_reads_it(self):
        spec = self.fixture_check()
        (self.shift / "briefs/check.json").write_bytes(receipts.encoded(spec))
        self.assertEqual(queue_check(self.shift, "briefs/check.json")["status"], "queued")
        host = BuilderHost(self.admission, self.driver, enabled=False)
        with replaced_while_read(self.shift / "builder-check.json") as done:
            host.poll()
        self.assertEqual(done, [1])
        self.assertEqual(decode((self.shift / "builder-check-response.json").read_bytes())["status"], "blocked")
        second = copy.deepcopy(spec)
        second["id"] = "second"
        (self.shift / "briefs/second.json").write_bytes(receipts.encoded(second))
        # The builder asks again while the host writes its answer to the first request.
        with replaced_while_read(self.shift / "builder-check-response.json") as done:
            self.assertEqual(queue_check(self.shift, "briefs/second.json")["status"], "queued")
        self.assertEqual(done, [1])

    def test_missing_reviewer_request_and_partial_queue_do_not_execute(self):
        spec=self.fixture_check();spec['review_request']['path']='logs/missing'
        self.assertEqual(BuilderHost(self.admission,self.driver).apply(spec)['status'],'invalid')
        (self.shift/'builder-check.json').write_text('{')
        BuilderHost(self.admission,self.driver).poll()
        self.assertEqual(decode((self.shift/'builder-check-response.json').read_bytes())['status'],'invalid')

    def test_queue_requires_matching_reply_and_changed_id_refuses(self):
        spec=self.fixture_check()
        (self.shift/'briefs/check.json').write_bytes(receipts.encoded(spec))
        queue_check(self.shift,'briefs/check.json')
        second=copy.deepcopy(spec);second['id']='second'
        (self.shift/'briefs/second.json').write_bytes(receipts.encoded(second))
        with self.assertRaises(Refused):queue_check(self.shift,'briefs/second.json')
        host=BuilderHost(self.admission,self.driver)
        first=host.apply(spec)
        self.assertEqual(first['status'],'recorded',first)
        spec['candidate_contains']='different'
        self.assertEqual(host.apply(spec)['status'],'invalid')

    def test_execution_timeout_output_bound_and_withdrawal_cleanup(self):
        env=dict(os.environ,HOME=str(self.root),TMPDIR=str(self.root),TMP=str(self.root),TEMP=str(self.root))
        code,raw,reason=run_command([sys.executable,'-c','import time; print("start",flush=True); time.sleep(30)'],str(self.root),env,seconds=2,guard=lambda:'')  # two seconds for an interpreter to start and print
        self.assertIn(b'start',raw)
        self.assertEqual(reason,'duration elapsed')
        code,raw,reason=run_command([sys.executable,'-c','print("x"*10000)'],str(self.root),env,seconds=2,guard=lambda:'',output_limit=100)
        self.assertEqual(len(raw),100);self.assertEqual(reason,'output limit exceeded')
        with self.assertRaises(Refused):
            run_command([sys.executable,'-c','raise SystemExit(0)'],str(self.root),env,seconds=1,guard=lambda:'withdrawn')

    def test_transient_owned_cleanup_denial_is_retried_within_bound(self):
        from sortie_kit.tick_supervise import Supervisor
        original = Supervisor.stop_child
        calls = []
        def transient(owner):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError('transient owned-group observation')
            return original(owner)
        env = dict(os.environ, HOME=str(self.root), TMPDIR=str(self.root), TMP=str(self.root), TEMP=str(self.root))
        with patch.object(Supervisor, 'stop_child', transient):
            _, _, reason = run_command([sys.executable, '-c', 'import time; time.sleep(30)'],  # never over by itself
                                        str(self.root), env, seconds=.05, guard=lambda: '')
        self.assertEqual(reason, 'duration elapsed')
        self.assertEqual(len(calls), 2)

    def test_instructions_are_packaged_and_tiger_is_verbatim(self):
        from importlib.resources import files
        templates=files('sortie_kit').joinpath('data','templates')
        # tiger.md is AGENTS.md without its one block marked as for this repository only:
        # rules for working on sortie-kit itself are not handed to a stranger's seats.
        # A published AGENTS.md has no such block and is tiger.md byte for byte.
        from support import without_repository_block
        source=(Path(__file__).resolve().parents[1]/'AGENTS.md').read_bytes()
        self.assertEqual(templates.joinpath('tiger.md').read_bytes(),without_repository_block(source))
        for item in templates.iterdir():
            self.assertNotIn('this repository only',item.read_text().casefold(),item.name)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['instructions','builder']),0)
        # The review is the host's: the builder is told so and is told of no review tool.
        self.assertIn('You do not run, request or record a review',out.getvalue())
        self.assertIn('`answers`',out.getvalue())
        for word in ('gemini','version-1','agy','check-review','signed_review_model'):
            self.assertNotIn(word,out.getvalue().casefold())
        # tiger.md speaks of a review skill in general; the builder's own page names none.
        self.assertNotIn('skill',templates.joinpath('builder.md').read_text().casefold())
        for name in ('driver.md','builder.md','tiger.md'):
            self.assertTrue((self.shift/name).is_file())

    def test_agents_md_is_read_with_or_without_its_repository_only_block(self):
        from support import BLOCK_BEGIN, BLOCK_END, without_repository_block
        head,block,tail=b'# Rules\n\n- one\n',b'- for this repository\n',b'## More\n\n- two\n'
        # With the block, as in the repository the work is done in.
        self.assertEqual(without_repository_block(head+BLOCK_BEGIN+block+BLOCK_END+tail),head+tail)
        # Without it, as in a published tree: nothing is taken out.
        self.assertEqual(without_repository_block(head+tail),head+tail)
        # An empty block is still a block.
        self.assertEqual(without_repository_block(head+BLOCK_BEGIN+BLOCK_END+tail),head+tail)
        for broken in (head+BLOCK_BEGIN+block+tail, head+block+BLOCK_END+tail,
                       head+BLOCK_END+block+BLOCK_BEGIN+tail,
                       head+BLOCK_BEGIN+block+BLOCK_END+BLOCK_BEGIN+block+BLOCK_END+tail,
                       # Marker text that is not a whole marker line: no newline, CRLF, a misspelling.
                       head+tail+BLOCK_END.rstrip(), head+BLOCK_BEGIN.replace(b'\n',b'\r\n')+block+BLOCK_END.replace(b'\n',b'\r\n')+tail,
                       head+b'<!-- this repository only: start -->\n'+tail,
                       head+BLOCK_BEGIN+block+BLOCK_END+tail+b'<!-- This repository only: end -->',
                       # Text before a marker on its line.
                       head+b'note: '+BLOCK_BEGIN+block+BLOCK_END+tail, head+BLOCK_BEGIN+block+b'  '+BLOCK_END+tail):
            with self.assertRaises(ValueError):
                without_repository_block(broken)
        # The file of this tree, whichever of the two it is, is what the wheel packages.
        from importlib.resources import files
        source=(Path(__file__).resolve().parents[1]/'AGENTS.md').read_bytes()
        packaged=files('sortie_kit').joinpath('data','templates','tiger.md').read_bytes()
        self.assertEqual(without_repository_block(source),packaged)
        self.assertEqual(without_repository_block(packaged),packaged)
