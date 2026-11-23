import contextlib
import copy
import datetime as dt
import io
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

from sortie_kit import cli, receipts, report, launcher, host
from sortie_kit.bounds import Budget
from sortie_kit.lifecycle import Driver
from sortie_kit.layout import Refused
from sortie_kit.receipt_schema import asset, decode, validate_shape
from support import LocalTest, Clock, FakeSpawn


def kept_v2(name):
    """A version 2 receipt template as earlier shifts wrote it: kept beside the tests, no longer packaged."""
    from pathlib import Path
    return (Path(__file__).resolve().parent / 'fixtures' / 'receipts-v2' / name).read_bytes()


class ReportTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.repo_path = self.root / 'target'; self.repo_path.mkdir()
        self.shift = self.repo_path / '.sortie/shifts/current'; self.shift.mkdir(parents=True)
        for name in ('briefs','receipts','logs','scratch'):
            (self.shift/name).mkdir()
        self.clock = Clock()
        self.artifact('direction.md', 'selected work')
        self.brief = self.artifact('briefs/task.md', 'build a fixture')
        self.grant = dict(start=self.clock.utc().isoformat(), until=(self.clock.utc()+dt.timedelta(seconds=100)).isoformat(),
                          seats={r:dict(agent='fixture',runtime='codex') for r in ('driver','builder')},
                          review_policy=dict(builder_model='example-builder-model',builder_family='example-builder-family',
                                             review_tool='agy-review',review_model='example-review-model',
                                             review_family='example-review-family',same_family_review=False),
                          allowed_actions=['local-work'], finalization_seconds=10,
                          direction_sha256=receipts.digest((self.shift/'direction.md').read_bytes()))
        raw=receipts.encoded(self.grant); (self.shift/'grant.json').write_bytes(raw)
        (self.shift/'grant.json.sig').write_text('fixture signature, never verified\n')
        # The kept grant is an earlier shift's: two seats and a review tool. The present host
        # drives only a grant with a reviewer seat, so the fixture's host is given one to write
        # the closeout with; every report here is made from the kept file.
        driving=dict(self.grant,seats=dict(self.grant['seats'],reviewer=dict(agent='fixture',runtime='codex')),
                     review_policy=dict(self.grant['review_policy'],review_choice='given'),review_limit_seconds=600)
        self.admission=SimpleNamespace(shift=self.shift,raw=raw,value=driving,now=self.clock.utc,
                                      budget=Budget(100,clock=self.clock.now),guard=lambda:'')
        self.spawn=FakeSpawn()
        self.driver=Driver(self.admission,self.spawn,seconds=100,wind_down=10)
        self.state=self.driver.initial()
        self.task=self.driver.new_task('task','defect',self.brief,'builder')
        self.task.update(stage='closed',candidate=dict(kind='commit',commit='a'*40))
        self.state['tasks']['task']=self.task
        self.entries=[]
        self.add_receipt('build'); ref=self.add_receipt('review')
        self.task['accepted']=dict(candidate=self.task['candidate'],receipt=ref)
        self.store()

    def artifact(self,path,text):
        p=self.shift/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        return dict(path=path,sha256=receipts.digest(p.read_bytes()))

    def add_receipt(self,phase):
        value=decode(kept_v2(phase+'-v2.json'))
        value.update(receipt_id=phase,shift_id='current',task_id='task',request_id=phase,role='builder',round=0,brief=self.brief)
        data=value['data']
        if phase=='build':
            data['commit']='a'*40;data['checks'][0]['output']=self.artifact('logs/tests.txt','reported pass')
        elif phase=='review':
            data['target']=self.task['candidate'];i=data['invocations'][0];i['target']=self.task['candidate']
            i['argv'][3]='a'*40;i['raw_output']=self.artifact('logs/review.txt','ACCEPT')
        else:
            data.update(status='stopped',scope='seat',evidence=[self.artifact('logs/stop.txt','seat reports stopped')])
        raw=receipts.encoded(value);path='receipts/'+phase+'.json';(self.shift/path).write_bytes(raw)
        ref=dict(path=path,sha256=receipts.digest(raw),stage=phase,round=0,status='satisfied')
        self.task['receipts'].append(ref)
        slot=receipts.digest(receipts.encoded([value[k] for k in ('shift_id','task_id','request_id','role','phase','round')]))
        self.entries.append(dict(receipt_id=phase,slot=slot,sha256=ref['sha256'],satisfies_phase=True))
        return ref

    def store(self):
        (self.shift/'driver-state.json').write_bytes(receipts.encoded(self.state))
        (self.shift/'receipts/.consumed.json').write_bytes(receipts.encoded(dict(version=1,entries=self.entries)))

    @contextlib.contextmanager
    def no_external(self):
        with patch('subprocess.Popen',side_effect=AssertionError('no subprocess during report')), \
             patch('socket.socket.connect',side_effect=AssertionError('no network during report')), \
             patch.object(self.spawn,'failure',AssertionError('no agent start during report')):
            yield

    def generate(self,**kw):
        with self.no_external():
            return report.finalize(self.shift,3,**kw)

    def test_normal_host_closeout_preserves_reviews_checks_and_unknown_seat_stop(self):
        self.clock.sleep(12)
        with self.no_external(): self.driver.finish('selected work complete')
        doc=decode((self.shift/'report.json').read_bytes())
        self.assertEqual(doc['status'],'evidence summarized')
        self.assertIn('retained review matches candidate',doc['tasks'][0]['acceptance'])
        self.assertEqual(doc['actual_window']['elapsed_seconds'],12)
        self.assertIn('unknown',doc['tasks'][0]['cessation'])
        self.assertEqual(doc['tasks'][0]['receipts'][0]['data']['checks'][0]['exit_status'],0)
        self.assertEqual(doc['tasks'][0]['receipts'][1]['data']['target']['commit'],'a'*40)
        self.assertEqual(doc['delta']['tasks']['value'],'unknown')
        self.assertIn(receipts.digest((self.shift/'report.json').read_bytes()),(self.shift/'report.md').read_text())

    def test_older_shift_with_version_one_receipts_is_still_summarized(self):
        """Live validation refuses format 1; a report must still read what an old shift kept."""
        self.task['fallback']=None  # field written by the format-1 lifecycle
        self.task['receipts'].clear(); self.entries.clear()
        for phase in ('build','review'):
            path='receipts/'+phase+'.json'; value=decode((self.shift/path).read_bytes()); value['version']=1
            if phase=='review':
                value['data']['implementer_family']='GPT'
                value['data']['invocations'][0].update(model_family='Gemini',model='legacy model')
            raw=receipts.encoded(value);(self.shift/path).write_bytes(raw)
            ref=dict(path=path,sha256=receipts.digest(raw),stage=phase,round=0,status='satisfied')
            self.task['receipts'].append(ref)
            slot=receipts.digest(receipts.encoded([value[k] for k in ('shift_id','task_id','request_id','role','phase','round')]))
            self.entries.append(dict(receipt_id=phase,slot=slot,sha256=ref['sha256'],satisfies_phase=True))
        self.task['accepted']=dict(candidate=self.task['candidate'],receipt=ref)
        self.store()
        # An old shift is audited by the offline report, never by a newer Driver.
        self.generate()
        doc=decode((self.shift/'report.json').read_bytes())
        self.assertEqual(doc['status'],'evidence summarized')
        self.assertIn('retained review matches candidate',doc['tasks'][0]['acceptance'])
        self.assertEqual(doc['tasks'][0]['receipts'][1]['data']['implementer_family'],'GPT')
        with self.assertRaisesRegex(Refused,'unsupported format version'):
            validate_shape(value,'receipt')
        validate_shape(value,'receipt',versions=(1,2))

    def test_pane_era_delivery_rows_are_kept_and_an_unended_turn_is_an_issue(self):
        self.task['delivery']=[dict(send_id='s1',code=0,detail='confirmed')]
        for name in ('nudged','ambiguous'): self.task[name]=False  # fields the pane lifecycle wrote
        self.store()
        doc=self.generate()
        self.assertEqual(doc['tasks'][0]['delivery'],[dict(send_id='s1',code=0,detail='confirmed')])
        self.assertEqual((doc['tasks'][0]['turns'],doc['status']),([],'evidence summarized'))
        del self.task['delivery']
        self.state['turn']=dict(task_id='task',turn=1,started=self.grant['start'],seconds=5,log='logs/x.log')
        self.store()
        doc=self.generate()
        self.assertEqual(doc['status'],'partial')
        self.assertNotIn('delivery',doc['tasks'][0])
        self.assertTrue(any('no recorded end' in i['reason'] for i in doc['issues']))

    def test_reason_closeouts_and_signed_deadline(self):
        for reason in ('expiry','foreground host interrupted','source changed','operator withdrawn'):
            with self.subTest(reason=reason),self.no_external():
                self.driver.finish(reason)
                self.assertEqual(decode((self.shift/'report.json').read_bytes())['actual_window']['reason'],reason)
        before=(self.shift/'report.json').read_bytes();self.clock.sleep(111)
        with self.no_external():self.driver.finish('too late')
        self.assertEqual((self.shift/'report.json').read_bytes(),before)

    def test_the_preparation_journal_is_not_presented_as_coverage_of_the_shift(self):
        """An older shift holds an events.jsonl with one row written at preparation;
        new ones have none. The report says nothing about it, whatever it contains,
        and does not read it."""
        for raw in (b'{}\n{broken\n\xff\n[]\n', None):
            if raw is None: (self.shift/'events.jsonl').unlink()
            else: (self.shift/'events.jsonl').write_bytes(raw)
            self.artifact('journal-receipts/events.jsonl.pending','{}')
            doc=self.generate()
            self.assertEqual(doc['status'],'evidence summarized')
            self.assertNotIn('journal',doc); self.assertNotIn('journal_rows',doc['metrics'])
            self.assertFalse([i for i in doc['inputs'] if 'events.jsonl' in i['path']])
            self.assertNotIn('ournal',(self.shift/'report.md').read_text())
        self.assertEqual(doc['schema'],'sortie-kit.report.v2')

    def text(self):
        self.generate(); return (self.shift/'report.md').read_text()

    def test_markdown_reads_as_prose_for_an_accepted_task(self):
        self.task['turns']=[dict(task_id='task',turn=1,started=self.grant['start'],ended=self.grant['start'],
                                 limit_seconds=60,log='logs/builder-turn-task-1.log',outcome='exit',exit=0,reaped=True,
                                 output_dropped_bytes=0,elapsed_seconds=75,receipts_changed=True)]
        self.store(); self.driver.cleaned=True
        self.driver.finish('selected work complete'); text=(self.shift/'report.md').read_text()
        for expected in ('# Shift report: current','- How it ended: selected work complete',
                         '> selected work','### task (defect): accepted locally','- Result: the driver recorded acceptance of commit '+'a'*40,
                         'review of the candidate, round 0: verdict ACCEPT on commit '+'a'*40,
                         'from agy-review with model example-review-model; no findings',
                         '1 check reported, 1 with the expected exit status','python -m unittest: exit 0, expected 0',
                         'Turn 1: exit with status 0, after 1m 15s; the receipts folder changed. Log: logs/builder-turn-task-1.log',
                         'no stop receipt from the builder','- Tasks: 1 accepted locally of 1 briefed (task at aaaaaaa); whether the direction held more is not known to the host.','- Shutdown: confirmed.','- Next: developer.',
                         'No briefed task is recorded as left undone. The host counts only the tasks the driver briefed; it does not '
                         'know whether the direction held more.','None found.',
                         '- Review choice in the grant: agy-review with model example-review-model.'):
            self.assertIn(expected,text)
        for raw in ('<pre>','&quot;','{"'):
            self.assertNotIn(raw,text)
        self.assertEqual(launcher.report_state(self.shift),'final')

    def test_the_tasks_line_and_not_done_say_the_count_is_of_briefed_tasks_and_what_the_driver_named(self):
        """On a real shift, the direction held two tasks, one was briefed and accepted, and the
        report read as if all of it was done."""
        self.store(); self.driver.cleaned=True
        named=[dict(name='second task',reason='not enough time before wind-down.'),dict(name='# third <b>',reason='*y*\n<i> [l](http://e)')]
        for kept,line,undone,absent in (
                (named,'- Tasks: 1 accepted locally of 1 briefed (task at aaaaaaa); the driver names 2 more as not started.',
                 ['- Not started, as the driver stated it: second task: not enough time before wind-down.',
                  '- Not started, as the driver stated it: \\# third &lt;b&gt;: \\*y\\* &lt;i&gt; \\[l\\](http://e).'],'left undone'),
                ([],'- Tasks: 1 accepted locally of 1 briefed (task at aaaaaaa); the driver says the direction held no more.',
                 ['No briefed task is recorded as left undone. The driver stated that nothing in the direction was left '
                  'unstarted.'],'Not started'),
                (None,'- Tasks: 1 accepted locally of 1 briefed (task at aaaaaaa); whether the direction held more is not known to the host.',
                 ['No briefed task is recorded as left undone. The host counts only the tasks the driver briefed; it does '
                  'not know whether the direction held more.'],'Not started'),
                ('bad',None,None,None),([dict(name='x')],None,None,None),([dict(name='x',reason='y')]*33,None,None,None)):
            with self.subTest(kept=kept):
                self.state['not_started']=kept; self.store()
                doc=self.generate(); text=(self.shift/'report.md').read_text()
                if line is None:
                    # A list of another shape is not the driver's statement: the report reads as if none was given.
                    self.assertIsNone(doc['not_started'])
                    self.assertEqual(doc['status'],'partial')
                    self.assertIn(dict(path='driver-state.json',
                                       reason="the driver's list of what was not started is malformed; not shown"),doc['issues'])
                    self.assertIn('whether the direction held more is not known to the host.\n',text)
                    continue
                self.assertEqual((doc['not_started'],doc['status'],doc['issues']),(kept,'evidence summarized',[]))
                self.assertIn(line+'\n',text)
                section=text.split('## Not done')[1].split('\n## ')[0]
                for expected in undone:
                    self.assertIn(expected+'\n',section)
                self.assertNotIn(absent,section)
                self.assertNotIn('Nothing is recorded as left undone',text)
        # A shift kept by a host from before the list existed: read as no list given, and not as damage.
        del self.state['not_started']; self.store()
        doc=self.generate()
        self.assertEqual((doc['not_started'],doc['status'],doc['issues']),(None,'evidence summarized',[]))
        self.assertIn('whether the direction held more is not known to the host.\n',(self.shift/'report.md').read_text())
        self.assertEqual(report.tasks_words(dict(tasks=[],not_started=named[:1])),
                         'none briefed; the driver names 1 as not started.')
        self.assertEqual(report.tasks_words(dict(tasks=[],not_started=[])),
                         'none briefed; the driver says the direction held nothing to brief.')
        self.assertEqual(report.tasks_words(dict(tasks=[])),
                         'none briefed; whether the direction held more is not known to the host.')

    def test_not_done_lists_an_unaccepted_task_beside_what_the_driver_named_as_not_started(self):
        self.task.update(stage='ack',candidate=None,accepted=None,receipts=[],reason='')
        self.state['not_started']=[dict(name='second task',reason='no time')]
        self.entries.clear(); self.store()
        self.driver.finish('out of time'); text=(self.shift/'report.md').read_text()
        section=text.split('## Not done')[1].split('\n## ')[0]
        self.assertIn('- Task task: not accepted.',section)
        self.assertIn('- Not started, as the driver stated it: second task: no time.\n',section)
        self.assertNotIn('left undone',section)
        self.assertIn('- Tasks: 0 accepted locally of 1 briefed; the driver names 1 more as not started.\n',text)

    def test_markdown_says_what_was_not_done_and_why(self):
        self.task.update(stage='ack',candidate=None,accepted=None,receipts=[],reason='')
        self.task['turns']=[dict(task_id='task',turn=1,started=self.grant['start'],ended=self.grant['start'],
                                 limit_seconds=60,log='logs/x.log',outcome='exit',exit=0,reaped=True,
                                 output_dropped_bytes=0,elapsed_seconds=30,receipts_changed=False)]
        self.state['commands']['c1']=dict(digest='0'*64,response=dict(id='c1',status='blocked',detail='no receipt at that path'))
        self.entries.clear(); self.store()
        self.driver.finish('Blocker: the builder could not write'); text=(self.shift/'report.md').read_text()
        for expected in ('- How it ended: Blocker: the builder could not write','### task (defect): not accepted',
                         "It stopped at: waiting for the builder's acknowledgment.",
                         '- The builder handed in nothing the driver consumed.',
                         'the receipts folder did not change','- Tasks: 0 accepted locally of 1 briefed; whether the direction held more is not known to the host.',
                         "- Task task: not accepted. It stopped at: waiting for the builder's acknowledgment. While the shift ran, the task was recorded as waiting on: builder. The shift is over; who has it now is the \"Next\" line under Outcome.",
                         '- Next: developer.',
                         '1 command result is recorded; 1 of them blocked or invalid:','- c1: blocked: no receipt at that path'):
            self.assertIn(expected,text)

    def test_retained_text_cannot_add_markdown_html_or_control_characters(self):
        hostile='# H\n\n![x](http://e/i.png) [l](http://e) <img src=x> `c` *b* _i_ a_b \x1b[31m\u202e&amp;'
        self.task['reason']=hostile; self.task['id']='task'; self.store()
        self.driver.finish(hostile); text=(self.shift/'report.md').read_text()
        line=next(l for l in text.splitlines() if l.startswith('- How it ended: '))
        self.assertEqual(line,'- How it ended: \\# H !\\[x\\](http://e/i.png) \\[l\\](http://e) &lt;img src=x&gt; '
                              '\\`c\\` \\*b\\* \\_i\\_ a_b \\[31m &amp;amp;')
        self.assertNotIn('\x1b',text); self.assertNotIn('\u202e',text); self.assertNotIn('<img',text)
        self.assertFalse([l for l in text.splitlines() if l.startswith('# H')])
        self.assertEqual(report.plain('x'*700),'x'*600+' (shortened; report.json has the rest)')
        # Text placed after a list marker or in a quote must not open a block of its own.
        for raw,inert in (('# h','\\# h'),('> q','\\&gt; q'),('- i','\\- i'),('+ i','\\+ i'),('~~~','\\~\\~\\~'),
                          ('=== x','\\=== x'),('1. i','1\\. i'),('12) i','12\\) i'),('| a | b |','\\| a \\| b \\|'),('a | b','a \\| b'),(':-- | --:',':-- \\| --:'),
                          ('x ~~gone~~ y','x \\~\\~gone\\~\\~ y'),
                          ('  # h','\\# h'),('1.5 x','1.5 x'),('a # b - c','a # b - c')):
            self.assertEqual(report.plain(raw),inert)
        self.artifact('direction.md','# heading\n- item\n~~~\n1. one\nplain line\nh1 | h2\n:-- | --:\n')
        self.state['direction_sha256']=receipts.digest((self.shift/'direction.md').read_bytes())
        text=self.text()
        for line in ('> \\# heading','> \\- item','> \\~\\~\\~','> 1\\. one','> plain line','> h1 \\| h2','> :-- \\| --:'):
            self.assertIn(line+'\n',text)
        self.assertEqual(report.plain({'a':'<b>'}),'{"a": "&lt;b&gt;"}')

    def test_markdown_keeps_recorded_acceptance_apart_from_backed_acceptance_and_from_none(self):
        (self.shift/'receipts/review.json').unlink()
        text=self.text()
        self.assertIn('### task (defect): acceptance recorded, not backed by the kept evidence',text)
        self.assertIn('- Result: the driver recorded acceptance of commit '+'a'*40+', but the review kept in the shift '
                      'does not back it',text)
        self.assertIn('review of the candidate, round 0: details unavailable',text)
        self.assertNotIn('no findings',text)
        self.assertIn('- Tasks: 0 accepted locally of 1 briefed; 1 more recorded as accepted without backing evidence; whether '
                      'the direction held more is not known to the host.',text)
        (self.shift/'receipts/build.json').write_text('{')
        text=self.text()
        self.assertIn('build, round 0: details unavailable',text); self.assertNotIn('no checks reported',text)
        self.assertIn('- Status: partial.',text)

    def test_markdown_states_signed_commands_and_observations_separately(self):
        from sortie_kit import agents
        seats={}
        for role,name in (('driver','claude'),('builder','codex')):
            seats[role]=agents.seat(role,agents.load(name,self.root,home=self.root/'no-home'),'m','f',
                                    {'path':'/opt/tools/'+name,'sha256':'0'*64,'bytes':1})
        self.grant.update(seats=seats,repo='/signed/repo'); raw=receipts.encoded(self.grant)
        (self.shift/'grant.json').write_bytes(raw); self.state['grant_sha256']=receipts.digest(raw); self.store()
        text=self.text()
        self.assertIn('## What the kept grant says about the seats',text)
        self.assertIn('  - Command in the grant: /opt/tools/codex exec --sandbox workspace-write --add-dir .git '
                      '--model m --cd /signed/repo PROMPT',text)
        self.assertIn('  - no network option is passed: unless your own codex configuration turns it on, the builder '
                      'cannot use the network in the commands it runs (codex itself still reaches its model). That is '
                      'how the CLI runs this sandbox; sortie-kit does not enforce it\n',text)
        self.assertNotIn('can use the network',text)
        # A grant signed with the network on, by this version's flag or by an earlier version's one form.
        seats['builder']=agents.seat('builder',agents.load('codex',self.root,home=self.root/'no-home'),'m','f',
                                     {'path':'/opt/tools/codex','sha256':'0'*64,'bytes':1},network=True)
        self.grant.update(seats=seats); raw=receipts.encoded(self.grant)
        (self.shift/'grant.json').write_bytes(raw); self.state['grant_sha256']=receipts.digest(raw); self.store()
        wide=self.text()
        self.assertIn('  - Command in the grant: /opt/tools/codex exec --sandbox workspace-write --add-dir .git -c '
                      'sandbox_workspace_write.network_access=true --model m --cd /signed/repo PROMPT',wide)
        self.assertIn('  - \\-c sandbox_workspace_write.network_access=true: the builder can use the network in the '
                      'commands it runs. That is what the option asks of the CLI; sortie-kit has not checked it\n',wide)
        self.assertNotIn('cannot use the network',wide)
        self.assertNotIn(str(self.repo_path),text.split('## What the kept grant says about the seats')[1])
        self.assertIn('The host recorded no closeout, so this report has no observation of either seat being started',text)
        self.assertNotIn('nothing was pushed',text)
        self.assertIn('- Status: no problem was found with the evidence that was read.',text)
        self.assertIn("did not check the grant's signature again",text)
        for word in ('Signed command','Signed window','Signed review','was signed'):
            self.assertNotIn(word,text)

    def test_a_kept_grant_without_a_signature_file_is_never_called_signed(self):
        # The signer writes grant.json before it asks for the passphrase; a failed signing leaves it behind.
        self.assertEqual(self.generate()['grant']['provenance'],'retained grant; signature not reverified')
        self.assertIn('- Window in the kept grant: ',self.text())
        self.assertNotIn('No signature file',self.text())
        (self.shift/'grant.json.sig').unlink()
        doc=self.generate(); text=(self.shift/'report.md').read_text()
        self.assertEqual(doc['grant']['provenance'],'retained grant without a signature file; not shown to be signed')
        self.assertIn('- **No signature file is kept beside grant.json**: nothing here shows this grant was ever signed.',text)
        self.assertNotIn('igned window',text)

    def test_direction_is_repeated_bounded_and_checked_against_the_grant(self):
        doc=self.generate()
        self.assertEqual(doc['direction'],dict(sha256=self.grant['direction_sha256'],text='selected work',
                                               shortened=False,matches_grant=True))
        self.artifact('direction.md','# not a heading\n'+'y'*5000)
        doc=self.generate(); text=(self.shift/'report.md').read_text()
        self.assertEqual((doc['direction']['shortened'],doc['direction']['matches_grant'],len(doc['direction']['text'])),
                         (True,False,2000))
        self.assertIn('**which does not match the kept grant**',text)
        self.assertIn('> \\# not a heading',text); self.assertIn('Shortened here',text)
        (self.shift/'direction.md').unlink()
        self.generate(); self.assertIn('The direction file could not be read.',(self.shift/'report.md').read_text())

    def test_a_receipt_the_driver_returned_is_not_a_problem_for_being_absent_from_the_ledger(self):
        ref=self.task['receipts'][1]; ref['status']='blocked'; self.entries.pop()
        self.task.update(accepted=None,stage='repair_needed'); self.store()
        text=self.text()
        self.assertNotIn('no matching entry in the consumed ledger',text)
        self.assertIn('The driver returned this receipt (blocked): it did not satisfy its phase and did not advance the task.',text)
        ref['status']='satisfied'; self.store()
        self.assertIn('**Problem: no matching entry in the consumed ledger.**',self.text())

    def returned_review(self, verdict='CHANGES', findings=('open',), status='blocked', exit_status=0):
        """The fixture's review receipt, rewritten as one the host returned, with a command result for it."""
        ref=self.task['receipts'][1]; path=self.shift/ref['path']; value=decode(path.read_bytes())
        value['data'].update(verdict=verdict,findings=[dict(id='F'+str(n+1),disposition=d,reason='Say what happens below 1.')
                                                       for n,d in enumerate(findings)])
        value['data']['invocations'][0]['exit_status']=exit_status
        raw=receipts.encoded(value); path.write_bytes(raw)
        ref.update(sha256=receipts.digest(raw),status=status); self.entries.pop()
        self.task.update(accepted=None,stage='repair_needed')
        return ref

    def test_a_review_that_asked_for_changes_reads_as_a_repair_round_and_not_as_a_refused_command(self):
        """Two routine plan repairs read as `2 of them blocked or invalid`."""
        self.returned_review()
        said='receipt did not satisfy phase: blocked; review requests changes; open review findings'
        self.state['commands'].update(
            c1=dict(digest='0'*64,response=dict(id='c1',status='blocked',detail=said,state='repair_needed')),
            c2=dict(digest='0'*64,response=dict(id='c2',status='advanced',detail='bound receipt recorded; review',state='review')))
        self.store(); text=self.text()
        self.assertIn('    - This review asked for changes (verdict CHANGES, 1 finding still open), so it did not advance '
                      'the task. That is what a repair round is for; it is not a refused command. No later round with a '
                      'review of the candidate that satisfied the phase is recorded.',text)
        self.assertNotIn('The driver returned this receipt',text)
        self.assertNotIn('no matching entry in the consumed ledger',text)
        commands=text.split('## Driver commands',1)[1].split('## ',1)[0]
        self.assertIn('2 command results are recorded. 1 of them is a review that asked for changes, which the host '
                      'answers `blocked` so that a repair can follow; see the task:\n- c1: '+said.replace('; ','; ')
                      +'\n\nOf the other 1, none was blocked or invalid.',commands)
        self.assertNotIn('of them blocked or invalid',commands)
        # A later round whose review satisfied the phase, intact and in the ledger, is named.
        later=self.add_receipt('review'); later['round']=1
        self.assertNotIn('Round 1 followed',self.text())  # the fixture file is of round 0: not corroborated
        self.task['receipts'].pop(); self.entries.pop(); self.store()
        # An ACCEPT with a finding left open is said as that.
        self.returned_review(verdict='ACCEPT',findings=('open','rejected')); self.entries.append({}); self.store()
        self.assertIn('This review asked for changes (verdict ACCEPT, 1 finding still open), so it did not advance',self.text())

    def test_a_later_round_is_named_only_when_its_receipt_satisfied_the_phase_and_is_backed(self):
        first=self.returned_review()
        first.update(path='receipts/review-r0.json'); (self.shift/'receipts/review.json').rename(self.shift/first['path'])
        value=decode(kept_v2('review-v2.json'))
        value.update(receipt_id='review-r1',shift_id='current',task_id='task',request_id='review-r1',role='builder',round=1,brief=self.brief)
        data=value['data']; data['target']=self.task['candidate']; i=data['invocations'][0]; i['target']=self.task['candidate']
        i['argv'][3]='a'*40; i['raw_output']=self.artifact('logs/review.txt','ACCEPT')
        raw=receipts.encoded(value); (self.shift/'receipts/review-r1.json').write_bytes(raw)
        ref=dict(path='receipts/review-r1.json',sha256=receipts.digest(raw),stage='review',round=1,status='satisfied')
        self.task['receipts'].append(ref); self.task.update(round=1,stage='closed',accepted=dict(candidate=self.task['candidate'],receipt=ref))
        slot=receipts.digest(receipts.encoded([value[k] for k in ('shift_id','task_id','request_id','role','phase','round')]))
        entry=dict(receipt_id='review-r1',slot=slot,sha256=ref['sha256'],satisfies_phase=True)
        # The fixture receipt at round 0 names receipt_id `review`; keep the file's identity as it was written.
        self.store(); text=self.text()
        self.assertIn('No later round with a review of the candidate that satisfied the phase is recorded.',text)
        self.entries.append(entry); self.store(); text=self.text()
        self.assertIn('it is not a refused command. Round 1 followed, and its review of the candidate satisfied the phase.',text)

    def test_a_returned_receipt_that_is_not_a_review_asking_for_changes_is_not_called_a_repair_round(self):
        """Plan review M1: a duplicate, a failed review run, and a stop receipt are not routine repairs."""
        old='The driver returned this receipt ('
        for kw,status in ((dict(status='duplicate'),'duplicate'),(dict(exit_status=2),'blocked'),
                          (dict(verdict='ACCEPT',findings=()),'blocked')):
            self.setUp(); self.returned_review(**kw)
            detail='receipt did not satisfy phase: '+status+'; review invocation failed or incomplete; review requests changes'
            self.state['commands']['c1']=dict(digest='0'*64,response=dict(id='c1',status='blocked',detail=detail,state='repair_needed'))
            self.store(); text=self.text()
            self.assertIn(old+status+'): it did not satisfy its phase and did not advance the task.',text,kw)
            self.assertNotIn('repair round',text,kw)
            self.assertIn('1 command result is recorded; 1 of them blocked or invalid:',text,kw)
        # Change review M1: a fix the reviewer did not reproduce is the validator's own block, not a routine round.
        self.setUp(); self.returned_review(findings=('open','fixed')); self.store()
        self.assertIn(old+'blocked): it did not satisfy',self.text())
        ref=self.task['receipts'][1]; path=self.shift/ref['path']; value=decode(path.read_bytes())
        run=dict(outcome='matched',exit_status=0,expected_exit=0)
        value['data']['reproductions']=[dict(finding_id='F2',candidate=run,baseline=dict(kind='run',run=dict(run,exit_status=1)),
                                             assessment=dict(status='reproduced'))]
        def again():
            raw=receipts.encoded(value); path.write_bytes(raw); ref['sha256']=receipts.digest(raw); self.store(); return self.text()
        with patch.object(report,'validate_shape',lambda *a,**k:None):
            self.assertIn(old+'blocked)',again())
            value['data']['reproductions'][0]['baseline']['run']['exit_status']=0
            self.assertIn('This review asked for changes (verdict CHANGES, 1 finding still open)',again())
            value['data']['reproductions'][0]['assessment']['status']='not reproduced'
            self.assertIn(old+'blocked)',again())
        # The same words on a command that left no repair to make are a refusal too.
        self.state['commands']['c1']=dict(digest='0'*64,response=dict(
            id='c1',status='blocked',detail='receipt did not satisfy phase: blocked; open review findings',state='review'))
        self.store(); self.assertIn('1 of them blocked or invalid:',self.text())

    def wake_line(self,entry):
        self.driver.wake=lambda:entry
        with self.no_external(): self.driver.finish('done')
        lines=[l for l in (self.shift/'report.md').read_text().splitlines() if 'Kept awake' in l]
        self.assertEqual(len(lines),1,lines)
        return lines[0]

    def test_the_outcome_says_whether_the_machine_was_kept_awake_for_the_whole_shift(self):
        at='2030-01-01T00:00:00+00:00'
        base=dict(asked=True,means='a macOS power assertion',held=True,held_from=at,seen=at,whole=True,
                  refusals=[],refusals_count=0,gaps=[],gaps_count=0)
        self.assertEqual(self.wake_line(base),'- Kept awake: for the whole shift, by a macOS power assertion.')
        refused=dict(base,whole=False,held=False,held_from=None,refusals_count=9,
                     refusals=[dict(at=at,what='create',code=-536870209),dict(at=at,what='create: OSError: no IOKit',code=None)])
        self.assertEqual(self.wake_line(refused),
                         '- **Kept awake: NOT for the whole shift**, by a macOS power assertion. It was never held. '
                         'Refused 9 times: at '+at+', create returned -536870209; at '+at+', create: OSError: no IOKit; '
                         'and 7 more. It was not held when this was written. While it was not held the machine could '
                         'idle-sleep, and the signed window ran on.')
        lapsed=dict(base,whole=False,gaps_count=1,gaps=[{'from':at,'to':'2030-01-01T02:00:00+00:00'}])
        self.assertIn('Held from '+at+'. Lapsed 1 time, when the host did not renew it in time (it was held up, or the '
                      'machine slept anyway): from '+at+' to 2030-01-01T02:00:00+00:00.',self.wake_line(lapsed))
        unknown=dict(base,whole=False,held=None,refusals_count=1,
                     refusals=[dict(at=at,what='whether it is held could not be asked: OSError: no answer',code=None)])
        line=self.wake_line(unknown)
        self.assertIn('. Whether it was held when this was written is not known. While it was not held the machine',line)
        self.assertNotIn('It was not held when',line)
        off=dict(base,asked=False,means='off: --no-wake was given',held=False,held_from=None,whole=False)
        self.assertEqual(self.wake_line(off),'- Kept awake: no (off: --no-wake was given). The machine could idle-sleep, '
                                             'and the signed window ran on.')
        none=dict(off,asked=True,means='none: systemd-inhibit is not installed')
        self.assertEqual(self.wake_line(none),'- **Kept awake: no** (none: systemd-inhibit is not installed). The machine '
                                              'could idle-sleep, and the signed window ran on.')

    def test_a_wake_entry_of_another_shape_or_with_hostile_text_stays_inert_and_costs_no_report(self):
        for entry in ('yes',['x'],dict(whole=True),dict(asked=True,means=5,whole='yes'),
                      dict(asked=True,means='m',whole=False,held=True,refusals='x',gaps=[3],refusals_count='many')):
            with self.subTest(entry=entry):
                line=self.wake_line(entry)
                self.assertNotIn('for the whole shift, by',line)
        line=self.wake_line(dict(asked=True,means='<script>x</script>\n## Heading',whole=True,held=True,refusals=[],gaps=[]))
        self.assertNotIn('<script>',line)
        self.assertIn('## Outcome',(self.shift/'report.md').read_text())

    def test_stamps_further_apart_than_the_time_the_host_counted_are_said_as_sleep_or_a_changed_clock(self):
        self.clock.sleep(12)
        with self.no_external(): self.driver.finish('done')
        self.assertIn('- Ran for 12s, from 2030-01-01T00:00:00+00:00 to 2030-01-01T00:00:12+00:00.\n',
                      (self.shift/'report.md').read_text())
        closed=decode((self.shift/'closeout.json').read_bytes())
        closed['end']='2030-01-01T02:00:12+00:00'  # the monotonic clock stood still while the machine slept
        (self.shift/'closeout.json').write_bytes(receipts.encoded(closed))
        self.generate()
        self.assertIn('- Ran for 12s, from 2030-01-01T00:00:00+00:00 to 2030-01-01T02:00:12+00:00. **Those two times are '
                      '2h 12s apart: for the other 2h the machine was asleep or its clock was changed.**\n',
                      (self.shift/'report.md').read_text())

    def test_the_limits_say_that_a_log_holds_only_what_the_seats_cli_printed(self):
        self.generate()
        limits=(self.shift/'report.md').read_text().split('## Limits')[1]
        self.assertIn("- A seat's log holds what that seat's CLI printed, which for some CLIs is only its final message. "
                      'sortie-kit does not record what a seat ran.',limits)

    def test_a_shift_without_a_reviewer_seat_has_no_list_of_open_findings(self):
        doc=self.generate()
        self.assertIsNone(doc['open_findings'])
        self.assertNotIn('Findings left open',(self.shift/'report.md').read_text())

    def test_a_builder_written_review_backs_nothing_in_a_shift_whose_grant_has_a_reviewer_seat(self):
        """In such a shift the host runs every review: an earlier version's review receipt, which the
        builder wrote, is never reported as the host's ACCEPT."""
        policy=dict(self.grant['review_policy'],review_choice='given'); del policy['review_tool']
        self.grant.update(review_policy=policy,review_limit_seconds=600,
                          seats=dict(self.grant['seats'],reviewer=dict(agent='fixture',runtime='codex')))
        raw=receipts.encoded(self.grant); (self.shift/'grant.json').write_bytes(raw)
        self.state['grant_sha256']=receipts.digest(raw); self.store()
        doc=self.generate()
        self.assertEqual(doc['tasks'][0]['acceptance'],report.UNBACKED)
        text=(self.shift/'report.md').read_text()
        self.assertNotIn("The host's review of",text)
        self.assertIn('not backed by the kept evidence',text)

    def headless(self,turns,running=None):
        self.grant['seats']['builder']['mode']='headless'
        raw=receipts.encoded(self.grant); (self.shift/'grant.json').write_bytes(raw)
        self.admission.raw=raw; self.driver=Driver(self.admission,self.spawn,seconds=100,wind_down=10)
        self.state['grant_sha256']=receipts.digest(raw)
        row=lambda n,outcome,reaped:dict(task_id='task',turn=n,started=self.grant['start'],ended=self.grant['start'],
                                       limit_seconds=60,log='logs/t'+str(n)+'.log',outcome=outcome,exit=0,reaped=reaped,
                                       output_dropped_bytes=0,elapsed_seconds=5,receipts_changed=True)
        self.task['turns']=[row(n+1,*turn) for n,turn in enumerate(turns)]
        self.state['turn']=running and dict(task_id='task',turn=len(turns)+1,started=self.grant['start'],limit_seconds=60,log='logs/r.log')
        self.store()

    def test_a_headless_builder_whose_turns_were_all_seen_to_end_is_not_reported_as_unknown(self):
        """The host started every turn and saw each end, and the report said `not known`."""
        self.headless([('exit',True),('exit',True),('timeout',True)]); text=self.text()
        self.assertIn('- Builder stop: the builder ran headless. The host started each of its 3 turns for this task and '
                      'saw each process end (turns seen to end: 3 of 3).',text)
        self.assertNotIn('whether its seat stopped is not known',text)
        self.assertNotIn('turn_running',decode((self.shift/'report.json').read_bytes())['tasks'][0])
        # A turn that never started is no process; one not seen to end keeps the unknown.
        self.headless([('exit',True),('not started: no such file',False)]); text=self.text()
        self.assertIn('The host started its one turn for this task and saw that process end (turns seen to end: 1 of 1).',text)
        self.headless([('exit',True),('lost',False)]); text=self.text()
        self.assertIn('- Builder stop: the builder ran headless. Turns seen to end: 1 of 2; whether the other stopped is not known.',text)
        self.headless([('exit',True)],running=True); doc=self.generate(); text=(self.shift/'report.md').read_text()
        self.assertIs(doc['tasks'][0]['turn_running'],True)
        self.assertIn('Turns seen to end: 1 of 2; whether the other stopped is not known.',text)
        self.headless([],running=True)
        self.assertIn('Turns seen to end: 0 of 1; whether the other stopped is not known.',self.text())
        self.headless([])
        self.assertIn('no stop receipt from the builder, so whether its seat stopped is not known.',self.text())

    def test_a_builder_that_is_not_headless_keeps_the_unknown(self):
        self.task['turns']=[dict(task_id='task',turn=1,started=self.grant['start'],ended=self.grant['start'],limit_seconds=60,
                                 log='logs/t.log',outcome='exit',exit=0,reaped=True,output_dropped_bytes=0,
                                 elapsed_seconds=5,receipts_changed=True)]
        self.store()
        self.assertIn('no stop receipt from the builder, so whether its seat stopped is not known.',self.text())

    def test_whether_a_commit_holds_what_was_uncommitted_at_the_start_is_said_from_the_look(self):
        """`A commit made during the shift may hold them`, and neither commit did."""
        old=[dict(path='.gitignore',state='untracked'),dict(path='tests/test_x.py',state='untracked')]
        lead='(.gitignore, tests/test_x.py). '
        between='between the HEAD at the start and the HEAD at the end'
        def said(held,start=None):
            self.driver.repo=dict(start=start or dict(head='a'*40,changes=old,truncated=False),
                                  end=dict(head='b'*40,changes=[],truncated=False))
            if held is not None: self.driver.repo['held']=held
            with self.no_external(): self.driver.finish('done')
            line=next(l for l in (self.shift/'report.md').read_text().splitlines() if 'was not clean' in l)
            return line.split(lead,1)[1] if lead in line else line
        self.assertEqual(said(dict(paths=[],complete=True)),'No commit '+between+' holds any of them.')
        self.assertEqual(said(dict(paths=['.gitignore','tests/test_x.py'],complete=True)),'Commits '+between+' hold both of them.')
        self.assertEqual(said(dict(paths=['tests/test_x.py','elsewhere.txt'],complete=True)),
                         'Commits '+between+' hold 1 of them (tests/test_x.py) and not the other.')
        self.assertEqual(said(dict(paths=['tests/test_x.py'],complete=False)),
                         'Commits '+between+' hold 1 of them (tests/test_x.py); whether they hold the other is not known: the look was not finished.')
        self.assertEqual(said(dict(paths=[],complete=False)),
                         'A commit made during the shift may hold them: the look for that was not finished.')
        self.assertEqual(said(dict(error='git log did not answer')),
                         'A commit made during the shift may hold them: the look for that failed (git log did not answer).')
        self.assertEqual(said(None),'A commit made during the shift may hold them.')
        for wrong in (dict(paths='x',complete=True),dict(paths=[1],complete=True),dict(paths=[],complete='yes'),[],'held'):
            self.assertEqual(said(wrong),'A commit made during the shift may hold them: the look for that was not finished.',wrong)
        # A cut look at the start is never the whole picture, whatever the record says.
        cut=dict(head='a'*40,changes=old,truncated=True)
        self.assertEqual(said(dict(paths=[],complete=True),cut),
                         'A commit made during the shift may hold them: the look for that was not finished.')
        one=dict(head='a'*40,changes=old[:1],truncated=False)
        self.assertTrue(said(dict(paths=[],complete=True),one).endswith('(.gitignore). No commit '+between+' holds it.'))
        self.assertTrue(said(dict(paths=['.gitignore'],complete=True),one).endswith('(.gitignore). A commit '+between+' holds it.'))

    def test_not_done_says_where_the_task_stopped_and_only_then_any_recorded_reason(self):
        self.task.update(stage='build',accepted=None,reason='Fix F1 in the plan',round=2); self.store()
        doc=self.generate(); undone=(self.shift/'report.md').read_text().split('## Not done',1)[1].split('## ',1)[0]
        self.assertEqual(doc['blockers'][0]['stage'],'build')
        self.assertIn('Task task: not accepted. It stopped at: waiting for the build. Last recorded reason: Fix F1 in the plan.',undone)
        self.task.update(reason=''); self.store()
        undone=self.text().split('## Not done',1)[1].split('## ',1)[0]
        self.assertIn('Task task: not accepted. It stopped at: waiting for the build. While the shift ran, the task was recorded as waiting on',undone)

    def test_the_direction_is_named_as_the_copy_kept_in_the_shift(self):
        text=self.text()
        self.assertIn("The direction, from the shift's own copy (direction.md in the shift folder, made from the file "
                      'given at the start; SHA256 ',text)

    def test_what_the_repository_looked_like_is_reported_from_the_closeout(self):
        old=dict(path='old.txt',state='untracked')
        left=[dict(path='tests/test_x.py',state='untracked'),dict(path='src/a b.py',state='changed')]
        self.driver.repo=dict(start=dict(head='a'*40,changes=[old],truncated=False),
                              end=dict(head='b'*40,changes=[old]+left,truncated=False))
        with self.no_external(): self.driver.finish('done')
        doc=decode((self.shift/'report.json').read_bytes()); text=(self.shift/'report.md').read_text()
        self.assertEqual(doc['leftovers'],dict(new=left,before=1,known=True,since_start=True,complete=True))
        undone=text.split('## Not done',1)[1].split('## ',1)[0]
        for part in ('Left uncommitted in the repository','  - tests/test_x.py (untracked)','src/a b.py (changed)',
                     '1 more was already there when the shift began'):
            self.assertIn(part,undone)
        self.assertIn('- Repository HEAD: '+'a'*40+' when the shift began, '+'b'*40+' when it ended.',text)
        self.driver.repo=dict(start=dict(head='a'*40,changes=[],truncated=False),end=dict(error='git did not answer'))
        with self.no_external(): self.driver.finish('done')
        doc=decode((self.shift/'report.json').read_bytes()); text=(self.shift/'report.md').read_text()
        self.assertEqual(doc['leftovers'],dict(new=[],before=0,known=False,since_start=True,complete=False))
        self.assertIn('could not be read when the shift ended',text.split('## Not done',1)[1].split('## ',1)[0])
        self.driver.repo=dict(start=dict(head='a'*40,changes=[],truncated=False),
                              end=dict(head='a'*40,changes=[],truncated=False))
        with self.no_external(): self.driver.finish('done')
        text=(self.shift/'report.md').read_text()
        self.assertIn('- Repository HEAD: '+'a'*40+', the same when the shift began and when it ended.',text)
        self.assertIn('Nothing uncommitted was seen in the repository when the shift ended',text)

    def test_a_repository_that_was_not_clean_at_the_start_is_said_so(self):
        """On a real shift, two files untracked before the shift went into the builder's commit;
        nothing was left at the end, so nothing in the report said they had been there."""
        old=[dict(path='.gitignore',state='untracked'),dict(path='tests/test_x.py',state='untracked')]
        self.driver.repo=dict(start=dict(head='a'*40,changes=old,truncated=False),
                              end=dict(head='b'*40,changes=[],truncated=False))
        with self.no_external(): self.driver.finish('done')
        text=(self.shift/'report.md').read_text()
        self.assertIn('- The repository was not clean when the shift began: 2 uncommitted paths were seen then '
                      '(.gitignore, tests/test_x.py). A commit made during the shift may hold them.',text)
        self.assertEqual(text.count('Nothing uncommitted was seen in the repository when the shift ended'),1)
        # One path seen in two states (staged, then deleted from disk) is one path.
        twice=[dict(path='a.txt',state='staged'),dict(path='a.txt',state='deleted'),dict(path='b.txt',state='untracked')]
        self.driver.repo=dict(start=dict(head='a'*40,changes=twice,truncated=True),
                              end=dict(head='b'*40,changes=[],truncated=False))
        with self.no_external(): self.driver.finish('done')
        self.assertIn('began: 2 or more uncommitted paths were seen then (a.txt, b.txt).',(self.shift/'report.md').read_text())
        self.driver.repo=dict(start=dict(head='a'*40,changes=[],truncated=False),
                              end=dict(head='b'*40,changes=[],truncated=False))
        with self.no_external(): self.driver.finish('done')
        self.assertNotIn('was not clean',(self.shift/'report.md').read_text())

    def test_long_text_is_cut_at_a_word_and_said_to_be_shortened(self):
        words='inspect '*100
        self.assertEqual(report.plain(words,30),'inspect inspect inspect (shortened; report.json has the rest)')
        self.assertEqual(report.plain('a'*10+' '+'b'*40,30),'a'*10+' '+'b'*19+' (shortened; report.json has the rest)')

    def test_a_look_at_the_repository_that_was_cut_or_missing_is_not_reported_as_the_whole_picture(self):
        left=[dict(path='tests/test_x.py',state='untracked')]
        def report(start,end):
            self.driver.repo=dict(start=start,end=end)
            with self.no_external(): self.driver.finish('done')
            text=(self.shift/'report.md').read_text()
            return decode((self.shift/'report.json').read_bytes())['leftovers'],text.split('## Not done',1)[1].split('## ',1)[0]
        whole=dict(head='a'*40,changes=[],truncated=False)
        doc,undone=report(whole,dict(head='a'*40,changes=[],truncated=True))
        self.assertEqual((doc['known'],doc['complete']),(True,False))
        self.assertNotIn('Nothing uncommitted was seen in the repository when the shift ended.',undone)
        self.assertIn('was cut',undone); self.assertIn('git status',undone)
        doc,undone=report(dict(whole,truncated=True),dict(head='a'*40,changes=left,truncated=False))
        self.assertEqual((doc['complete'],doc['since_start']),(False,False))  # Age not known
        self.assertNotIn('new since the shift began',undone)
        self.assertIn('tests/test_x.py (untracked)',undone); self.assertIn('was cut',undone)
        doc,undone=report(dict(error='git did not answer'),dict(head='a'*40,changes=left,truncated=False))
        self.assertEqual((doc['new'],doc['since_start'],doc['complete']),(left,False,True))
        self.assertNotIn('new since the shift began',undone)
        self.assertIn('whether they were already there when the shift began is not known',undone)
        self.assertIn('tests/test_x.py (untracked)',undone)

    def test_unexpected_shapes_still_publish_a_report_that_points_at_the_json(self):
        for key,value in (('tasks',[{'id':7,'receipts':'x','turns':{'a':1},'candidate':[1]}]),('grant','text'),
                          ('actions',[3,{'record':None}]),('blockers',[None]),('issues',['x']),('limits',None)):
            doc=dict(self.generate()); doc[key]=value
            self.assertIn(b'## Outcome',report.rendered(doc,'0'*64))
        with patch.object(report,'markdown',side_effect=TypeError('shape')):
            text=report.rendered(self.generate(),'0'*64).decode()
        self.assertIn('could not be written (TypeError); read report.json',text)
        self.assertIn('SHA256 of report.json: '+'0'*64+'\n',text)

    def test_missing_or_changed_receipt_and_logs_do_not_corroborate_acceptance(self):
        for path in ('receipts/review.json','logs/review.txt','logs/tests.txt'):
            before=(self.shift/path).read_bytes();(self.shift/path).unlink()
            doc=self.generate();self.assertIn('uncorroborated',doc['tasks'][0]['acceptance'])
            (self.shift/path).write_bytes(before+b'changed')
            doc=self.generate();self.assertIn('uncorroborated',doc['tasks'][0]['acceptance'])
            (self.shift/path).write_bytes(before)
        self.entries.clear();self.store();doc=self.generate()
        self.assertIn('uncorroborated',doc['tasks'][0]['acceptance'])

    def test_stale_review_has_both_candidate_and_exact_reviewed_target(self):
        self.task['candidate']=dict(kind='commit',commit='b'*40)
        self.task['accepted']['candidate']=self.task['candidate'];self.store()
        doc=self.generate();task=doc['tasks'][0]
        self.assertIn('uncorroborated',task['acceptance'])
        self.assertEqual(task['candidate']['commit'],'b'*40)
        self.assertEqual(task['receipts'][1]['data']['target']['commit'],'a'*40)

    def test_a_missing_consumed_ledger_is_damage_only_when_a_receipt_was_recorded(self):
        self.store(); (self.shift/'receipts/.consumed.json').unlink()
        recorded = bool(self.task['receipts'])
        self.assertEqual(any(i['path'] == 'receipts/.consumed.json' for i in self.generate()['issues']), recorded)
        self.task['receipts'] = []; self.task['accepted'] = None; self.task['cessation'] = None
        self.store(); (self.shift/'receipts/.consumed.json').unlink()
        self.assertFalse([i for i in self.generate()['issues'] if 'consumed' in i['path']])

    def test_partial_preparation_and_damaged_state(self):
        (self.shift/'driver-state.json').write_text('{')
        doc=self.generate();self.assertEqual(doc['status'],'partial');self.assertEqual(doc['next_owner'],'developer')
        (self.shift/'grant.json').rename(self.shift/'grant.draft.json')
        doc=self.generate();self.assertEqual(doc['grant']['provenance'],'unsigned preparation only')
        self.assertEqual(doc['actual_window']['value'],'unknown')

    def test_preparation_draft_allows_finite_fractional_duration(self):
        (self.shift/'grant.json').unlink()
        draft=dict(self.grant, duration_seconds=60.0)
        (self.shift/'grant.draft.json').write_text(json.dumps(draft))
        self.assertEqual(self.generate()['grant']['provenance'], 'unsigned preparation only')

    def test_malformed_task_and_closeout_are_partial_not_exceptions(self):
        for value in (None, [], {'receipts': []}):
            self.state['tasks']['task'] = value; self.store()
            self.assertEqual(self.generate()['status'], 'partial')
        self.state['tasks']['task'] = self.task; self.store()
        self.artifact('closeout.json', '{"version":1,"elapsed_seconds":-1}')
        doc = self.generate()
        self.assertEqual(doc['actual_window']['value'], 'unknown')
        self.assertEqual(doc['status'], 'partial')

    def test_host_expiry_source_change_and_interruption_finalize_without_external_calls(self):
        for why in ('shift expired', 'source changed', KeyboardInterrupt()):
            guard = dict(side_effect=why) if isinstance(why, BaseException) else dict(return_value=why)
            with patch.object(Driver, 'guard', **guard), self.no_external():
                code, reason = host.run_shift(self.admission, self.spawn, seconds=100)
            doc = decode((self.shift/'report.json').read_bytes())
            self.assertEqual(doc['actual_window']['reason'], reason)
            self.assertIn(code, (3, 130))
            self.assertEqual(doc['status'], 'evidence summarized')

    def test_partial_admitted_start_finalizes_without_retry_or_host(self):
        def begin(guard):
            raise ValueError('fixture start refusal')
        def never(seconds):
            raise AssertionError('host must not start')
        with self.no_external():
            with self.assertRaisesRegex(ValueError, 'fixture start refusal'):
                launcher.admitted_start(self.shift, self.admission, seconds=100,
                                        until=self.clock.utc()+dt.timedelta(seconds=100),
                                        begin=begin, host=never)
        doc = decode((self.shift/'report.json').read_bytes())
        self.assertEqual(doc['actual_window']['source'], 'admitted start failed')
        self.assertEqual(self.spawn.started, [])

    def test_seat_cessation_stays_reported_and_artifact_bound(self):
        self.task['cessation']=self.add_receipt('stop');self.store()
        doc=self.generate();self.assertIn('reported',doc['tasks'][0]['cessation'])
        (self.shift/'logs/stop.txt').unlink();doc=self.generate()
        self.assertIn('unknown',doc['tasks'][0]['cessation'])

    def test_prior_comparison_requires_older_same_repo_and_complete_metrics(self):
        prior=self.generate();prior['shift_id']='prior';prior['grant']['start']='2029-01-01T00:00:00+00:00'
        (self.shift/'logs/prior.json').write_bytes(receipts.encoded(prior))
        doc=self.generate(previous='logs/prior.json');self.assertTrue(doc['prior']['eligible'])
        self.assertEqual(doc['delta']['tasks']['value'],0)
        for key,value in [('repo','/other/project'),('shift_id','current')]:
            changed=copy.deepcopy(prior);changed[key]=value
            (self.shift/'logs/prior.json').write_bytes(receipts.encoded(changed))
            doc=self.generate(previous='logs/prior.json');self.assertFalse(doc['prior']['eligible'])
            self.assertEqual(doc['delta']['tasks']['value'],'unknown')

    def test_large_retained_numeric_prior_does_not_overflow_float_conversion(self):
        prior=self.generate(); prior['shift_id']='prior'; prior['grant']['start']='2029-01-01T00:00:00Z'
        prior['metrics']['tasks']['value']=10**400
        (self.shift/'logs/prior.json').write_bytes(receipts.encoded(prior))
        doc=self.generate(previous='logs/prior.json')
        self.assertEqual(doc['delta']['tasks']['value'], 1-10**400)

    def test_special_files_and_escape_references_never_read_external_bytes(self):
        p=self.shift/'logs/review.txt';p.unlink();os.mkfifo(p)
        begun=time.monotonic();doc=self.generate();self.assertLess(time.monotonic()-begun,1)
        self.assertIn('uncorroborated',doc['tasks'][0]['acceptance'])
        p.unlink();p.symlink_to(self.root/'outside')
        self.assertEqual(self.generate()['status'],'partial')
        self.assertEqual(self.generate(previous='../outside')['status'],'partial')

    def test_limits_emit_partial_and_markdown_does_not_activate_retained_html(self):
        self.task['reason']='<script>bad</script>';self.store()
        with patch.object(report,'MAX_RECEIPTS',1):doc=self.generate()
        self.assertEqual(doc['status'],'partial')
        self.generate();text=(self.shift/'report.md').read_text()
        self.assertNotIn('<script>',text);self.assertIn('&lt;script&gt;',text)
        with patch.object(report.Reader,'guard',side_effect=ValueError('extraction budget exhausted')):
            self.assertEqual(self.generate()['status'],'partial')

    def test_byte_and_output_limits_publish_explicit_omissions(self):
        self.task['reason'] = '&' * 100000; self.store()
        doc = self.generate(); self.assertEqual(doc['status'], 'partial')
        self.assertLess((self.shift/'report.md').stat().st_size, receipts.DOCUMENT_LIMIT)
        self.state['next_owner'] = '&' * 300000; self.state['not_started'] = [dict(name='second', reason='no time')]
        self.store()
        with patch.object(receipts, 'DOCUMENT_LIMIT', 200000):  # the bound on each output, not on reading
            doc = self.generate()
        self.assertEqual(doc['next_owner'], 'developer')
        self.assertIn('output limit', doc['issues'][0]['reason'])
        # The short report drops the tasks, so it must not keep a count of what came after them.
        self.assertIsNone(doc['not_started'])
        self.assertIn('- Tasks: none briefed; whether the direction held more is not known to the host.\n',
                      (self.shift/'report.md').read_text())

    def test_publication_failure_is_not_success_and_json_digest_detects_mixed_pair(self):
        self.generate();old=(self.shift/'report.md').read_bytes()
        real=receipts.Evidence.atomic
        def fail(view,path,raw,**kw):
            if path=='report.md':raise OSError('simulated final write failure')
            return real(view,path,raw,**kw)
        self.task['reason']='changed';self.store()
        with patch.object(receipts.Evidence,'atomic',fail),self.assertRaises(ValueError):self.generate()
        self.assertEqual((self.shift/'report.md').read_bytes(),old)
        self.assertNotIn(receipts.digest((self.shift/'report.json').read_bytes()).encode(),old)

    def test_cli_duration_and_diagnostic_report_output(self):
        with self.no_external(),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['report','--shift',str(self.shift),'--for','2s']),0)
        self.assertEqual(json.loads(out.getvalue())['summary'],'report.json')
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            cli.main(['report','--shift',str(self.shift),'--for','inf'])
