"""The host's own look at the repository for a review of a candidate: the branch, the
commits of the task, the remotes, the remote-tracking refs and what is not committed."""
import os
import subprocess
from unittest.mock import patch

from sortie_kit import repo_state
from support import LocalTest


class CandidateLookTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.own_home()  # no remote of the person running the tests is anywhere near
        self.target = self.repo()
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        (self.target / ".git/info/exclude").write_text("/.sortie/\n")
        (self.target / "source.txt").write_text("before\n")
        self.base = self.commit()
        (self.target / "source.txt").write_text("after\n")
        self.candidate = self.commit()

    def git(self, *args, repo=None):
        return subprocess.run(["git", "-C", str(repo or self.target), "-c", "user.name=Fixture",
                               "-c", "user.email=fixture@example.invalid", *args],
                              check=True, capture_output=True, timeout=10).stdout.decode()

    def commit(self, message="fixture"):
        self.git("add", ".")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def look(self, base="base", commit=None, seconds=10, **given):
        return repo_state.candidate_look(self.target, self.base if base == "base" else base,
                                         commit or self.candidate, seconds, **given)

    def test_one_commit_on_main_with_no_remote_and_nothing_left(self):
        seen = self.look()
        self.assertRegex(seen["at"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assertEqual(seen["branch"], dict(name="main", ref="refs/heads/main", head=self.candidate,
                                              at_candidate=True, holds_candidate=None))
        self.assertEqual(seen["commits"], dict(based=True, count=1, more=False,
                                               rows=[dict(id=self.candidate, merge=False)]))
        self.assertEqual(seen["remotes"], dict(rows=[], removed=[], more=0, compared=False,
                                               unlisted=dict(added=0, changed=0, unsure=0)))
        self.assertEqual(seen["tracking"], dict(any=False, on=[], more=False))
        self.assertEqual(seen["uncommitted"], dict(untracked=[], count=0, more=False, staged=0, compared=False))

    def test_the_commits_of_a_task_are_counted_and_the_newest_named(self):
        ids = [self.candidate]
        for n in range(24):
            ids.append(self.commit("more %d" % n))
        seen = self.look(commit=ids[-1])["commits"]
        self.assertEqual((seen["based"], seen["count"], seen["more"]), (True, 25, False))
        self.assertEqual([row["id"] for row in seen["rows"]], ids[:4:-1])  # the 20 newest, newest first
        # A task begun in a repository without a commit: everything the candidate reaches.
        seen = self.look(base=None, commit=self.candidate)["commits"]
        self.assertEqual((seen["based"], seen["count"]), (None, 2))
        self.assertEqual([row["id"] for row in seen["rows"]], [self.candidate, self.base])

    def test_more_commits_than_are_counted_are_said_to_be_more(self):
        with patch.object(repo_state, "COMMITS", 3):
            for n in range(4):
                last = self.commit("more %d" % n)
            seen = self.look(commit=last)["commits"]
        self.assertEqual((seen["count"], seen["more"], len(seen["rows"])), (3, True, 3))

    def test_a_merge_and_a_base_that_is_no_ancestor_are_said(self):
        self.git("checkout", "-q", "-b", "side", self.base)
        (self.target / "side.txt").write_text("side\n")
        side = self.commit("side")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merged", "side")
        merged = self.git("rev-parse", "HEAD").strip()
        seen = self.look(commit=merged)["commits"]
        self.assertEqual((seen["based"], seen["count"]), (True, 3))
        self.assertEqual({row["id"]: row["merge"] for row in seen["rows"]},
                         {merged: True, side: False, self.candidate: False})
        # An amended history: the commit the task began at is not under the candidate.
        seen = self.look(base=self.candidate, commit=side)["commits"]
        self.assertEqual((seen["based"], seen["count"], [row["id"] for row in seen["rows"]]), (False, 1, [side]))

    def test_a_detached_head_and_a_head_that_is_not_the_candidate(self):
        self.git("checkout", "-q", "--detach", self.candidate)
        seen = self.look()["branch"]
        self.assertEqual((seen["name"], seen["ref"], seen["at_candidate"]), (None, None, True))
        self.git("checkout", "-q", "-b", "other", self.base)
        seen = self.look()["branch"]
        self.assertEqual((seen["name"], seen["head"], seen["at_candidate"], seen["holds_candidate"]),
                         ("other", self.base, False, False))
        self.git("checkout", "-q", "main")
        later = self.commit("later")
        seen = self.look()["branch"]
        self.assertEqual((seen["head"], seen["at_candidate"], seen["holds_candidate"]), (later, False, True))

    def test_remotes_are_named_and_compared_with_the_look_at_the_start(self):
        self.git("remote", "add", "kept", "https://example.invalid/kept.git")
        self.git("remote", "add", "moved", "https://example.invalid/moved.git")
        self.git("remote", "add", "gone", "https://example.invalid/gone.git")
        self.git("remote", "add", "pushes", "https://example.invalid/pushes.git")
        start = repo_state.observe(self.target, 5)
        self.assertEqual([row["name"] for row in start["remotes"]], ["gone", "kept", "moved", "pushes"])
        self.assertTrue(all(len(row["sha256"]) == 64 for row in start["remotes"]))
        self.assertNotIn("example.invalid", str(start))  # no URL is kept in a look
        self.assertNotIn("remotes_truncated", start)
        self.git("remote", "remove", "gone")
        self.git("remote", "set-url", "moved", "https://example.invalid/elsewhere.git")
        # The same URL under the other key is another configuration.
        self.git("config", "--unset", "remote.pushes.url")
        self.git("config", "remote.pushes.pushurl", "https://example.invalid/pushes.git")
        self.git("remote", "add", "new.one", "ssh://example.invalid/new.git")
        self.git("config", "--add", "remote.new.one.url", "https://second.example.invalid/new.git")
        seen = self.look(start=start)["remotes"]
        self.assertEqual((seen["removed"], seen["more"], seen["compared"]), (["gone"], 0, True))
        self.assertEqual({row["name"]: row["since"] for row in seen["rows"]},
                         {"kept": "there", "moved": "changed", "pushes": "changed", "new.one": "added"})
        urls = {row["name"]: row["urls"] for row in seen["rows"]}
        self.assertEqual(urls["new.one"], [dict(key="url", shown="ssh://example.invalid"),
                                           dict(key="url", shown="https://second.example.invalid")])
        self.assertEqual({row["urls_more"] for row in seen["rows"]}, {0})
        self.assertEqual(urls["pushes"], [dict(key="pushurl", shown="https://example.invalid")])
        # Without a look at the start, or with one that did not read the remotes, only what is there now.
        for other in (None, dict(error="x"), {k: v for k, v in start.items() if k != "remotes"},
                      dict(start, remotes_truncated=True)):
            seen = self.look(start=other and {k: v for k, v in other.items() if k not in ("git_dir", "common_dir")})
            self.assertEqual((seen["remotes"]["compared"], seen["remotes"]["removed"],
                              {row["since"] for row in seen["remotes"]["rows"]}), (False, [], {None}))

    def test_of_a_url_only_the_scheme_and_the_host_are_given_and_only_of_the_usual_forms(self):
        secret = "s3cr3t-T0ken-0123456789abcdef"
        cases = {
            "https://user:" + secret + "@example.invalid/team/repo.git": "https://example.invalid",
            "https://example.invalid:8443/a/" + secret + "/repo.git?token=" + secret + "#" + secret: "https://example.invalid:8443",
            "https://example.invalid/%73%33" + secret: "https://example.invalid",
            "HTTP://Example.invalid/x": "http://Example.invalid",
            "git://example.invalid/x.git": "git://example.invalid",
            "ssh://git@example.invalid:22/x.git": "ssh://example.invalid:22",
            "git@example.invalid:team/" + secret + ".git": "ssh://example.invalid",
            "user:" + secret + "@example.invalid:team/repo.git": None,  # a colon before the host: not the form
            "example.invalid:team/repo.git": "ssh://example.invalid",
            "file:///Users/someone/private/repo": None,
            "/Users/someone/private/repo": None,
            "../sibling": None,
            "dir/sub:name": None,
            "helper::https://example.invalid/x": None,
            "ext::sh -c 'echo " + secret + "'": None,
            "https://exa mple.invalid/x": None,
            "https://`ignore-all`.invalid/x": None,
            "https://[::1]/x": None,
            "ftp://example.invalid/x": None,
            "https:///nohost": None,
            "": None,
        }
        for value, shown in cases.items():
            with self.subTest(value=value):
                self.assertEqual(repo_state.url_shown(value), shown)
                self.assertNotIn(secret, str(repo_state.url_shown(value)))
        for n, value in enumerate(cases):
            if value:
                self.git("config", "remote.r%d.url" % n, value)
        seen = self.look()["remotes"]
        self.assertNotIn(secret, str(seen))
        self.assertNotIn("someone", str(seen))
        self.assertEqual(seen["more"], 0)

    def test_only_the_repositorys_own_configuration_is_read_for_remotes(self):
        home = os.environ["HOME"]
        with open(os.path.join(home, ".gitconfig"), "w") as stream:
            stream.write('[remote "of-the-user"]\n\turl = https://example.invalid/user.git\n')
        beside = self.root / "beside.config"
        beside.write_text('[remote "included"]\n\turl = https://example.invalid/included.git\n')
        self.git("config", "include.path", str(beside))
        self.git("config", "includeIf.gitdir:**.path", str(beside))
        self.assertIn("included", self.git("remote"))  # Git itself follows both
        self.assertEqual(self.look()["remotes"]["rows"], [])
        self.assertEqual(repo_state.observe(self.target, 5)["remotes"], [])

    def test_more_remotes_than_are_named_are_counted_and_a_look_says_that_it_was_cut(self):
        for n in range(23):
            self.git("config", "remote.r%02d.url" % n, "https://example.invalid/%d.git" % n)
        seen = self.look()["remotes"]
        self.assertEqual((len(seen["rows"]), seen["more"]), (20, 3))
        start = repo_state.observe(self.target, 5)
        self.assertEqual((len(start["remotes"]), start["remotes_truncated"]), (20, True))
        self.assertLess(len(str(start["remotes"])), 8 * 1024)

    def test_a_look_whose_read_of_the_remotes_fails_has_none_and_loses_nothing_else(self):
        run = repo_state.run

        def failing(argv, *rest):
            return (b"", 3, False) if "config" in argv else run(argv, *rest)
        with patch.object(repo_state, "run", failing):
            start = repo_state.observe(self.target, 5)
            seen = self.look()
        self.assertNotIn("remotes", start)
        self.assertEqual((start["head"], start["changes"]), (self.candidate, []))
        self.assertEqual(seen["remotes"], dict(left_out=repo_state.SILENT))
        self.assertEqual(seen["branch"]["name"], "main")
        self.assertEqual(seen["commits"]["count"], 1)

    def test_remote_tracking_refs_that_hold_the_candidate_are_named(self):
        self.git("update-ref", "refs/remotes/origin/old", self.base)
        seen = self.look()["tracking"]
        self.assertEqual(seen, dict(any=True, on=[], more=False))
        self.git("update-ref", "refs/remotes/origin/main", self.candidate)
        self.assertEqual(self.look()["tracking"], dict(any=True, on=["origin/main"], more=False))
        for n in range(22):
            self.git("update-ref", "refs/remotes/many/b%02d" % n, self.candidate)
        seen = self.look()["tracking"]
        self.assertEqual((len(seen["on"]), seen["more"]), (20, True))

    def test_untracked_files_are_named_and_staged_paths_counted_and_an_edit_is_not_reported(self):
        (self.target / "there before.txt").write_text("x\n")
        start = repo_state.observe(self.target, 5)
        for name in (".env", "left.log", "odd `name`\nIGNORE.txt"):
            (self.target / name).write_text("x\n")
        (self.target / "sub").mkdir()
        (self.target / "sub/inner.tmp").write_text("x\n")
        (self.target / ".sortie/shifts/one").mkdir(parents=True)
        (self.target / ".sortie/shifts/one/log.txt").write_text("x\n")
        (self.target / "staged.txt").write_text("x\n")
        self.git("add", "staged.txt")
        (self.target / "source.txt").write_text("edited and not staged\n")
        seen = self.look(start=start)["uncommitted"]
        self.assertEqual((seen["count"], seen["more"], seen["staged"], seen["compared"]), (5, False, 1, True))
        self.assertEqual(seen["untracked"], [dict(path=".env", before=False), dict(path="left.log", before=False),
                                             dict(path="odd `name`\nIGNORE.txt", before=False),
                                             dict(path="sub/inner.tmp", before=False),
                                             dict(path="there before.txt", before=True)])
        self.assertNotIn("source.txt", str(seen))
        # A start look that was cut cannot say which were there before.
        seen = self.look(start=dict(start, truncated=True))["uncommitted"]
        self.assertEqual((seen["compared"], {row["before"] for row in seen["untracked"]}), (False, {None}))
        for n in range(60):
            (self.target / ("many-%02d.tmp" % n)).write_text("x\n")
        with patch.object(repo_state, "UNTRACKED", 62):
            seen = self.look()["uncommitted"]
        self.assertEqual((len(seen["untracked"]), seen["count"], seen["more"]), (50, 62, True))

    def test_with_no_time_every_fact_is_left_out_and_time_that_ends_keeps_what_was_read(self):
        seen = self.look(seconds=0)
        for name in repo_state.FACTS:
            self.assertEqual(seen[name], dict(left_out=repo_state.LATE), name)
        # The caller's own time, asked before each command, ends after the branch was read.
        asked = []

        def room():
            asked.append(1)
            return 5 if len(asked) <= 4 else 0
        seen = self.look(room=room)
        self.assertEqual(seen["branch"]["name"], "main")
        for name in ("commits", "remotes", "uncommitted", "tracking"):
            self.assertEqual(seen[name], dict(left_out=repo_state.LATE), name)

    def test_a_command_that_was_cut_or_did_not_answer_leaves_its_fact_out_and_never_reads_as_none(self):
        run = repo_state.run
        full = (b"a.txt\0" * repo_state.OUTPUT)[:repo_state.OUTPUT]  # as much as is read of one command
        for word, answer, why in (("ls-files", (full, None, True), repo_state.CUT),
                                  ("config", (full, None, True), repo_state.CUT),
                                  ("for-each-ref", (b"", None, True), repo_state.LATE),
                                  ("rev-list", (b"", 128, False), repo_state.SILENT),
                                  ("symbolic-ref", (b"", 128, False), repo_state.SILENT)):
            with self.subTest(word=word):
                # A command ended with less output than is read of one was ended by the time.
                with patch.object(repo_state, "run", lambda argv, *rest: answer if word in argv else run(argv, *rest)):
                    seen = self.look()
                out = [name for name in repo_state.FACTS if "left_out" in seen[name]]
                self.assertEqual(out, [dict([("ls-files", "uncommitted"), ("for-each-ref", "tracking"),
                                             ("rev-list", "commits"), ("symbolic-ref", "branch"),
                                             ("config", "remotes")])[word]])
                self.assertEqual(seen[out[0]], dict(left_out=why))

    def test_a_head_that_could_not_be_read_is_not_taken_for_a_repository_without_a_commit(self):
        """The paths staged are those that differ from HEAD, and with no commit all that the
        index holds. A HEAD that Git did not answer for is not the second case: every
        tracked file would be counted as staged."""
        run = repo_state.run

        def failing(argv, *rest):
            return (b"", 128, False) if "HEAD^{commit}" in argv else run(argv, *rest)
        with patch.object(repo_state, "run", failing):
            seen = self.look()
        self.assertEqual(seen["branch"], dict(left_out=repo_state.SILENT))
        self.assertEqual(seen["uncommitted"], dict(left_out=repo_state.SILENT))
        self.assertEqual(seen["commits"]["count"], 1)
        # A repository that has no commit: what the index holds is what is staged.
        bare = self.root / "unborn"
        subprocess.run(["git", "init", "-q", str(bare)], check=True, capture_output=True, timeout=10)
        (bare / "one.txt").write_text("x\n")
        self.git("add", "one.txt", repo=bare)
        seen = repo_state.candidate_look(bare, None, self.candidate, 10)
        self.assertEqual((seen["branch"]["head"], seen["uncommitted"]["staged"]), (None, 1))

    def test_once_the_time_has_ended_no_further_command_is_started(self):
        ran = []
        run = repo_state.run

        def slow(argv, *rest):
            ran.append(argv)
            return (b"", None, True) if "rev-list" in argv else run(argv, *rest)
        with patch.object(repo_state, "run", slow):
            seen = self.look()
        self.assertEqual(seen["branch"]["name"], "main")
        for name in ("commits", "remotes", "uncommitted", "tracking"):
            self.assertEqual(seen[name], dict(left_out=repo_state.LATE), name)
        self.assertIn("rev-list", ran[-1])

    def test_a_remote_added_is_said_whatever_its_name_and_however_many_there_are(self):
        """A name is kept in the start look cut at a length, and at most 20 remotes are listed.
        Neither lets a remote that was added pass as one that was there."""
        long = "p" * (repo_state.NAME_SHOWN - 1)
        self.git("config", "remote.short.url", "https://example.invalid/short.git")
        start = repo_state.observe(self.target, 5)
        # A name longer than the start look keeps, where the start look cut none: it was not there.
        self.git("config", "remote." + long + "xy.url", "https://example.invalid/short.git")
        seen = self.look(start=start)["remotes"]
        self.assertEqual((seen["compared"], {row["name"]: row["since"] for row in seen["rows"]}),
                         (True, {long + "xy": "added", "short": "there"}))
        # A start look that cut a name: of a long name with that beginning the host cannot say, of
        # another long name it can, and the short ones are compared as before.
        start = repo_state.observe(self.target, 5)
        self.assertEqual(max(len(row["name"]) for row in start["remotes"]), repo_state.NAME_SHOWN)
        self.git("config", "remote." + long + "xz.url", "https://example.invalid/short.git")
        self.git("config", "remote." + "q" * (repo_state.NAME_SHOWN + 5) + ".url", "https://example.invalid/q.git")
        self.git("config", "remote.new.url", "https://example.invalid/new.git")
        seen = self.look(start=start)["remotes"]
        self.assertEqual((seen["compared"], seen["removed"]), (True, []))
        self.assertEqual({row["name"]: row["since"] for row in seen["rows"]},
                         {long + "xy": None, long + "xz": None, "q" * (repo_state.NAME_SHOWN + 5): "added",
                          "new": "added", "short": "there"})
        # The cut name gone: nothing now begins as it did.
        self.git("config", "--unset", "remote." + long + "xy.url")
        self.git("config", "--unset", "remote." + long + "xz.url")
        self.assertEqual(self.look(start=start)["remotes"]["removed"], [long + "x"])
        # A start look whose rows are not what a look writes is not compared with.
        for rows in ([dict(name="short")], [dict(name="short", sha256="0" * 64)] * 2, [dict(name="short", sha256=7)]):
            seen = self.look(start=dict(start, remotes=rows))["remotes"]
            self.assertEqual((seen["compared"], {row["since"] for row in seen["rows"]}), (False, {None}), rows)

    def test_a_remote_that_sets_no_url_is_a_remote(self):
        """Git takes a section `remote "NAME"` for a remote whatever it sets: one with a
        fetch rule alone is listed by `git remote` and can be pushed to under its name. A
        look that found remotes by their URLs said of such a repository that it names none."""
        start = repo_state.observe(self.target, 5)
        self.assertEqual(start["remotes"], [])
        self.git("config", "remote.fetch-only.fetch", "+refs/heads/*:refs/remotes/fetch-only/*")
        self.git("config", "remote.other.tagOpt", "--no-tags")
        self.git("config", "remote.pushDefault", "fetch-only")  # a setting of all remotes, and no remote
        (self.target / ".git/config").open("a").write('[remote "bare"]\n\tprune\n')  # a key with no value
        self.assertEqual(set(self.git("config", "--get-regexp", "^remote[.]").split()) & {"remote.bare.prune"},
                         {"remote.bare.prune"})
        seen = self.look(start=start)["remotes"]
        self.assertEqual(seen["compared"], True)
        self.assertEqual(seen["rows"], [dict(name=name, urls=[], urls_more=0, since="added")
                                        for name in ("bare", "fetch-only", "other")])
        later = repo_state.observe(self.target, 5)
        self.assertEqual([row["name"] for row in later["remotes"]], ["bare", "fetch-only", "other"])
        # A URL given to it afterwards is a change, and taking the section away a removal.
        self.git("config", "remote.fetch-only.url", "https://example.invalid/x.git")
        self.git("config", "--remove-section", "remote.other")
        seen = self.look(start=later)["remotes"]
        self.assertEqual(({row["name"]: row["since"] for row in seen["rows"]}, seen["removed"]),
                         ({"bare": "there", "fetch-only": "changed"}, ["other"]))
        # The values of the other keys are nowhere in a look.
        self.assertNotIn("no-tags", str(seen) + str(later))
        self.assertNotIn("refs/heads", str(seen) + str(later))

    def test_a_remote_section_with_an_empty_name_is_a_remote_and_no_setting_of_all_remotes(self):
        """`[remote ""]` gives keys `remote..url`. Git takes that for a remote, lists it under
        the name `.url` and resolves that name to the URL. A look that took the empty name
        for a key of the section itself, as `remote.pushDefault` is one, said "no remote"."""
        self.git("config", "remote.pushDefault", "elsewhere")
        start = repo_state.observe(self.target, 5)
        self.assertEqual(start["remotes"], [])  # the setting of all remotes alone names none
        self.assertEqual(self.look(start=start)["remotes"]["rows"], [])
        with (self.target / ".git/config").open("a") as stream:
            stream.write('[remote ""]\n\turl = https://example.invalid/team/repo.git\n\tfetch = +refs/heads/*:refs/x/*\n')
        self.assertIn("remote..url", self.git("config", "--local", "--get-regexp", "^remote[.]"))
        seen = self.look(start=start)["remotes"]
        self.assertEqual(seen["compared"], True)
        self.assertEqual(seen["rows"], [
            dict(name=".fetch", urls=[], urls_more=0, since="added"),
            dict(name=".url", urls=[dict(key="url", shown="https://example.invalid")], urls_more=0, since="added")])
        later = repo_state.observe(self.target, 5)
        self.assertEqual([row["name"] for row in later["remotes"]], [".fetch", ".url"])
        self.assertEqual({row["since"] for row in self.look(start=later)["remotes"]["rows"]}, {"there"})
        # Whatever this Git lists as a remote and can resolve is in the look.
        native = subprocess.run(["git", "-C", str(self.target), "remote"], capture_output=True, timeout=10)
        if native.returncode == 0:
            listed = set(native.stdout.decode().split())
            self.assertLessEqual(listed, {row["name"] for row in seen["rows"]})
            for name in listed & {".url"}:
                self.assertEqual(self.git("remote", "get-url", name).strip(), "https://example.invalid/team/repo.git")

    def test_more_remotes_than_are_listed_are_still_compared_with_the_start(self):
        self.git("config", "remote.z-there.url", "https://example.invalid/there.git")
        self.git("config", "remote.z-moved.url", "https://example.invalid/moved.git")
        start = repo_state.observe(self.target, 5)
        self.git("config", "remote.z-moved.url", "https://example.invalid/elsewhere.git")
        for n in range(21):
            self.git("config", "remote.a%02d.url" % n, "https://example.invalid/%d.git" % n)
        seen = self.look(start=start)["remotes"]
        self.assertEqual((seen["compared"], len(seen["rows"]), seen["more"]), (True, 20, 3))
        self.assertEqual({row["since"] for row in seen["rows"]}, {"added"})
        self.assertEqual(seen["unlisted"], dict(added=1, changed=1, unsure=0))

    def test_of_one_remote_four_urls_of_each_kind_are_given_and_the_rest_counted(self):
        for n in range(6):
            self.git("config", "--add", "remote.many.url", "https://u%d.example.invalid/x.git" % n)
        self.git("config", "--add", "remote.many.pushurl", "https://push.example.invalid/x.git")
        row, = self.look()["remotes"]["rows"]
        self.assertEqual([url["shown"] for url in row["urls"]],
                         ["https://u%d.example.invalid" % n for n in range(4)] + ["https://push.example.invalid"])
        self.assertEqual(row["urls_more"], 2)

    def test_a_scheme_or_a_drive_before_one_colon_is_not_taken_for_a_host(self):
        for value in ("file:/Users/someone/private/repo", "http:example.invalid/x", "https:x", "ssh:x", "git:x",
                      "ftp:x", "FILE:x", "C:/Users/someone/repo", "C:\\Users\\someone\\repo", "c:repo"):
            with self.subTest(value=value):
                self.assertIsNone(repo_state.url_shown(value))
        self.assertEqual(repo_state.url_shown("files.example.invalid:team/repo.git"), "ssh://files.example.invalid")

    def test_an_untracked_path_longer_than_the_start_look_keeps_is_not_said_to_have_been_there(self):
        deep = self.target / ("d" * 200) / ("e" * 198)
        deep.mkdir(parents=True)
        (deep / "first.txt").write_text("x\n")
        (self.target / "short.txt").write_text("x\n")
        start = repo_state.observe(self.target, 5)
        self.assertEqual(max(len(row["path"]) for row in start["changes"]), repo_state.PATH_SHOWN)
        (deep / "second.txt").write_text("x\n")
        other = self.target / ("f" * 200) / ("g" * 200)
        other.mkdir(parents=True)
        (other / "third.txt").write_text("x\n")
        seen = self.look(start=start)["uncommitted"]
        self.assertEqual({row["path"].rpartition("/")[2]: row["before"] for row in seen["untracked"]},
                         {"first.txt": None, "second.txt": None, "third.txt": False, "short.txt": True})

    def test_an_id_that_is_no_commit_id_is_refused_before_git_runs(self):
        ran = []
        with patch.object(repo_state, "run", lambda *a: ran.append(a) or (b"", 0, False)):
            for base, commit in ((self.base, "--all"), ("HEAD", self.candidate), (self.base, self.candidate[:12]),
                                 (self.base, None), (None, None), (None, "--all")):
                with self.assertRaises(ValueError):
                    repo_state.candidate_look(self.target, base, commit, 5)
        self.assertEqual(ran, [])

    def test_nothing_the_repository_configures_is_run_and_a_replaced_object_is_not_followed(self):
        marker = self.root / "ran"
        hooks = self.root / "hooks"
        hooks.mkdir()
        tool = self.root / "tool.sh"
        tool.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        tool.chmod(0o755)
        for name in ("post-index-change", "reference-transaction", "pre-commit"):
            (hooks / name).write_text(tool.read_text())
            (hooks / name).chmod(0o755)
        # The candidate replaced by its parent: Git would then say that nothing came after the base.
        self.git("replace", self.candidate, self.base)
        (self.target / "left.txt").write_text("x\n")
        self.git("config", "core.hooksPath", str(hooks))
        self.git("config", "core.fsmonitor", str(tool))
        self.git("config", "core.pager", str(tool))
        self.git("config", "alias.rev-list", "!" + str(tool))
        self.assertFalse(marker.exists())
        seen = self.look()
        self.assertEqual(seen["commits"]["rows"], [dict(id=self.candidate, merge=False)])
        self.assertEqual([row["path"] for row in seen["uncommitted"]["untracked"]], ["left.txt"])
        self.assertFalse(marker.exists())

    def test_a_working_tree_or_git_directory_pointed_elsewhere_is_not_described(self):
        start = repo_state.observe(self.target, 5)
        other = self.root / "other"
        other.mkdir()
        (other / "elsewhere.txt").write_text("x\n")
        self.git("config", "core.worktree", str(other))
        seen = self.look(start=start)
        self.assertEqual(seen["uncommitted"]["untracked"], [])
        self.git("config", "--unset", "core.worktree")
        # The .git of the repository replaced by another repository's since the shift began.
        subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True, timeout=10)
        os.rename(self.target / ".git", self.root / "real.git")
        (self.target / ".git").write_text("gitdir: " + str(other / ".git") + "\n")
        seen = self.look(start=start)
        for name in repo_state.FACTS:
            self.assertEqual(seen[name], dict(left_out=repo_state.MOVED), name)

    def test_the_look_writes_nothing(self):
        (self.target / "left.txt").write_text("x\n")
        self.git("update-ref", "refs/remotes/origin/main", self.candidate)
        self.git("remote", "add", "origin", "https://example.invalid/x.git")

        def listing():
            found = {}
            for folder, _, files in os.walk(self.target):
                for name in files:
                    path = os.path.join(folder, name)
                    status = os.lstat(path)
                    found[path] = (status.st_size, status.st_mtime_ns)
            return found
        before = listing()
        seen = self.look(start=repo_state.observe(self.target, 5))
        self.assertEqual(seen["tracking"]["on"], ["origin/main"])
        self.assertEqual(listing(), before)
