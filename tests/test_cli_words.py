"""What an agent CLI wrote last before it failed: what is quoted, what is left out, where
the words go for a builder turn, a review and the driver's CLI, and what they name.

No real agent CLI is started: a builder and a reviewer are the fixture's stand-ins, and
what they "print" is bytes the test puts on their pipes. Every credential here is made
up, and put together from pieces so that no line of this file has the shape of one.
"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from sortie_kit import agents, cli_words, shift_wait
from sortie_kit.host import run_shift
from sortie_kit.lifecycle import TURN_LOG_LIMIT
from sortie_kit.receipt_schema import decode
from test_host_review import ReviewFixture
from test_lifecycle import LifecycleFixture

LEFT = cli_words.LEFT_OUT
MIXED = "A1b2C3d4E5f6G7h8J9k0"  # letters and digits mixed: the shape of a credential
PAT = "gh" + "p_" + MIXED
KEYLIKE = "s" + "k-" + "proj" + MIXED


class LastTests(unittest.TestCase):
    def test_the_last_lines_are_bounded_in_number_in_length_and_in_all(self):
        lines = cli_words.last("".join("line %d\n" % n for n in range(20)).encode())
        self.assertEqual(lines, ["line %d" % n for n in range(14, 20)])
        long = cli_words.last(("word " * 100 + "\nshort\n").encode())
        self.assertEqual(len(long[0]), cli_words.LINE)
        self.assertTrue(long[0].endswith(cli_words.CUT))
        self.assertEqual(long[1], "short")
        # Six lines of 200 characters are more than the 800 in all: the last ones are kept.
        many = cli_words.last("".join(str(n) + " " + "x " * 150 + "\n" for n in range(6)).encode())
        self.assertLessEqual(sum(len(line) for line in many), cli_words.TOTAL)
        self.assertEqual([line[0] for line in many], ["2", "3", "4", "5"])

    def test_nothing_gives_nothing_and_what_is_no_bytes_too(self):
        for value in (b"", None, "text", b"\n\n \n\x1b[0m\n"):
            self.assertEqual(cli_words.last(value), [])

    def test_control_sequences_are_removed_and_what_is_no_text_becomes_a_space(self):
        raw = b"\x1b[31mError:\x1b[0m model\x00gone\x07\r\n\x1b]0;title\x07next \xff line\rlast\tone\x1b(B\n"
        self.assertEqual(cli_words.last(raw), ["Error: model gone", "next line", "last one"])

    def test_only_the_end_is_read_and_a_first_line_that_may_be_cut_is_dropped(self):
        """Of more than the 8 KiB that are kept, the beginning may be the middle of a
        line or of a secret, which no mask could read as what it was."""
        raw = b"api_key=" + (MIXED * 500).encode() + b"\nthe error\n"
        self.assertEqual(cli_words.last(raw), ["the error"])
        # One long line: all of it goes. What stands before a space may be the name or the scheme word
        # that a mask needs to know the value after it.
        raw = (MIXED * 500).encode() + b" then the error"
        self.assertEqual(cli_words.last(raw), [])
        self.assertEqual(cli_words.last((MIXED * 500).encode()), [])
        raw = ('{"items":[' + "1," * 4200 + '0],"Authorization":"Bearer short' + 'token123"}').encode()
        self.assertEqual(cli_words.last(raw), [])
        # The rolling end the host keeps of a pipe is what `last` is given.
        kept = b""
        for chunk in (b"x" * 5000, b"y" * 5000, b"\nthe end\n"):
            kept = cli_words.tail(kept, chunk)
        self.assertEqual((len(kept), cli_words.last(kept)), (cli_words.TAIL, ["the end"]))

    def test_what_has_the_shape_of_a_credential_is_left_out(self):
        for line, wanted in (
                ("api_key=" + MIXED + " was refused", "api_key=" + LEFT + " was refused"),
                ('{"error": "bad", "api_key": "' + MIXED + '", "n": 1}', '{"error": "bad", "api_key": "' + LEFT + '", "n": 1}'),
                ("ACCESS_TOKEN: abc", "ACCESS_TOKEN: " + LEFT),
                ("client_secret = 'hunter2hunter2'", "client_secret = '" + LEFT + "'"),
                ("password=short&next=1", "password=" + LEFT + "&next=1"),
                ("Authorization: Bearer " + MIXED, "Authorization: Bearer " + LEFT),
                ("authorization=Basic dXNlcjpwYXNz", "authorization=Basic " + LEFT),
                ("sent Bearer " + MIXED + " and was refused", "sent Bearer " + LEFT + " and was refused"),
                ("Set-Cookie: session=abc; Path=/", "Set-Cookie: " + LEFT),
                ('{"session_cookie": "abc123"}', '{"session_cookie": "' + LEFT + '"}'),
                ("fetch https://user:pass" + "word@example.invalid/x failed", "fetch https://" + LEFT + "@example.invalid/x failed"),
                ("token " + PAT + " expired", "token " + LEFT + " expired"),
                ("key " + KEYLIKE + " is not valid", "key " + LEFT + " is not valid"),
                ("id " + "0123456789abcdef" * 4 + " unknown", "id " + LEFT + " unknown"),
                ("blob " + (MIXED + "+") * 2 + "==", "blob " + LEFT),
                ('Set-Cookie: session="my' + 'secret123"; HttpOnly', "Set-Cookie: " + LEFT),
                ('Authorization: Bearer "my' + 'secret123"', "Authorization: Bearer " + LEFT),
                ('Authorization: Digest username="alice", nonce="short' + 'nonce", uri="/x"', "Authorization: Digest " + LEFT),
                ('{"Authorization": "Bearer my' + 'tok1234", "error": "model gone"}',
                 '{"Authorization": "Bearer ' + LEFT + '", "error": "model gone"}'),
                ('{"cookie": "sid=abc; theme=dark", "error": "model gone"}', '{"cookie": "' + LEFT + '", "error": "model gone"}'),
                ('body {\\"api_key\\":\\"my_short_' + 'secret\\"} refused', 'body {\\"api_key\\":\\"' + LEFT + '\\"} refused'),
                ('{\\"password\\":\\"hunter' + '2\\",\\"n\\":1}', '{\\"password\\":\\"' + LEFT + '\\",\\"n\\":1}'),
                ('{\\"Authorization\\":\\"Bearer my' + 'tok1234\\"}', '{\\"Authorization\\":\\"Bearer ' + LEFT + '\\"}'),
                ('{\\"Set-Cookie\\":\\"sid=abc\\"}', '{\\"Set-Cookie\\":\\"' + LEFT + '\\"}'),
                ('password: "two words ' + 'here" was refused', 'password: "' + LEFT + '" was refused'),
                ("api_key\x1b(B=" + "short" + "secret", "api_key=" + LEFT),
                ("key s" + "k-ab\ufffd1234567890 refused", "key " + LEFT + " refused")):
            with self.subTest(line=line):
                self.assertEqual(cli_words.last(line.encode()), [wanted])
        # A byte that is no text inside a credential does not make two short words of it.
        self.assertEqual(cli_words.last(b"key s" + b"k-ab\xff1234567890 refused"), ["key " + LEFT + " refused"])

    def test_a_quote_inside_a_value_does_not_end_it_however_often_the_text_was_encoded(self):
        """Major of the merge gate: in a JSON text that is itself a string the quotes of a field have
        one backslash before them and a quote inside the value has three. The mask took the first
        backslash and quote it met for the end and showed the rest of the password."""
        inner, tail = 'prefix9"tail', "SEC" + "RET7"
        for depth in range(4):
            for value in (inner + tail, "short" + tail, "two words " + tail, "ends in a backslash\\" + tail[:0]):
                line = json.dumps({"password": value, "n": 1})
                for _ in range(depth):
                    line = json.dumps(line)
                with self.subTest(depth=depth, value=value):
                    got, = cli_words.last(line.encode())
                    self.assertIn(LEFT, got)
                    for part in (tail, "prefix9", "short", "two words", "backslash"):
                        self.assertNotIn(part, got)
                    self.assertIn("password", got)
        # What follows the value is kept where the value's end is known: the field after it.
        got, = cli_words.last(json.dumps(json.dumps({"password": inner + tail, "error": "model gone"})).encode())
        self.assertIn("model gone", got)
        got, = cli_words.last(json.dumps(json.dumps({"Authorization": "Bearer " + inner + tail, "error": "model gone"})).encode())
        self.assertNotIn(tail, got)
        self.assertIn("Bearer " + LEFT, got)
        self.assertIn("model gone", got)
        got, = cli_words.last(json.dumps(json.dumps({"cookie": "sid=" + inner + tail})).encode())
        self.assertNotIn(tail, got)
        # A value whose quote never comes back goes to the end of the line.
        self.assertEqual(cli_words.last(b'password: "never closed ' + tail.encode() + b" and more"), ['password: "' + LEFT])

    def test_a_long_line_with_nothing_to_mask_is_not_searched_from_every_character(self):
        """First Minor of the merge gate: the name before `=` was looked for from each character of a
        long word again. No clock is asked here: the pattern that names a value begins with one of
        the words, so a word without them is passed over once."""
        self.assertEqual(cli_words.last(b"a" * 4000 + b" end"), [LEFT + " end"])
        self.assertTrue(cli_words.ASSIGNED.pattern.startswith("(?i)" + cli_words.SECRET_NAME))
        for line, wanted in (("my_api_key=" + MIXED, "my_api_key=" + LEFT), ("x.y-z_token: abc", "x.y-z_token: " + LEFT),
                             ("monkey business", "monkey business")):
            self.assertEqual(cli_words.last(line.encode()), [wanted])

    def test_a_secret_that_a_cut_would_have_shortened_is_masked_before_the_cut(self):
        """Finding 4 of the agy read: the line this test had was cut before its secret began, so
        it passed with the cut made first. Here the cut falls 19 characters into a run of 40 that
        only the mask of long runs names: cut first, those 19 would be shown."""
        line = "word " * 35 + MIXED * 2 + " tail" * 10
        self.assertLess(175, cli_words.LINE - len(cli_words.CUT))
        got, = cli_words.last(line.encode())
        self.assertEqual(len(got), cli_words.LINE)
        self.assertNotIn(MIXED[:4], got)
        self.assertTrue(got.startswith("word " * 35 + LEFT + " tail"), got)
        self.assertTrue(got.endswith(cli_words.CUT), got)

    def test_the_errors_this_is_for_are_kept_as_they_are(self):
        for line in ("Error: invalid API key provided",
                     "refresh token expired, sign in again",
                     "EXAMPLE_API_KEY not set",
                     "Basic authentication failed for this account",
                     "model example-sonnet-4-5-20250929-thinking does not exist",
                     "unknown option --experimental-enable-feature-foo",
                     "EXAMPLE_LONG_SETTING_NAME_IN_CAPITALS is not supported",
                     "cannot read /Users/someone/.config/example/settings.json: permission denied",
                     "status 401 Unauthorized (request 12345)"):
            with self.subTest(line=line):
                self.assertEqual(cli_words.last(line.encode()), [line])

    def test_what_is_quoted_of_a_turn_and_of_a_review(self):
        err, out = b"the error\n", b"the output\n"
        said = cli_words.turn_said
        self.assertEqual(said("exit", 1, True, err), ["the error"])
        self.assertEqual(said("exit; stop not confirmed", 1, None, err), ["the error"])
        self.assertEqual(said("signal", -11, None, err), ["the error"])
        self.assertEqual(said("exit", 0, False, err), ["the error"])
        for outcome, code, changed in (("exit", 0, True), ("exit", 0, None), ("timeout", -9, False),
                                       ("stopped: host stopped", -15, False), ("not started: no", None, None),
                                       ("lost", None, None)):
            self.assertEqual(said(outcome, code, changed, err), [], outcome)
        said = cli_words.review_said
        self.assertEqual(said("exit", 2, True, out, err), ["the error"])
        self.assertEqual(said("exit", 2, True, out, b""), ["the output"])
        self.assertEqual(said("signal", -6, True, out, None), ["the output"])
        self.assertEqual(said("exit", 0, True, out, err), ["the error"])
        self.assertEqual(said("exit", 0, True, out, b""), [])  # what stood in place of the review is not quoted
        self.assertEqual(said("exit", 0, False, out, err), [])  # a review that was taken
        self.assertEqual(said("timeout", -9, True, out, err), [])
        self.assertEqual(said("stopped: host stopped; stop not confirmed", -15, True, out, err), [])
        self.assertEqual(said(cli_words.LATE, 1, True, out, err), ["the error"])


class KnobsTests(unittest.TestCase):
    def seat(self, role, name, network=False):
        profile = agents.load(name, "/nowhere", home="/nowhere/home")
        return agents.seat(role, profile, "model-x", "family-x", {"path": "/opt/tools/" + name, "sha256": "0" * 64,
                                                                   "bytes": 1}, network=network)

    def test_the_sentences_name_the_model_the_form_and_where_the_profile_is(self):
        words = cli_words.knobs("builder", self.seat("builder", "codex"))
        for part in ("in a new shift", "cannot be changed in this one", "model-x in --agent builder=codex:model-x",
                     "headless_argv of the profile codex (built into sortie-kit", "copy the profile under a new name",
                     "sign-in and settings, outside sortie-kit", "sortie doctor shows",
                     "does not show a sign-in or whether a model exists"):
            self.assertIn(part, words)
        self.assertNotIn("headless_network_argv", words)
        # From a real shift: a provider refused the content of two builder turns, and no model name, option or
        # sign-in was at fault. The paragraph names what can be changed and says that none of it need be the cause.
        for role, name in (("builder", "codex"), ("reviewer", "claude"), ("driver", "claude")):
            self.assertTrue(cli_words.knobs(role, self.seat(role, name)).endswith(
                " None of these need be what went wrong: the host saw how the process ended, and not why."))
        self.assertIn("headless_network_argv of the profile codex",
                      cli_words.knobs("builder", self.seat("builder", "codex", network=True)))
        self.assertIn("review_argv of the profile claude", cli_words.knobs("reviewer", self.seat("reviewer", "claude")))
        words = cli_words.knobs("driver", self.seat("driver", "claude"))
        self.assertIn("interactive_argv of the profile claude", words)
        self.assertIn("--agent driver=claude:model-x", words)

    def test_a_profile_of_the_persons_own_is_named_as_their_file_and_doctor_with_its_name(self):
        words = cli_words.knobs("builder", dict(agent="mine", model="m1", argv=["run", "{prompt}", "{model}"]))
        for part in ("your own file ~/.config/sortie-kit/agents/mine.json", "sortie doctor --agent mine shows",
                     "headless_argv (headless_network_argv in a shift started with --builder-network)"):
            self.assertIn(part, words)

    def test_a_seat_of_any_shape_gives_words_and_its_names_go_through_the_callers_filter(self):
        for seat in (None, {}, dict(agent=3, model=None), "seat"):
            self.assertIn("--agent reviewer=NAME:MODEL", cli_words.knobs("reviewer", seat))
        shown = cli_words.knobs("builder", dict(agent="a*b", model="m_1"), lambda text: "<" + text + ">")
        self.assertIn("--agent builder=<a*b>:<m_1>", shown)
        self.assertIn("<m_1> in --agent reviewer=<a*b>:<m_1>",
                      cli_words.limit_words(dict(agent="a*b", model="m_1"), 300, lambda text: "<" + text + ">"))

    def test_the_drivers_lines_are_only_for_a_cli_that_ended_by_itself_and_not_well(self):
        seat = self.seat("driver", "claude")
        for seen in ("exited on its own with status 1", "exited on its own with status 2; the terminal was restored",
                     "ended on its own by signal 9"):
            lines = cli_words.driver_words(seen, seat)
            self.assertEqual(len(lines), 2, seen)
            self.assertIn("above in this terminal", lines[0])
            self.assertIn("does not read or keep", lines[0])
            self.assertEqual(lines[1], cli_words.knobs("driver", seat))
        for seen in ("exited on its own with status 0", "stopped by the host", "not started",
                     "stop signaled; exit not observed", None):
            self.assertEqual(cli_words.driver_words(seen, seat), [], seen)
        self.assertIn("the terminal the shift was started in",
                      cli_words.driver_words("exited on its own with status 1", seat, terminal=False)[0])


class Waiting:
    def wait(self):
        return shift_wait.wait(self.shift, 1, clock=self.clock.now, sleep=self.clock.sleep, now=self.clock.utc)

    def report(self, reason="the fixture ends the shift"):
        self.driver.finish(reason)
        return (self.shift / "report.md").read_text(), decode((self.shift / "report.json").read_bytes())


class TurnWordsTests(Waiting, LifecycleFixture):
    """A builder turn whose CLI ended by itself and not well."""
    ERROR = b"working...\nError: The model `no-such-model` does not exist or you do not have access to it.\n"

    def turn(self):
        return self.state()["tasks"]["task"]["turns"][-1]

    def test_a_builder_that_prints_an_error_and_exits_1_is_quoted_to_the_driver_with_what_can_be_changed(self):
        self.begin(running=True)
        self.prints(self.ERROR)
        self.end_turn(1)
        said = ["working...", "Error: The model `no-such-model` does not exist or you do not have access to it."]
        self.assertEqual(self.turn()["said"], said)
        result = self.wait()
        self.assertEqual((result["status"], result["last_turn"]["said"]), ("ready", said))
        note = result["note"]
        for part in ("builder turn 1 ended by its CLI's own doing and not well (exit, exit 1)",
                     "last_turn.said holds the last lines the builder's CLI wrote", "nothing to follow",
                     "All it wrote is in " + result["last_turn"]["log"],
                     cli_words.knobs("builder", self.value["seats"]["builder"]),
                     "--agent builder=fixture:MODEL", "developer as next owner", "a driver cannot change a seat"):
            self.assertIn(part, note)
        self.assertNotIn("no-such-model", note)  # the CLI's words are in a list of their own, never in the host's sentences
        self.assertNotIn("the same way", note)  # the host saw how a process ended, not a cause

    def test_a_password_with_a_quote_in_it_reaches_neither_the_driver_nor_the_report(self):
        """The merge gate showed its Major through these: sortie wait, report.md and report.json."""
        tail = "SEC" + "RET7"
        self.begin(running=True)
        self.prints(json.dumps(json.dumps({"password": 'prefix9"tail' + tail})).encode() + b"\n")
        self.end_turn(1)
        result = self.wait()
        self.assertEqual(len(result["last_turn"]["said"]), 1)
        self.assertNotIn(tail, json.dumps(result))
        text, doc = self.report()
        self.assertNotIn(tail, text)
        self.assertNotIn(tail, json.dumps(doc))
        self.assertIn("left out", text)

    def test_the_report_shows_the_lines_under_the_turn_as_inert_text(self):
        self.begin(running=True)
        self.prints(b"# A heading\n[a link](https://example.invalid) <script>x</script>\n- **bold** `code`\n")
        self.end_turn(1)
        text, doc = self.report()
        self.assertEqual(doc["tasks"][0]["turns"][0]["said"],
                         ["# A heading", "[a link](https://example.invalid) <script>x</script>", "- **bold** `code`"])
        lines = text.splitlines()
        at = next(n for n, line in enumerate(lines) if line.startswith("  - Turn 1: exit with status 1"))
        self.assertEqual(lines[at + 1:at + 6], [
            "    - The builder's CLI wrote last (its own words, shown as text; what looks like a credential is left out):",
            "      - \\# A heading",
            "      - \\[a link\\](https://example.invalid) &lt;script&gt;x&lt;/script&gt;",
            "      - \\- \\*\\*bold\\*\\* \\`code\\`",
            "    - " + cli_words.knobs("builder", self.value["seats"]["builder"])])
        self.assertTrue(lines[at + 6].startswith("- Builder stop:"), lines[at + 6])
        self.assertNotIn("\n# A heading", text)

    def test_past_what_the_log_holds_the_quote_is_still_the_end_of_what_was_printed(self):
        self.begin(running=True)
        self.prints(b"x" * 100 + b"\n" + b"y" * TURN_LOG_LIMIT + b"\nError: the end of it\n")
        self.driver.poll_turn()  # one pass while it runs reads as much as the log holds
        self.end_turn(1)
        row = self.turn()
        self.assertEqual(row["said"], ["Error: the end of it"])
        self.assertGreater(row["output_dropped_bytes"], 0)
        self.assertNotIn(b"the end of it", (self.shift / row["log"]).read_bytes())

    def test_a_turn_with_an_unconfirmed_stop_after_its_own_exit_is_quoted_too(self):
        self.begin(running=True)
        self.prints(self.ERROR)
        with mock.patch("sortie_kit.tick_supervise.stop_group", side_effect=PermissionError("no signal")):
            self.spawn.child.code = 1
            self.driver.poll_turn(stop="stopped: host stopped")
        row = self.turn()
        self.assertEqual((row["outcome"], len(row["said"])), ("exit; stop not confirmed", 2))
        self.assertIn("by its CLI's own doing", shift_wait.turn_note(row, None))

    def test_a_cli_that_printed_nothing_gets_the_note_and_the_sentences_and_no_lines(self):
        self.begin(running=True)
        self.end_turn(3)
        self.assertNotIn("said", self.turn())
        note = self.wait()["note"]
        self.assertIn("ended by its CLI's own doing and not well (exit, exit 3)", note)
        self.assertNotIn("last_turn.said", note)
        self.assertIn("What a person can change", note)
        text, _ = self.report()
        self.assertIn("    - What a person can change, in a new shift", text)
        self.assertNotIn("CLI wrote last", text)

    def test_a_turn_the_host_ended_at_its_limit_is_not_quoted(self):
        self.begin(running=True)
        self.prints(b"half a sentence of work in progr")
        self.clock.sleep(self.state()["turn"]["limit_seconds"] + 1)
        self.driver.poll_turn(stop="stopped: working time over")
        row = self.turn()
        self.assertTrue(row["outcome"].startswith("stopped: working time over"), row)
        self.assertNotIn("said", row)
        self.assertIsNone(shift_wait.turn_note(dict(row, receipts_changed=None), None))

    def test_a_turn_that_ran_to_its_own_limit_is_not_quoted(self):
        self.assertEqual(self.action("brief", task_id="task", kind="feature", brief=self.artifact("briefs/task.md"),
                                     next_owner="builder", turn_seconds=5)["status"], "recorded")
        self.prints(b"half a sentence of work in progr")
        self.clock.sleep(5)
        self.driver.poll_turn()
        row = self.turn()
        self.assertEqual(cli_words.how(row["outcome"]), "timeout", row)
        self.assertNotIn("said", row)

    def test_the_lines_of_an_earlier_turn_are_said_to_be_output_while_the_next_turn_runs(self):
        """Finding 7: `sortie wait` gave last_turn.said without the words that say what it is
        whenever a newer turn was running."""
        self.begin(running=True)
        self.prints(self.ERROR)
        self.end_turn(1)
        self.assertEqual(self.action("continue")["status"], "recorded")
        result = self.wait()
        self.assertIsNotNone(result["turn"])
        self.assertEqual(len(result["last_turn"]["said"]), 2)
        for part in ("last_turn is builder turn 1, which ended before the turn that runs now",
                     "last_turn.said holds the last lines the builder's CLI wrote", "nothing to follow"):
            self.assertIn(part, result["note"])
        # A turn before it that left no such lines says nothing while the next one runs.
        self.end_turn(0)
        self.assertEqual(self.action("continue")["status"], "recorded")
        self.assertNotIn("note", self.wait())

    def test_status_0_without_a_receipt_keeps_its_note_and_says_where_the_lines_are(self):
        self.begin(running=True)
        self.prints(b"I could not write to the receipts folder.\n")
        self.end_turn(0)
        self.assertEqual(self.turn()["said"], ["I could not write to the receipts folder."])
        note = self.wait()["note"]
        for part in ("builder turn 1 ended (exit, exit 0)", "looked the same before and after",
                     "last_turn.said holds the last lines", "sandbox or permission"):
            self.assertIn(part, note)
        # A turn that left a receipt and ended well says nothing and is not quoted.
        self.assertEqual(self.action("continue")["status"], "recorded")
        (self.shift / "receipts/ack.json").write_text("{}")
        self.prints(b"done\n")
        self.end_turn(0)
        self.assertNotIn("said", self.turn())
        self.assertNotIn("note", self.wait())

    def test_an_exit_with_another_status_after_a_receipt_says_to_look_there_first(self):
        self.begin(running=True)
        (self.shift / "receipts/ack.json").write_text("{}")
        self.prints(b"warning: exiting with an error after the work\n")
        self.end_turn(1)
        note = self.wait()["note"]
        self.assertIn("receipts/ changed while it ran, so look first whether it handed in", note)

    def test_a_state_and_a_report_without_any_of_this_read_as_before(self):
        self.begin()
        self.assertNotIn("said", self.turn())
        text, doc = self.report()
        self.assertNotIn("CLI wrote last", text)
        self.assertNotIn("What a person can change", text)


class ReviewWordsTests(Waiting, ReviewFixture):
    """A run of the reviewer whose CLI ended by itself and not well, or that the host ended at its limit."""
    @staticmethod
    def attempts(doc):
        """What the report keeps of the attempts of the last review receipt of the task named task."""
        task = next(item for item in doc["tasks"] if item["id"] == "task")
        return task["receipts"][-1]["data"]["attempts"]

    def fails(self, err, out="", code=2):
        if err is not None:
            self.prints(err, "stderr")
        return self.end_review(out, code=code)

    def test_a_reviewer_that_fails_twice_is_quoted_in_the_answer_and_the_receipt_stays_as_it_is(self):
        self.reach_review()
        self.fails(b"Error: model no-such-model not found (first)\n")
        state = self.fails(b"warming up\nError: model no-such-model not found (second)\n")
        last, seat = state["last_review"], self.value["seats"]["reviewer"]
        self.assertEqual((last["outcome"], state["tasks"]["task"]["stage"]), ("FAILED", "blocked"))
        self.assertEqual(last["said"], dict(attempt=2, lines=["warming up", "Error: model no-such-model not found (second)"]))
        self.assertLessEqual(len(last["detail"]), 2000)
        for part in ("review failed twice: ", "no repair round was used",
                     "last_review.said holds the last lines the reviewer's CLI wrote in attempt 2",
                     "nothing to follow", cli_words.knobs("reviewer", seat)):
            self.assertIn(part, last["detail"])
        self.assertNotIn("no-such-model", last["detail"])
        self.assertEqual(self.wait()["last_review"]["said"], last["said"])
        # The receipt has the shape it had: no new key, and it was checked and recorded as any receipt is.
        attempts = self.review_receipt(state)["data"]["attempts"]
        self.assertEqual([sorted(set(item) - {"evidence"}) for item in attempts], [sorted(
            ("attempt", "directory", "instructions_sha256", "marker", "argv", "started", "ended", "elapsed_seconds",
             "exit_status", "ended_by", "stdout", "stderr", "output_dropped_bytes", "status", "verdict", "reasons",
             "record", "repository"))] * 2)
        self.assertEqual(self.action("brief", task_id="next", kind="defect", brief=self.artifact("briefs/task.md"),
                                     next_owner="builder")["status"], "recorded")  # the state loads and the shift goes on
        self.end_turn()
        text, doc = self.report()
        kept = self.attempts(doc)
        self.assertEqual([item["said"] for item in kept], [["Error: model no-such-model not found (first)"],
                                                           ["warming up", "Error: model no-such-model not found (second)"]])
        lines = text.splitlines()
        at = next(n for n, line in enumerate(lines) if line.startswith("    - Attempt 1: failed (exit with status 2"))
        self.assertEqual(lines[at + 1:at + 4], [
            "      - The reviewer's CLI wrote last (its own words, shown as text; what looks like a credential is left out):",
            "        - Error: model no-such-model not found (first)",
            "      - " + cli_words.knobs("reviewer", seat)])
        self.assertTrue(lines[at + 4].startswith("    - Attempt 2: failed (exit with status 2"), lines[at + 4])
        self.assertEqual(doc["issues"], [])

    def test_the_reasons_are_cut_so_that_the_sentences_stay_whole(self):
        self.reach_review()
        long = "What a person can change: " + "words " * 280
        with mock.patch.object(cli_words, "knobs", lambda *args, **kw: long):
            self.fails(b"Error: no\n")
            last = self.fails(b"Error: no\n")["last_review"]
        self.assertEqual(len(last["detail"]), 2000)
        self.assertTrue(last["detail"].endswith(long))
        self.assertIn(". The task is blocked for the developer; no repair round was used.", last["detail"])

    def test_the_report_quotes_from_the_kept_copy_when_a_log_was_changed_and_nothing_when_both_were(self):
        self.reach_review()
        self.fails(b"Error: the first words\n")
        state = self.fails(b"Error: the second words\n")
        first = self.review_receipt(state)["data"]["attempts"][0]["stderr"]
        (self.shift / first["path"]).write_bytes(b"Error: something a seat wrote later\n")
        from sortie_kit import receipts, report
        with receipts.session(self.shift, 5, local_only=True) as view:
            kept = self.attempts(report.extract(view))
        self.assertEqual(kept[0]["said"], ["Error: the first words"])
        copy = self.shift / receipts.kept_path(first["sha256"])
        copy.chmod(0o600)
        copy.write_bytes(b"Error: not what the host kept\n")
        with receipts.session(self.shift, 5, local_only=True) as view:
            kept = self.attempts(report.extract(view))
        self.assertNotIn("said", kept[0])
        self.assertEqual(kept[1]["said"], ["Error: the second words"])

    def test_the_report_quotes_nothing_from_a_log_that_was_written_to_while_the_review_ran(self):
        """Finding 1: the receipt names the log as it is, also when the host saw that it is not
        what the reviewer printed, and the report quoted that file as the CLI's words."""
        self.reach_review()
        running = self.state()["review"]["running"]
        self.prints(b"Error: what the reviewer printed\n", "stderr")
        with open(self.shift / running["stderr"], "ab") as stream:
            stream.write(b"Error: what a seat put into the log\n")
        self.end_review("", code=2)
        state = self.fails(b"Error: the second words\n")
        first = self.review_receipt(state)["data"]["attempts"][0]
        self.assertIn("is not what the reviewer printed", " ".join(first["reasons"]))
        text, doc = self.report()
        kept = self.attempts(doc)
        self.assertNotIn("said", kept[0])
        self.assertEqual(kept[1]["said"], ["Error: the second words"])
        self.assertNotIn("what a seat put into the log", text)
        # The CLI did fail by itself, so what a person can change is named under both attempts.
        self.assertEqual(text.count("      - " + cli_words.knobs("reviewer", self.value["seats"]["reviewer"])), 2)

    def test_the_report_quotes_nothing_of_a_run_that_printed_more_than_the_log_holds(self):
        """The end of a full log is not the end of what was printed, so the report, which has the
        log alone, quotes nothing; the answer to the driver has the end, from the pipe."""
        self.reach_review()
        self.prints(b"Error: early words\n" + b"words in the middle\n" * (TURN_LOG_LIMIT // 20 + 1)
                    + b"Error: the end of it\n", "stderr")
        self.driver.poll_turn()  # one pass while it runs reads as much as the log holds
        self.end_review("", code=2)
        state = self.fails(b"Error: the second words\n")
        first = self.review_receipt(state)["data"]["attempts"][0]
        self.assertGreater(first["output_dropped_bytes"], 0)
        text, doc = self.report()
        self.assertNotIn("said", self.attempts(doc)[0])
        self.assertNotIn("words in the middle", text)

    def test_a_reviewer_that_failed_by_itself_and_wrote_nothing_still_gets_what_can_be_changed(self):
        """Finding 8: a builder that wrote nothing got the sentences, a reviewer did not."""
        self.reach_review()
        self.end_review("", code=2)
        last = self.end_review("", code=2)["last_review"]
        seat = self.value["seats"]["reviewer"]
        self.assertNotIn("said", last)
        self.assertLessEqual(len(last["detail"]), 2000)
        for part in ("The reviewer's CLI ended by itself and not well in attempt 1 and 2",
                     "the host holds no last lines of it", cli_words.knobs("reviewer", seat)):
            self.assertIn(part, last["detail"])
        text, _ = self.report()
        self.assertEqual(text.count("      - " + cli_words.knobs("reviewer", seat)), 2)
        self.assertNotIn("CLI wrote last", text)

    def test_an_error_on_stdout_alone_is_quoted_of_a_run_that_ended_with_another_status(self):
        self.reach_review()
        self.fails(None, "Error: unknown model\n", code=1)
        last = self.fails(None, "Error: unknown model\n", code=1)["last_review"]
        self.assertEqual(last["said"], dict(attempt=2, lines=["Error: unknown model"]))

    def test_status_0_with_no_review_and_an_error_on_stderr_is_quoted_and_with_stdout_alone_is_not(self):
        self.reach_review()
        self.fails(None, "Error: on stdout only\n", code=0)
        state = self.fails(b"Error: on stderr\n", "I looked.\n", code=0)
        self.assertEqual(state["last_review"]["said"], dict(attempt=2, lines=["Error: on stderr"]))
        text, doc = self.report()
        self.assertNotIn("said", self.attempts(doc)[0])
        self.assertNotIn("on stdout only", text)

    def test_the_words_of_a_first_attempt_are_told_when_the_second_did_not_start(self):
        self.reach_review()
        self.prints(b"Error: the only words\n", "stderr")
        self.spawn.child.code = 2
        self.spawn.failure = OSError("no such program")
        self.driver.poll_turn()
        last = self.state()["last_review"]
        self.assertEqual(last["outcome"], "FAILED")
        self.assertEqual(last["said"], dict(attempt=1, lines=["Error: the only words"]))
        self.assertIn("in attempt 1", last["detail"])

    def test_a_review_the_host_ended_at_its_limit_is_not_quoted_and_names_the_limit_and_the_model(self):
        self.driver.review_limit = 100
        self.reach_review()
        for _ in range(2):
            self.prints(b"still reading the patch\n", "stderr")
            self.clock.sleep(101)
            self.driver.poll_turn()
        state = self.state()
        last, seat = state["last_review"], self.value["seats"]["reviewer"]
        self.assertEqual((last["outcome"], state["tasks"]["task"]["stage"]), ("FAILED", "blocked"))
        self.assertNotIn("said", last)
        self.assertLessEqual(len(last["detail"]), 2000)
        for part in ("The host ended attempt 1 and 2 at the limit of one run.", cli_words.limit_words(seat, 100),
                     "at most 100 seconds", "a longer --review-limit", "--agent reviewer=fixture:MODEL",
                     "does not know which"):
            self.assertIn(part, last["detail"])
        self.assertNotIn("still reading", last["detail"])
        text, doc = self.report()
        self.assertNotIn("still reading", text)
        self.assertNotIn("CLI wrote last", text)
        self.assertEqual(text.count("      - The host ended this run at the limit of one run. "
                                    + cli_words.limit_words(seat, 100)), 2)
        self.assertNotIn("said", self.attempts(doc)[0])

    def test_a_first_run_that_failed_by_itself_and_a_second_ended_at_its_limit_say_both(self):
        self.driver.review_limit = 100
        self.reach_review()
        self.fails(b"Error: model not found\n")
        self.clock.sleep(101)
        self.driver.poll_turn()
        last = self.state()["last_review"]
        self.assertEqual(last["said"], dict(attempt=1, lines=["Error: model not found"]))
        self.assertLessEqual(len(last["detail"]), 2000)
        for part in ("The host ended attempt 2 at the limit of one run.", "a longer --review-limit",
                     "in attempt 1", cli_words.knobs("reviewer", self.value["seats"]["reviewer"])):
            self.assertIn(part, last["detail"])

    def test_a_review_that_is_taken_says_none_of_this(self):
        self.reach_review()
        self.prints(b"note: a warning of the CLI\n", "stderr")
        state = self.end_review()
        self.assertEqual(state["last_review"]["outcome"], "ACCEPT")
        self.assertNotIn("said", state["last_review"])
        text, _ = self.report()
        for part in ("CLI wrote last", "What a person can change", "limit of one run"):
            self.assertNotIn(part, text)


class DriverWordsTests(LifecycleFixture):
    def ended(self, said):
        seat = SimpleNamespace(ended=lambda: True, close=lambda: said, group=4242)
        run_shift(self.admission, self.spawn, seconds=100, wind_down=10, driver_seat=seat)
        return (self.shift / "report.md").read_text()

    def test_the_report_says_where_the_words_of_a_driver_that_failed_are_and_what_can_be_changed(self):
        text = self.ended("exited on its own with status 1")
        lines = text.splitlines()
        at = next(n for n, line in enumerate(lines) if line.startswith("- Driver process: exited on its own with status 1."))
        self.assertEqual(lines[at + 1:at + 3], [
            "  - The driver's CLI ended by itself and not well. What it wrote was in the terminal the shift was "
            "started in, where the CLI left it there; the host does not read or keep what a driver writes.",
            "  - " + cli_words.knobs("driver", self.value["seats"]["driver"])])

    def test_a_driver_that_ended_well_or_was_stopped_gets_none_of_it(self):
        for said in ("exited on its own with status 0", "stopped by the host"):
            with self.subTest(said=said):
                self.setUp()
                text = self.ended(said)
                self.assertIn("- Driver process: " + said + ".", text)
                self.assertNotIn("What a person can change", text)
                self.assertNotIn("ended by itself", text)


if __name__ == "__main__":
    unittest.main()
