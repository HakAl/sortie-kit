"""Synthetic keys and disposable terminals only; no real panes or credentials."""
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
from unittest.mock import patch

from sortie_kit import agents, grants, launcher, signing
from sortie_kit.bounds import Sources
from sortie_kit.layout import Refused, Request, prepare
from support import Clock, LocalTest, agent_fixture, NOW

TOOL = str(Path(shutil.which('ssh-keygen')).resolve())


def terminal_run(argv, env, password, *, timeout=12, answers=()):
    """The test impersonates a user ONLY with a random synthetic secret in RAM.

    `answers` are (question, reply) pairs typed in order, each when the terminal's
    last line is that question; they are ordinary choices, never a secret.
    """
    answers = list(answers)
    pid, master = pty.fork()
    if pid == 0:
        os.execve(argv[0], argv, env)
    output, pending, prompts, status = b'', b'', 0, None
    began = time.monotonic()
    try:
        while time.monotonic() - began < timeout:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    part = os.read(master, 65536)
                except OSError:
                    part = b''
                output += part
                pending += part
                # The prompt is the final line. The displayed scope is JSON that also
                # contains "passphrase" and colons, so a read boundary inside it must not count.
                last = pending.rstrip().rsplit(b'\n', 1)[-1]
                if answers and last.strip() == answers[0][0]:
                    os.write(master, answers.pop(0)[1] + b'\n')
                    pending = b''
                    continue
                if (last.lstrip().startswith(b'Enter ') and b'passphrase' in last.lower()
                        and last.endswith(b':')):
                    # Assert actual transcript isolation below; master-side
                    # termios flags do not reflect slave echo on every OS.
                    if password is not None:
                        os.write(master, password + b'\n')
                    prompts += 1
                    pending = b''
            reaped, observed = os.waitpid(pid, os.WNOHANG)
            if reaped:
                status = observed
                break
        if status is None:
            raise AssertionError('finite terminal fixture timed out: ' + output.decode(errors='replace'))
        assert not answers, 'questions never asked: ' + repr(answers) + output.decode(errors='replace')
        if password:
            assert password not in output, ('synthetic password leaked to terminal output: '
                                           + repr(output.replace(password, b'<redacted>')))
        return os.waitstatus_to_exitcode(status), output, prompts
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)


POLICY = grants.review_policy('fixture-model', 'fixture-family', 'fixture-review-model', 'Gemini', 'given')
# What formats 2 and 3 signed about the review: the agy-review script and a model.
POLICY_3 = dict(builder_model='fixture-model', builder_family='fixture-family', review_tool='agy-review',
                review_model='fixture-review-model', review_family='gemini', same_family_review=False)
PANES = {'driver': {'pane': 'driver', 'runtime': 'codex'}, 'builder': {'pane': 'builder', 'runtime': 'claude'}}
# The window of the fixture's grant. No test here is about its length: those about expiry, a stale
# admission or the cutoff make a grant of their own and give its clock. It is an hour and not a minute
# because `sortie verify` as a child and `grants.verify` without a clock read the time of day, and a
# machine that slept in the suite ended a window of 60 seconds under a test that asked nothing of it.
WINDOW = 3600


class GrantTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.repo_path = self.repo()
        self.protected = self.root / 'protected'
        self.protected.mkdir(mode=0o700)
        self.key = self.protected / 'signing-key'
        self.env = dict(os.environ, HOME=str(self.protected), TMPDIR=str(self.root),
                        TMP=str(self.root), TEMP=str(self.root), PYTHONDONTWRITEBYTECODE='1',
                        PYTHONPATH=str(Path(grants.__file__).resolve().parent.parent))
        # Crypto-only fixtures use empty passphrases, never the production signer.
        # Separate real-PTY tests below exercise encrypted-key password isolation.
        subprocess.run([TOOL, '-q', '-t', 'ed25519', '-C', 'fixture', '-N', '', '-f', str(self.key)],
                       env=self.env, check=True, capture_output=True, timeout=10)
        self.trust_path = self.protected / 'trust.json'
        self.write_trust()
        self.trust = grants.Trust(self.trust_path, self.repo_path)
        self.request = Request(self.repo_path, self.repo_path / 'direction.md', WINDOW)
        # The profile lives under the fixture HOME so a child CLI process finds it too.
        self.fixture = agent_fixture(self.root, self.repo_path, home=self.protected)
        self.fresh_shift()
        self.began = dt.datetime.now(dt.timezone.utc)
        self.raw = self.grant()
        self.sign_crypto(self.raw)

    def grant(self):
        """A grant of the present shift and seats that starts at the moment `setUp` signed, which is
        the clock `admit` hands an admission: its start is never after that clock."""
        return grants.make_grant(self.shift, self.trust, self.seats, WINDOW, policy=POLICY, now=self.began)

    def fresh_shift(self):
        self.shift = prepare(self.request, Sources())
        self.seats, _ = launcher.all_seats(self.repo_path, self.fixture['choices'], home=self.protected)

    def write_trust(self):
        value = {'version': 1, 'identity': 'fixture-developer',
                 'public_key': ' '.join(self.key.with_suffix('.pub').read_text().split()[:2]),
                 'ssh_keygen': TOOL}
        self.trust_path.write_bytes(grants.encoded(value))
        self.trust_path.chmod(0o600)

    def sign_crypto(self, raw, *, namespace=grants.NAMESPACE, key=None):
        (self.shift / 'grant.json').write_bytes(raw)
        (self.shift / 'grant.json.sig').unlink(missing_ok=True)
        subprocess.run([TOOL, '-Y', 'sign', '-f', str(key or self.key), '-n', namespace,
                        str(self.shift / 'grant.json')], stdin=subprocess.DEVNULL,
                       env=self.env, capture_output=True, check=True, timeout=10)

    def admit(self, **kwargs):
        # The moment of `setUp` and not the time of day, unless the test gives a clock: however late
        # the test gets here, the window and the admission cutoff of a grant from `grant` are open.
        # The 60 seconds are the admission's own duration on the monotonic clock, from here.
        kwargs.setdefault('now', lambda: self.began)
        return grants.Admission(self.shift, self.trust, seconds=60,
                                expected_seats=self.seats, **kwargs)

    def test_real_signature_and_immutable_admission_binding(self):
        self.assertEqual(grants.verify(self.shift, self.trust)[0], self.raw)
        with self.admit() as admitted:
            changed = admitted.value
            changed['allowed_actions'].append('publish')
            changed['seats']['builder']['executable']['path'] = '/outside/agent'
            self.assertEqual(admitted.guard(), '')
            self.assertEqual(admitted.value['seats'], self.seats)
            self.assertEqual(admitted.value['allowed_actions'],
                             ['local-work', 'spawn-builder', 'spawn-driver', 'spawn-reviewer'])
            self.assertEqual(admitted.seat_start('builder', 'go')[0], str(self.fixture['program']))

    def test_the_fixtures_grant_admits_by_the_clock_of_setup_however_late_the_admission_comes(self):
        """A machine that slept in the suite for 51 minutes ended the fixture's window of 60 seconds
        of the time of day, and a test that had nothing to do with expiry failed."""
        real = dt.datetime

        class Late(real):
            @classmethod
            def now(cls, tz=None):
                return real.now(tz) + dt.timedelta(hours=2)
        with patch.object(dt, 'datetime', Late):
            # By the time of day the grant is over, which is what a test of expiry asks about.
            with self.assertRaisesRegex(Refused, 'grant not current'):
                grants.verify(self.shift, self.trust)
            with self.admit() as admitted:
                self.assertEqual(admitted.guard(), '')
                self.assertTrue(admitted.seat_start('builder', 'go'))

    def test_a_grant_signed_with_arguments_the_profile_no_longer_has_still_verifies_and_starts_as_signed(self):
        # What an earlier version signed for a built-in builder: one headless form with an option the
        # profile has since lost, under that version's profile hash. Nothing reads the profile again.
        option = ['-c', 'sandbox_workspace_write.network_access=true']
        self.seats['builder'] = dict(self.seats['builder'], profile_sha256='1' * 64,
                                     argv=['run'] + option + self.seats['builder']['argv'][1:])
        self.raw = self.grant()
        self.sign_crypto(self.raw)
        self.assertEqual(grants.verify(self.shift, self.trust)[0], self.raw)
        with self.admit() as admitted:
            self.assertEqual(admitted.value['seats']['builder']['argv'][:3], ['run'] + option)
            self.assertEqual(admitted.seat_start('builder', 'go')[1:4], ['run'] + option)

    def test_altered_bytes_wrong_signature_namespace_and_signer_refused(self):
        cases = ['changed bytes', 'wrong namespace', 'wrong signer', 'damaged signature']
        for case in cases:
            with self.subTest(case=case):
                self.sign_crypto(self.raw)
                if case == 'changed bytes':
                    (self.shift / 'grant.json').write_bytes(self.raw + b' ')
                elif case == 'wrong namespace':
                    self.sign_crypto(self.raw, namespace='unrelated-namespace')
                elif case == 'wrong signer':
                    other = self.protected / 'other-key'
                    subprocess.run([TOOL, '-q', '-t', 'ed25519', '-C', 'fixture', '-N', '', '-f', str(other)],
                                   env=self.env, check=True, capture_output=True, timeout=10)
                    self.sign_crypto(self.raw, key=other)
                else:
                    (self.shift / 'grant.json.sig').write_bytes(b'damaged')
                with self.assertRaisesRegex(Refused, 'signature'):
                    grants.verify(self.shift, self.trust)

    def test_signed_scope_mismatches_and_unknown_fields_refused(self):
        original = grants.decoded(self.raw)
        mutations = {'repo': str(self.root), 'shift_id': 'another', 'namespace': 'wrong',
                     'identity': 'another', 'direction_sha256': '0' * 64,
                     'allowed_actions': ['publish'], 'finalization_seconds': 11,
                     'until': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat(),
                     'start': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)).isoformat(),
                     'unknown': 'must refuse'}
        for field, value in mutations.items():
            with self.subTest(field=field):
                self.sign_crypto(grants.encoded(dict(original, **{field: value})))
                with self.assertRaises(Refused):
                    grants.verify(self.shift, self.trust)
        self.sign_crypto(self.raw)
        with self.assertRaisesRegex(Refused, 'seats'):
            grants.verify(self.shift, self.trust,
                          expected_seats=dict(self.seats, driver=dict(self.seats['driver'], model='changed')))
        with self.assertRaises(Refused):
            grants.scope(self.raw.replace(b'"version":4', b'"version":4,"version":4'), self.shift, self.trust)
        # Old pane actions and pane seats have no meaning in an admissible grant.
        builder = original['seats']['builder']
        reviewer = original['seats']['reviewer']
        for seats in (PANES, {'driver': original['seats']['driver']},
                      {role: original['seats'][role] for role in ('driver', 'builder')},  # format 3's two seats
                      dict(original['seats'], reviewer=original['seats']['driver']),
                      dict(original['seats'], reviewer=dict(reviewer, model='another-model')),
                      dict(original['seats'], reviewer=dict(reviewer, family='another-family')),
                      dict(original['seats'], reviewer=dict(reviewer, mode='headless')),
                      dict(original['seats'], host=reviewer),
                      dict(original['seats'], builder=dict(builder, pane='w1:p1')),
                      dict(original['seats'], builder=dict(builder, model='another-model')),
                      dict(original['seats'], builder=dict(builder, family='another-family')),
                      dict(original['seats'], builder=dict(builder, mode='interactive')),
                      dict(original['seats'], builder=dict(builder, argv=None)),
                      dict(original['seats'], driver=builder)):
            with self.subTest(seats=seats):
                self.sign_crypto(grants.encoded(dict(original, seats=seats)))
                with self.assertRaises(Refused):
                    grants.verify(self.shift, self.trust)
        for action in ('inventory', 'read', 'prompt'):
            self.sign_crypto(grants.encoded(dict(original, allowed_actions=['local-work', action])))
            with self.subTest(action=action), self.assertRaisesRegex(Refused, 'actions'):
                grants.verify(self.shift, self.trust)

    def test_grant_names_models_and_format_one_is_signature_only(self):
        value = grants.decoded(self.raw)
        self.assertEqual(value['version'], 4)
        self.assertEqual(value['review_policy'], dict(
            builder_model='fixture-model', builder_family='fixture-family', review_choice='given',
            review_model='fixture-review-model', review_family='gemini', same_family_review=False))
        self.assertEqual(value['review_limit_seconds'], 600)
        with self.assertRaises(TypeError):
            grants.make_grant(self.shift, self.trust, self.seats, 60)  # no model is assumed
        for change in (dict(review_tool='agy-review'), dict(review_model='-flag'), dict(review_model=''),
                       dict(builder_model='two words'), dict(builder_family='GPT'),
                       dict(review_family='fixture-family'), dict(same_family_review=1), dict(extra='x'),
                       dict(same_family_review=True), dict(review_family='gemini\n'),
                       dict(review_choice='picked-same-family'), dict(review_choice='asked'),
                       dict(review_choice=None), dict(review_model='fixture-review-model\n')):
            with self.subTest(change=change):
                self.sign_crypto(grants.encoded(dict(value, review_policy=dict(value['review_policy'], **change))))
                with self.assertRaises(Refused):
                    grants.verify(self.shift, self.trust)
        # Spelling cannot turn one family into two.
        with self.assertRaisesRegex(Refused, 'how the reviewer was picked'):
            grants.review_policy('a', 'Claude', 'b', ' claude ', 'picked-other-family')
        with self.assertRaisesRegex(Refused, 'how the reviewer was picked'):
            grants.review_policy('a', 'Claude', 'b', 'gemini', 'picked-same-family')
        # Same-family is a signed fact about the two families, never a switch.
        for choice in ('picked-same-family', 'given'):
            said = grants.review_policy('a', 'Claude', 'b', 'CLAUDE', choice)
            self.assertEqual((said['builder_family'], said['review_family'], said['same_family_review']),
                             ('claude', 'claude', True))
        self.assertFalse(grants.review_policy('a', 'openai', 'b', 'anthropic', 'given')['same_family_review'])
        self.assertTrue(grants.review_policy('a', 'openai', 'b', 'gpt', 'given')['same_family_review'])
        # A policy that reaches the check by another road cannot use a maker's name to pass as two families.
        for alias in (dict(builder_family='openai', review_family='gpt'), dict(builder_family='gpt', review_family='openai')):
            with self.assertRaises(Refused):
                grants.policy_checked(dict(builder_model='a', review_model='b', same_family_review=False,
                                           review_choice='picked-other-family', **alias))
        for limit in (29, 3601, 600.5, '600', True, None):
            with self.subTest(limit=limit):
                self.sign_crypto(grants.encoded(dict(value, review_limit_seconds=limit)))
                with self.assertRaises(Refused):
                    grants.verify(self.shift, self.trust)
                with self.assertRaises(Refused):
                    grants.make_grant(self.shift, self.trust, self.seats, 60, policy=POLICY, review_seconds=limit)
        self.assertEqual(grants.decoded(grants.make_grant(self.shift, self.trust, self.seats, 60, policy=POLICY,
                                                          review_seconds=30.0))['review_limit_seconds'], 30)
        for bad in ('', '-x', 'two words', None, 'x' * 65):
            with self.subTest(family=bad), self.assertRaises(Refused):
                grants.family(bad)
        # Earlier formats keep a checkable signature and nothing else.
        value.pop('review_limit_seconds')
        pane_era = dict(value, version=2, seats=PANES, allowed_actions=['inventory', 'local-work', 'prompt', 'read'],
                        review_policy=dict(POLICY_3, builder_model='m', builder_family='gpt', review_model='r'))
        self.sign_crypto(grants.encoded(pane_era))
        grants.verify(self.shift, self.trust, legacy=True)
        checked = subprocess.run([sys.executable, '-B', '-m', 'sortie_kit', 'verify',
                                  '--shift', str(self.shift), '--trust', str(self.trust_path)],
                                 env=self.env, capture_output=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn(b'format-2 grant', checked.stdout)
        with self.assertRaisesRegex(Refused, 'sign a fresh shift'):
            self.admit()
        for mixed in (dict(pane_era, seats=value['seats']), dict(pane_era, allowed_actions=value['allowed_actions']),
                      dict(value, version=2)):
            self.sign_crypto(grants.encoded(mixed))
            with self.assertRaises(Refused):
                grants.verify(self.shift, self.trust, legacy=True)
        old = {k: v for k, v in pane_era.items() if k != 'review_policy'}
        old['version'] = 1
        self.sign_crypto(grants.encoded(old))
        grants.verify(self.shift, self.trust, legacy=True)
        checked = subprocess.run([sys.executable, '-B', '-m', 'sortie_kit', 'verify',
                                  '--shift', str(self.shift), '--trust', str(self.trust_path)],
                                 env=self.env, capture_output=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn(b'format-1 grant', checked.stdout)
        self.assertIn(b'cannot be admitted', checked.stdout)
        with self.assertRaisesRegex(Refused, 'sign a fresh shift'):
            grants.verify(self.shift, self.trust)
        with self.assertRaisesRegex(Refused, 'sign a fresh shift'):
            self.admit()
        self.sign_crypto(grants.encoded(dict(value, version=1)))
        with self.assertRaises(Refused):
            grants.verify(self.shift, self.trust, legacy=True)

    def format_three(self):
        """The grant a shift signed before the reviewer was a seat: two seats, the review
        through the agy-review script, three actions, no review limit."""
        value = grants.decoded(self.raw)
        value.pop('review_limit_seconds')
        value.update(version=3, review_policy=dict(POLICY_3), allowed_actions=['local-work', 'spawn-builder', 'spawn-driver'],
                     seats={role: value['seats'][role] for role in ('driver', 'builder')})
        return value

    def test_verify_says_that_a_shift_ended_before_it_speaks_of_the_grants_window(self):
        """After a trial shift `sortie verify` said "Window: current until ...", true of the grant
        and read as a shift that still ran."""
        def checked():
            return subprocess.run([sys.executable, '-B', '-m', 'sortie_kit', 'verify',
                                   '--shift', str(self.shift), '--trust', str(self.trust_path)],
                                  env=self.env, capture_output=True, text=True, timeout=10)
        pin = hashlib.sha256((self.shift / 'grant.json').read_bytes()).hexdigest()
        end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(seconds=90)
        until = grants.moment(grants.decoded(self.raw)['until']).strftime('%Y-%m-%d %H:%M:%S UTC')
        ended = 'Shift: ended ' + end.strftime('%Y-%m-%d %H:%M:%S UTC') + ', by the host\'s own record of it'

        def close(raw=None, **changed):
            (self.shift / 'closeout.json').write_bytes(raw if raw is not None else grants.encoded(dict(dict(
                version=1, source='foreground host', end=end.isoformat(), grant_sha256=pin), **changed)))

        def state(mode):
            (self.shift / 'driver-state.json').write_bytes(grants.encoded(dict(mode=mode)))

        def running(what):
            seen = checked()
            self.assertEqual((seen.returncode, seen.stderr), (0, ''), what)
            self.assertIn('Window: current until ' + until, seen.stdout, what)
            self.assertNotIn('Shift:', seen.stdout, what)
        # The host writes a closeout while the shift runs too, and when the driver closes the
        # shift, before it has stopped anything: neither is the end. Its last one says what
        # became of its cleanup.
        close()
        running('a closeout that says nothing of the cleanup')
        close(processes=dict(cleanup='not yet performed'))
        state('active')
        running('a closeout of a shift that runs')
        state('closed')
        running('a closeout written when the driver closed the shift, the host still stopping its processes')
        for odd in (None, 'done', [], dict(cleanup=None), dict(cleanup=['done']), dict(cleanup='')):
            close(processes=odd)
            running('processes of another shape: ' + repr(odd))
        (self.shift / 'driver-state.json').unlink()
        close(processes=dict(cleanup='incomplete: one directory stayed'))
        self.assertIn(ended, checked().stdout)
        close(processes=dict(cleanup='done'))
        seen = checked()
        self.assertEqual((seen.returncode, seen.stderr), (0, ''), seen.stdout)
        lines = seen.stdout.splitlines()
        at = next(n for n, line in enumerate(lines) if line.startswith(ended))
        self.assertIn('not signed', lines[at])
        self.assertEqual(lines[at + 1], "Window: the grant's window runs until " + until + '.')
        self.assertNotIn('current until', seen.stdout)
        self.assertTrue(lines[0].startswith('Signature: verified.'), lines)
        # A closeout of another grant, one that cannot be read, and a time that is none say nothing.
        done = dict(cleanup='done')
        for what, change in (('another grant', lambda: close(grant_sha256='0' * 64, processes=done)),
                             ('no JSON', lambda: close(b'{')), ('no object', lambda: close(b'[]')),
                             ('no time', lambda: close(end='soon', processes=done)),
                             ('a time of another kind', lambda: close(end=7, processes=done))):
            change()
            running(what)
        # The state is not what says it: a host that could not read its state still ended.
        (self.shift / 'driver-state.json').write_bytes(b'{')
        close(processes=done)
        self.assertIn(ended, checked().stdout)
        (self.shift / 'driver-state.json').unlink()
        # A start that was admitted and failed has a closeout that names no cleanup.
        close(source='admitted start failed')
        self.assertIn(ended, checked().stdout)
        # Once the window has passed it is said as before, after the shift's own end.
        value = grants.decoded(self.raw)
        begun = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
        value.update(start=begun.isoformat(), until=(begun + dt.timedelta(hours=1)).isoformat(),
                     admit_before=(begun + dt.timedelta(seconds=120)).isoformat())
        self.sign_crypto(grants.encoded(value))
        close(grant_sha256=hashlib.sha256((self.shift / 'grant.json').read_bytes()).hexdigest(), processes=done)
        past = checked()
        self.assertEqual((past.returncode, past.stderr), (4, ''), past.stdout)
        lines = past.stdout.splitlines()
        at = next(n for n, line in enumerate(lines) if line.startswith(ended))
        self.assertTrue(lines[at + 1].startswith('Window: ended ' + (begun + dt.timedelta(hours=1)).strftime(
            '%Y-%m-%d %H:%M:%S UTC') + '. Nothing is admitted'), lines)

    def test_verify_tells_a_good_signature_on_an_ended_window_from_a_bad_signature(self):
        """On a real shift, once its window had passed, `sortie verify` said only that the grant was
        not current, as it would for a grant nobody signed."""
        def checked():
            return subprocess.run([sys.executable, '-B', '-m', 'sortie_kit', 'verify',
                                   '--shift', str(self.shift), '--trust', str(self.trust_path)],
                                  env=self.env, capture_output=True, text=True, timeout=10)
        now = checked()
        self.assertEqual(now.returncode, 0, now.stderr)
        self.assertIn('Signature: verified', now.stdout)
        self.assertIn('Window: current', now.stdout)
        value = grants.decoded(self.raw)
        begun = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
        value.update(start=begun.isoformat(), until=(begun + dt.timedelta(hours=1)).isoformat(),
                     admit_before=(begun + dt.timedelta(seconds=120)).isoformat())
        ended = grants.encoded(value)
        self.sign_crypto(ended)
        past = checked()
        self.assertEqual((past.returncode, past.stderr), (4, ''), past.stdout)
        self.assertIn('Signature: verified', past.stdout)
        self.assertIn('this shift was signed by the key in the trust file', past.stdout)
        self.assertIn('Window: ended ' + (begun + dt.timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S UTC'), past.stdout)
        self.assertNotIn('current scope', past.stdout)
        self.assertEqual(grants.window(value, self.shift, now=begun - dt.timedelta(seconds=1))[1],
                         'not begun; it starts ' + begun.strftime('%Y-%m-%d %H:%M:%S UTC'))
        # Nothing ended is admitted, and the check an admission makes is as it was.
        for call in (lambda: grants.verify(self.shift, self.trust), self.admit):
            with self.assertRaisesRegex(Refused, 'grant not current'):
                call()
        # The same ended window with bytes nobody signed: a refusal, with its own status.
        (self.shift / 'grant.json').write_bytes(grants.encoded(dict(value, nonce='0' * 32)))
        bad = checked()
        self.assertEqual((bad.returncode, bad.stdout), (3, ''), bad.stderr)
        self.assertIn('grant signature verification failed', bad.stderr)
        # A window that is not sound in itself is refused whenever it is read.
        self.sign_crypto(grants.encoded(dict(value, admit_before=value['until'])))
        self.assertEqual(checked().returncode, 3)
        # Withdrawn while its window is open: a good signature, and not current.
        self.sign_crypto(self.raw)
        (self.shift / 'STOP').write_text('{}')
        stopped = checked()
        self.assertEqual(stopped.returncode, 4, stopped.stderr)
        self.assertIn('Signature: verified', stopped.stdout)
        self.assertIn('withdrawn', stopped.stdout)

    def test_a_format_three_grant_still_verifies_and_is_refused_at_admission(self):
        old = self.format_three()
        self.sign_crypto(grants.encoded(old))
        self.assertEqual(grants.verify(self.shift, self.trust, legacy=True)[0], grants.encoded(old))
        checked = subprocess.run([sys.executable, '-B', '-m', 'sortie_kit', 'verify',
                                  '--shift', str(self.shift), '--trust', str(self.trust_path)],
                                 env=self.env, capture_output=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn(b'format-3 grant', checked.stdout)
        self.assertIn(b'sign the shift again', checked.stdout)
        for call in (lambda: grants.verify(self.shift, self.trust), self.admit,
                     lambda: grants.Admission(self.shift, self.trust, seconds=60, expected_seats=old['seats'])):
            with self.assertRaisesRegex(Refused, 'no reviewer seat.*sign the shift again'):
                call()
        self.assertFalse(list((self.repo_path / '.sortie').glob('admissions/*')))  # no nonce was spent on it
        # Under its own rules, not the present ones: what format 4 signs has no place in it, and the reverse.
        new = grants.decoded(self.raw)
        for mixed in (dict(old, seats=new['seats']), dict(old, review_policy=new['review_policy']),
                      dict(old, allowed_actions=new['allowed_actions']), dict(old, review_limit_seconds=600),
                      dict(old, review_policy=dict(POLICY_3, review_tool='other')),
                      dict(new, version=3), dict(new, review_policy=dict(POLICY_3)),
                      dict(new, seats=old['seats'])):
            with self.subTest(mixed=sorted(mixed)):
                self.sign_crypto(grants.encoded(mixed))
                with self.assertRaises(Refused):
                    grants.verify(self.shift, self.trust, legacy=True)

    def test_the_reviewer_is_started_only_where_the_host_made_its_directory_and_only_while_pinned(self):
        made = self.shift / 'reviews' / 'request-1'
        made.mkdir(parents=True)
        (self.shift / 'reviews' / 'a-file').write_text('not a directory')
        os.symlink(made, self.shift / 'reviews' / 'a-link')
        with self.admit() as admitted:
            argv = admitted.seat_start('reviewer', 'Read REVIEW.md', cwd=str(made))
            seat = self.seats['reviewer']
            self.assertEqual(argv, [str(self.fixture['program']), 'review', '--read-only', '--model',
                                    'fixture-review-model', '--cd', str(made), 'Read REVIEW.md'])
            self.assertEqual((seat['mode'], seat['model'], seat['family']), ('review', 'fixture-review-model', 'gemini'))
            for where in (None, str(self.repo_path), str(self.shift), str(self.shift / 'reviews'),
                          str(made / 'deeper'), 'reviews/request-1', str(self.shift / 'reviews' / 'a-file'),
                          str(self.shift / 'reviews' / 'a-link'), str(self.shift / 'reviews' / 'missing'),
                          str(self.shift / 'logs')):
                with self.subTest(where=where), self.assertRaisesRegex(Refused, "under the shift's reviews"):
                    admitted.seat_start('reviewer', 'Read REVIEW.md', cwd=where)
            for role in ('driver', 'builder'):
                with self.assertRaisesRegex(Refused, 'only the reviewer'):
                    admitted.seat_start(role, 'go', cwd=str(made))

    def test_a_reviewer_whose_executable_changed_since_signing_is_refused(self):
        # The fixture's three seats share one program, so the reviewer gets its own to change.
        other = self.fixture['program'].with_name('fixture-reviewer')
        shutil.copy(self.fixture['program'], other)
        pin, _ = agents.hash_executable(os.path.realpath(other), self.repo_path)
        self.seats = dict(self.seats, reviewer=dict(self.seats['reviewer'], executable=pin))
        raw = self.grant()
        self.sign_crypto(raw)
        other.write_text('#!/bin/sh\nexit 1\n')
        with self.assertRaisesRegex(Refused, 'differs from the signed pin'):
            self.admit()
        self.fresh_shift()
        shutil.copy(self.fixture['program'], other)
        self.seats = dict(self.seats, reviewer=dict(self.seats['reviewer'], executable=pin))
        self.sign_crypto(self.grant())
        with self.admit() as admitted:
            made = self.shift / 'reviews' / 'r'
            made.mkdir(parents=True)
            self.assertTrue(admitted.seat_start('reviewer', 'go', cwd=str(made)))
            self.assertTrue(admitted.seat_start('builder', 'go'))
            other.write_text('#!/bin/sh\nexit 2\n')
            with self.assertRaisesRegex(Refused, 'reviewer agent executable replaced or rewritten after admission'):
                admitted.seat_start('reviewer', 'go', cwd=str(made))
            self.assertTrue(admitted.seat_start('builder', 'go'))  # the other seats' programs are as signed

    def test_a_grant_without_the_reviewer_action_starts_no_reviewer(self):
        value = grants.decoded(self.raw)
        value['allowed_actions'].remove('spawn-reviewer')
        self.sign_crypto(grants.encoded(value))
        made = self.shift / 'reviews' / 'r'
        made.mkdir(parents=True)
        with self.admit() as admitted:
            self.assertTrue(admitted.seat_start('builder', 'go'))
            with self.assertRaisesRegex(Refused, 'does not authorize starting the reviewer'):
                admitted.seat_start('reviewer', 'go', cwd=str(made))

    def test_concurrent_ownership_and_replay_even_after_clean_close(self):
        with self.admit():
            with self.assertRaisesRegex(Refused, 'another shift'):
                self.admit()
        with self.assertRaisesRegex(Refused, 'replay'):
            self.admit()

    def test_stale_admission_and_copied_shift_refused(self):
        old = grants.make_grant(self.shift, self.trust, self.seats, 3600, policy=POLICY,
                                now=self.began - dt.timedelta(minutes=3))
        self.sign_crypto(old)
        with self.assertRaisesRegex(Refused, 'stale'):
            self.admit()
        copied = self.shift.with_name('copied-shift')
        shutil.copytree(self.shift, copied)
        with self.assertRaisesRegex(Refused, 'shift'):
            grants.verify(copied, self.trust)

    def test_delayed_admission_refuses_at_cutoff_and_preserves_nonce(self):
        for delay_at in ('publication', 'final authority read'):
            for elapsed in (120, 121):
                with self.subTest(delay_at=delay_at, elapsed=elapsed):
                    self.fresh_shift()
                    raw = grants.make_grant(self.shift, self.trust, self.seats, 3600, policy=POLICY, now=NOW)
                    self.sign_crypto(raw)
                    marker = self.repo_path / '.sortie/admissions' / grants.decoded(raw)['nonce']
                    clock = Clock()
                    publish, read_file = grants.publish, grants.read_file
                    marker_reads = 0

                    def delayed_publish(path, binding):
                        if delay_at == 'publication':
                            clock.value = elapsed
                        return publish(path, binding)

                    def delayed_read(path, *args, **kwargs):
                        nonlocal marker_reads
                        result = read_file(path, *args, **kwargs)
                        if path == marker:
                            marker_reads += 1
                            # First read pins the published nonce; the second is
                            # the final guard's authority validation.
                            if delay_at == 'final authority read' and marker_reads == 2:
                                clock.value = elapsed
                        return result

                    with patch.object(grants, 'publish', delayed_publish), \
                            patch.object(grants, 'read_file', delayed_read):
                        with self.assertRaisesRegex(Refused, 'stale admission'):
                            with grants.Admission(self.shift, self.trust, seconds=3600,
                                                  expected_seats=self.seats,
                                                  now=clock.utc, clock=clock.now):
                                pass
                    binding = marker.read_bytes()
                    self.assertEqual(grants.decoded(binding)['grant_sha256'],
                                     hashlib.sha256(raw).hexdigest())
                    # Rolling the fixture clock back must not make the consumed
                    # grant reusable. Reaching replay refusal also proves that
                    # the failed admission released its owner lock.
                    clock.value = 119
                    with self.assertRaisesRegex(Refused, 'replay'):
                        with grants.Admission(self.shift, self.trust, seconds=3600,
                                              expected_seats=self.seats,
                                              now=clock.utc, clock=clock.now):
                            pass
                    self.assertEqual(marker.read_bytes(), binding)

    def test_timely_admission_remains_usable_after_cutoff_until_expiry(self):
        raw = grants.make_grant(self.shift, self.trust, self.seats, 3600, policy=POLICY, now=NOW)
        self.sign_crypto(raw)
        clock = Clock()
        publish = grants.publish

        def delayed_publish(path, binding):
            clock.value = 119
            return publish(path, binding)

        with patch.object(grants, 'publish', delayed_publish):
            with grants.Admission(self.shift, self.trust, seconds=3600,
                                  expected_seats=self.seats,
                                  now=clock.utc, clock=clock.now) as admitted:
                self.assertEqual(admitted.guard(), '')
                clock.value = 121
                self.assertEqual(admitted.guard(), '')
                self.assertTrue(admitted.seat_start('builder', 'work'))
                clock.value = 3600
                with self.assertRaisesRegex(Refused, 'elapsed'):
                    admitted.seat_start('builder', 'work')

    def test_replacement_and_stop_are_sticky(self):
        with self.admit() as admitted:
            grants.stop(self.shift)
            self.assertTrue(admitted.guard())
            (self.shift / 'STOP').unlink()
            self.assertTrue(admitted.guard())
        # A separate fresh shift is needed even after clean close.
        self.fresh_shift()
        self.raw = self.grant()
        self.sign_crypto(self.raw)
        with self.admit() as admitted:
            temp = self.shift / 'new-grant'
            temp.write_bytes(self.raw)
            temp.replace(self.shift / 'grant.json')
            self.assertIn('replaced', admitted.guard())

    def test_trust_replacement_and_direction_change_stop_admission(self):
        with self.admit() as admitted:
            self.write_trust()
            # Same bytes/inode remain equivalent; a different key/identity does not.
            value = grants.decoded(self.trust_path.read_bytes())
            value['identity'] = 'another-developer'
            self.trust_path.write_bytes(grants.encoded(value))
            self.assertIn('trust', admitted.guard())
        self.write_trust()
        (self.shift / 'direction.md').write_text('changed direction')
        with self.assertRaisesRegex(Refused, 'direction'):
            grants.verify(self.shift, self.trust)

    def test_permissions_symlinks_and_repo_local_trust_refused(self):
        self.trust_path.chmod(0o644)
        with self.assertRaisesRegex(Refused, 'permissions'):
            grants.Trust(self.trust_path, self.repo_path)
        self.trust_path.chmod(0o600)
        link = self.protected / 'link'
        link.symlink_to(self.trust_path)
        with self.assertRaisesRegex(Refused, 'symlink'):
            grants.Trust(link, self.repo_path)
        local = self.repo_path / 'trust.json'
        local.write_bytes(self.trust_path.read_bytes())
        local.chmod(0o600)
        with self.assertRaisesRegex(Refused, 'outside'):
            grants.Trust(local, self.repo_path)

    def test_monotonic_expiry_and_source_change(self):
        clock = Clock()
        raw = grants.make_grant(self.shift, self.trust, self.seats, 2, policy=POLICY, now=NOW)
        self.sign_crypto(raw)
        with self.admit(now=clock.utc, clock=clock.now) as admitted:
            clock.sleep(3)
            self.assertIn('elapsed', admitted.guard())
        self.fresh_shift()
        self.raw = self.grant()
        self.sign_crypto(self.raw)
        source = self.root / 'sources'
        source.mkdir()
        (source / 'code').write_text('original')
        with self.admit(sources=Sources(source)) as admitted:
            (source / 'code').write_text('replacement')
            self.assertIn('source', admitted.guard())

    def test_seat_start_is_the_signed_argv_and_nothing_from_the_caller(self):
        with self.admit() as admitted:
            builder = admitted.seat_start('builder', 'Read builder.md; --model other')
            driver = admitted.seat_start('driver', 'Start the shift')
        program = str(self.fixture['program'])
        self.assertEqual(builder, [program, 'run', '--model', 'fixture-model', '--cd', str(self.repo_path),
                                   'Read builder.md; --model other'])
        self.assertEqual(driver, [program, '--model', 'fixture-model', 'Start the shift'])
        self.assertFalse((self.shift / 'runtime').exists())  # no permission file is written for a seat
        self.assertFalse(any(self.root.rglob('ran')))  # nothing was executed to learn this

    def replace_program(self):
        program = self.fixture['program']
        raw = program.read_bytes()
        program.unlink()
        program.write_bytes(raw)  # same bytes, another file
        program.chmod(0o755)

    def test_seat_start_refuses_after_withdrawal_or_an_executable_change(self):
        changes = {
            # The same bytes in another file: the host compares the file and reads no byte, and says so.
            'builder agent executable replaced or rewritten after admission: the file at the pinned path is '
            'another file or has other size or times; its bytes were not read again$': self.replace_program,
            'builder agent executable cannot be looked at after admission$': self.fixture['program'].unlink,
            'withdrawn': lambda: grants.stop(self.shift),
            'option': None,
        }
        for message, change in changes.items():
            with self.subTest(message=message):
                self.fresh_shift()
                self.sign_crypto(self.grant())
                with self.admit() as admitted:
                    self.assertTrue(admitted.seat_start('builder', 'go'))
                    if change:
                        change()
                    with self.assertRaisesRegex(Refused, message):
                        admitted.seat_start('builder', '--go' if change is None else 'go')
                if not self.fixture['program'].exists():
                    self.fixture['program'].write_text('#!/bin/sh\nexit 0\n')
                    self.fixture['program'].chmod(0o755)

    def test_admission_rechecks_the_executable_pin_before_consuming_the_nonce(self):
        program = self.fixture['program']
        original = program.read_bytes()
        changes = {'differs from the signed pin': lambda: program.write_bytes(original + b'# changed\n'),
                   'group or world writable': lambda: program.chmod(0o775)}
        ledger = self.repo_path / '.sortie/admissions'
        for message, change in changes.items():
            with self.subTest(message=message):
                change()
                with self.assertRaisesRegex(Refused, message):
                    self.admit()
                self.assertFalse(ledger.exists() and list(ledger.iterdir()))
                program.write_bytes(original)
                program.chmod(0o755)
        with self.admit() as admitted:  # the same grant is still unused
            self.assertEqual(admitted.guard(), '')

    def during_pin_checks(self, change):
        """Patch for agents.check_executable: the last seat's real check passes, then `change` happens."""
        real, calls = agents.check_executable, []
        def checked(*args):
            seen = real(*args)
            calls.append(1)
            if len(calls) == 2:
                change()
            return seen
        return patch.object(agents, 'check_executable', checked)

    def test_seat_start_refuses_an_executable_replaced_during_its_own_checks(self):
        real = agents.render_argv
        def rendered(*args, **kwargs):
            argv = real(*args, **kwargs)
            self.replace_program()  # after the first comparison has passed
            return argv
        with self.admit() as admitted:
            with patch.object(agents, 'render_argv', rendered):
                with self.assertRaisesRegex(Refused, 'executable replaced or rewritten after admission'):
                    admitted.seat_start('builder', 'go')

    def test_admission_refuses_an_executable_replaced_during_later_pin_checks(self):
        ledger = self.repo_path / '.sortie/admissions'
        with self.during_pin_checks(self.replace_program):
            with self.assertRaisesRegex(Refused, 'executable changed'):
                self.admit()
        self.assertFalse(ledger.exists() and list(ledger.iterdir()))
        with self.admit() as admitted:  # the nonce was not consumed
            self.assertEqual(admitted.guard(), '')

    def test_withdrawal_during_pin_checks_refuses_before_the_nonce_is_consumed(self):
        ledger = self.repo_path / '.sortie/admissions'
        with self.during_pin_checks(lambda: grants.stop(self.shift)):
            with self.assertRaisesRegex(Refused, 'withdrawn'):
                self.admit()
        self.assertFalse(ledger.exists() and list(ledger.iterdir()))

    def test_owner_lock_replaced_during_pin_checks_refuses_before_the_nonce_is_consumed(self):
        lock = self.repo_path / '.sortie/owner.lock'
        def replace_lock():
            lock.unlink()
            lock.write_bytes(b'')  # another file at the same path; the held lock is the old one
        ledger = self.repo_path / '.sortie/admissions'
        with self.during_pin_checks(replace_lock):
            with self.assertRaisesRegex(Refused, 'owner lock replaced'):
                self.admit()
        self.assertFalse(ledger.exists() and list(ledger.iterdir()))

    def test_duration_ending_during_the_last_checks_refuses_before_the_nonce_is_consumed(self):
        ticks = [0.0]
        class Slow(Sources):
            def guard(inner):
                ticks[0] += 100  # the source check outlasts the 60 s duration; wall time does not move
                return super().guard()
        ledger = self.repo_path / '.sortie/admissions'
        with self.assertRaisesRegex(Refused, 'duration elapsed'):
            self.admit(sources=Slow(), clock=lambda: ticks[0])
        self.assertFalse(ledger.exists() and list(ledger.iterdir()))

    def test_grant_without_a_spawn_action_cannot_start_that_seat(self):
        value = grants.decoded(self.raw)
        value['allowed_actions'].remove('spawn-builder')
        self.sign_crypto(grants.encoded(value))
        with self.admit() as admitted:
            self.assertTrue(admitted.seat_start('driver', 'go'))
            with self.assertRaisesRegex(Refused, 'does not authorize starting the builder'):
                admitted.seat_start('builder', 'go')

    def test_admission_refuses_seats_other_than_the_requested_ones(self):
        raw, signature = (self.shift / 'grant.json').read_bytes(), (self.shift / 'grant.json.sig').read_bytes()
        other = dict(self.seats, builder=dict(self.seats['builder'], profile_sha256='0' * 64))
        with self.assertRaisesRegex(Refused, 'differ from the requested agents'):
            grants.Admission(self.shift, self.trust, seconds=60, expected_seats=other)
        self.assertEqual((self.shift / 'grant.json').read_bytes(), raw)
        self.assertEqual((self.shift / 'grant.json.sig').read_bytes(), signature)

    def test_expiry_during_verification_never_admits(self):
        clock = Clock()
        self.sign_crypto(grants.make_grant(self.shift, self.trust, self.seats, 2, policy=POLICY, now=NOW))
        real = subprocess.run
        def advance(*args, **kwargs):
            result = real(*args, **kwargs)
            clock.sleep(3)
            return result
        with patch('sortie_kit.grants.subprocess.run', side_effect=advance):
            with self.assertRaisesRegex(Refused, 'stale|positive|elapsed'):
                self.admit(now=clock.utc, clock=clock.now)

    def test_unencrypted_key_and_missing_terminal_refused(self):
        with self.assertRaisesRegex(Refused, 'unencrypted'):
            signing.encrypted_key(self.key.read_bytes(), self.trust.key_blob)
        with patch('os.isatty', return_value=False), self.assertRaisesRegex(Refused, 'terminal'):
            signing.terminal()

    def encrypted_fixture(self):
        self.key.unlink()
        self.key.with_suffix('.pub').unlink()
        secret = secrets.token_urlsafe(24).encode()  # Never a user credential.
        code, output, prompts = terminal_run([TOOL, '-q', '-t', 'ed25519', '-a', '16', '-C', 'fixture',
                                               '-f', str(self.key)], self.env, secret)
        self.assertEqual(code, 0, output)
        self.assertEqual(prompts, 2)
        self.write_trust()
        self.trust = grants.Trust(self.trust_path, self.repo_path)
        signing.encrypted_key(self.key.read_bytes(), self.trust.key_blob)
        return secret

    def test_real_encrypted_terminal_signing_password_isolation(self):
        secret = self.encrypted_fixture()
        self.shift = prepare(self.request, Sources())
        audit = self.root / 'process-audit.json'
        askpass = self.root / 'askpass'
        askpass.write_text('#!/bin/sh\ntouch "' + str(self.root / 'askpass-ran') + '"\nexit 1\n')
        askpass.chmod(0o700)
        script = self.root / 'sign-fixture.py'
        script.write_text(
            'import builtins, getpass, json, os, subprocess, sys\nfrom pathlib import Path\n'
            'from sortie_kit import grants, signing\n'
            'from sortie_kit.cli import main\n'
            'def forbidden(*a, **kw): raise AssertionError("no Python password read")\n'
            'builtins.input = getpass.getpass = forbidden\n'
            'original = subprocess.Popen\n'
            'def observed(argv, **kw):\n'
            '    if argv[1:3] == ["-Y", "sign"]:\n'
            '        assert kw["stdin"] == kw["stdout"] == kw["stderr"]\n'
            '        assert os.isatty(kw["stdin"])\n'
            f'        Path({str(audit)!r}).write_text(json.dumps({{"argv": argv, "env": kw["env"]}}))\n'
            '    return original(argv, **kw)\n'
            'subprocess.Popen = observed\n'
            f'sys.exit(main({["start", "--repo", str(self.repo_path), "--direction", str(self.request.direction), "--for", "30s", "--sign-only", "--trust", str(self.trust_path), "--key", str(self.key), "--agent", "driver=fixture:fixture-model", "--agent", "builder=fixture:fixture-model", "--agent", "reviewer=fixture:fixture-review-model"]!r}))\n'
        )
        before = set(self.shift.parent.iterdir())
        poisoned = dict(self.env, SSH_AUTH_SOCK=str(self.root / 'agent-socket'), SSH_ASKPASS=str(askpass),
                        SSH_ASKPASS_REQUIRE='force', DISPLAY='synthetic:0')
        code, output, prompts = terminal_run([sys.executable, '-B', str(script)], poisoned, secret)
        self.assertEqual(code, 0, output.decode(errors='replace'))
        self.assertEqual(prompts, 1)
        invoked = json.loads(audit.read_text())
        self.assertNotIn('SSH_AUTH_SOCK', invoked['env'])
        self.assertNotIn('SSH_ASKPASS', invoked['env'])
        self.assertEqual(invoked['env']['SSH_ASKPASS_REQUIRE'], 'never')
        self.assertNotIn('DISPLAY', invoked['env'])
        self.assertFalse((self.root / 'askpass-ran').exists())
        # Includes keys, signed scope, receipts, journal, subprocess audit, and
        # every scratch file accessible to the simulated seats under this root.
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes(), str(path))
        self.assertNotIn(secret, output)
        added = set(self.shift.parent.iterdir()) - before
        # The plain-words summary comes first, then the exact bytes that are signed.
        # One line end, however the terminal wrote it: with a write of several kilobytes the
        # macOS terminal driver was seen to give one line two carriage returns (at a 1024 byte
        # boundary of its output), which a terminal shows no differently.
        shown = re.sub(rb'\r+\n', b'\n', output)
        raw = (next(iter(added)) / 'grant.json').read_bytes()
        self.assertLess(shown.index(b'Shift to sign\n  Repository: '), shown.index(b'  What the builder may do:\n'))
        self.assertIn(b'Builder:    fixture, model fixture-model (family fixture-family), headless', shown)
        # The reviewer is shown with how it was chosen, its command and what it can reach, and signed.
        self.assertIn(b'Reviewer:   fixture, model fixture-review-model (family gemini), headless, started by the '
                      b'host for each review\n              given with --agent reviewer=\n', shown)
        self.assertIn(b'--model fixture-review-model --cd ', shown)
        self.assertLess(shown.index(b'  What the reviewer can reach:\n'), shown.index(b'\nExact bytes being signed:\n'))
        self.assertNotIn(b'same-family review', shown)
        self.assertNotIn(b'Review model', shown)  # nothing about the reviewer was asked
        self.assertIn(b'\nExact bytes being signed:\n' + raw, shown)
        self.assertEqual(len(added), 1)
        signed = added.pop()
        value = grants.decoded(grants.verify(signed, self.trust)[0])
        self.assertEqual(value['version'], 4)
        self.assertEqual(value['review_policy'], dict(POLICY))
        self.assertEqual(value['seats']['reviewer']['model'], 'fixture-review-model')
        for role in ('driver', 'builder'):
            seat = value['seats'][role]
            self.assertEqual((seat['agent'], seat['model'], seat['executable']['path']),
                             ('fixture', 'fixture-model', str(self.fixture['program'])))
            self.assertNotIn('config', seat)
        self.assertFalse((signed / 'runtime').exists())
        checked = subprocess.run([sys.executable, '-B', '-m', 'sortie_kit', 'verify',
                                  '--shift', str(signed), '--trust', str(self.trust_path)],
                                 env=self.env, capture_output=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_signing_deadline_kills_prompt_and_restores_terminal(self):
        self.encrypted_fixture()
        self.shift = prepare(self.request, Sources())
        script = self.root / 'timeout-fixture.py'
        script.write_text(
            'import os, termios\nfrom pathlib import Path\n'
            'from sortie_kit import grants, signing\nfrom sortie_kit.layout import Refused\n'
            f'trust = grants.Trust(Path({str(self.trust_path)!r}), Path({str(self.repo_path)!r}))\n'
            f'shift = Path({str(self.shift)!r})\n'
            f'raw = grants.make_grant(shift, trust, {self.seats!r}, 30, policy={POLICY!r})\n'
            'before = termios.tcgetattr(0)\n'
            'try:\n'
            f'    signing.sign(shift, trust, Path({str(self.key)!r}), raw, seconds=0.3)\n'
            'except Refused as exc:\n'
            '    assert "elapsed" in str(exc), str(exc)\n'
            'else: raise AssertionError("signed without password")\n'
            'assert termios.tcgetattr(0) == before\n'
        )
        began = time.monotonic()
        code, output, prompts = terminal_run([sys.executable, '-B', str(script)], self.env, None)
        self.assertEqual(code, 0, output)
        self.assertEqual(prompts, 1)
        self.assertLess(time.monotonic() - began, 3)
        self.assertFalse((self.shift / 'grant.json.sig').exists())
        self.assertFalse((self.repo_path / '.sortie/admissions').exists())
        # What a start that ends at the prompt leaves, as the walkthrough says: the shift's folder
        # with the grant that was shown and no signature.
        self.assertTrue((self.shift / 'grant.json').is_file())

    def test_echo_control_failure_refuses_before_signer_or_scope_write(self):
        self.encrypted_fixture()
        self.shift = prepare(self.request, Sources())
        raw = grants.make_grant(self.shift, self.trust, self.seats, 30, policy=POLICY)
        with patch('sortie_kit.signing.terminal', return_value=os.dup(0)), \
             patch('termios.tcgetattr', side_effect=termios.error('fixture denial')), \
             patch('subprocess.Popen', side_effect=AssertionError('must not prompt')):
            with self.assertRaisesRegex(Refused, 'echo control'):
                signing.sign(self.shift, self.trust, self.key, raw, seconds=30)
        self.assertFalse((self.shift / 'grant.json').exists())
