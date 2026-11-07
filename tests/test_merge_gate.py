"""merge_gate.py: the tiers, who may approve and when, the check's verdict per commit,
and the caller workflow. Offline: a fake REST API stands in for GitHub.
"""
import io, json, os, sys, tempfile, unittest, urllib.error
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import merge_gate  # noqa: E402

POLICY = merge_gate.load_policy()
REPO = "dfakkeldy/example"
SHA = "abc1234567890"
PUSHED = "2026-11-07T09:00:00Z"  # when the head commit's first check suite appeared


def files(*paths, removed=(), renamed=None):
    out = [{"path": p, "previous": None, "status": "modified"} for p in paths]
    out += [{"path": p, "previous": None, "status": "removed"} for p in removed]
    if renamed:
        out.append({"path": renamed[1], "previous": renamed[0], "status": "renamed"})
    return out


def tier(paths=(), base="nightly", labels=(), nightly=True, repo=REPO, **kw):
    return merge_gate.classify(repo, base, list(labels), files(*paths, **kw), nightly, POLICY)


class Globs(unittest.TestCase):
    def test_double_star_crosses_folders_single_star_does_not(self):
        self.assertTrue(merge_gate.glob_re("**/*.md").match("README.md"))
        self.assertTrue(merge_gate.glob_re("**/*.md").match("docs/a/b.md"))
        self.assertTrue(merge_gate.glob_re(".github/workflows/**").match(".github/workflows/ci.yml"))
        self.assertFalse(merge_gate.glob_re("docs/*.md").match("docs/a/b.md"))
        self.assertFalse(merge_gate.glob_re("**/*.md").match("notes.md.bak"))


class Tiers(unittest.TestCase):
    def test_stop_list(self):
        for path in (".github/workflows/ci.yml", "ios/fastlane/Fastfile", "App/App.entitlements",
                     ".env.production", "keys/AuthKey.p8", ".claude/settings.json",
                     ".claude/commands/ship.md", ".codex/hooks.json", ".github/copilot-instructions.md",
                     "fastlane/screenshots/en-US/1.png", ".claude", "app/.cursor"):
            with self.subTest(path):
                self.assertEqual(tier([path, "src/app.ts"])[0], "dan")

    def test_rename_out_of_the_stop_list_still_counts(self):
        self.assertEqual(tier(renamed=(".github/workflows/ci.yml", "old/ci.yml"))[0], "dan")

    def test_promotion_past_nightly_waits_but_main_without_a_ladder_does_not(self):
        self.assertEqual(tier(["src/a.py"], base="main", nightly=True)[0], "dan")
        self.assertEqual(tier(["src/a.py"], base="weekly", nightly=True)[0], "dan")
        self.assertEqual(tier(["src/a.py"], base="main", nightly=False)[0], "normal")

    def test_label_big_deletions_and_huge_prs(self):
        self.assertEqual(tier(["README.md"], labels=["needs-dan"])[0], "dan")
        gone = [f"old/{i}.swift" for i in range(26)]
        self.assertEqual(tier(["src/a.py"], removed=gone)[0], "dan")
        self.assertEqual(tier(["src/a.py"], removed=gone[:25])[0], "normal")
        self.assertEqual(tier([f"docs/{i}.md" for i in range(3000)])[0], "dan")

    def test_prose_and_images_are_low_but_agent_instructions_are_not(self):
        self.assertEqual(tier(["README.md", "docs/plan.md", "art/shot.png"])[0], "low")
        for path in ("CLAUDE.md", "skills/x/SKILL.md", "docs/index.html", "requirements-dev.txt"):
            with self.subTest(path):
                self.assertEqual(tier(["README.md", path])[0], "normal")

    def test_everything_in_the_gate_repo_waits_for_dan(self):
        for path in ("merge_gate.py", "argparse.py", "json.pyc", "tests/test_merge_gate.py", "README.md"):
            with self.subTest(path):
                self.assertEqual(tier([path], repo="dfakkeldy/merge-gate", base="main", nightly=False)[0], "dan")

    def test_repo_names_match_in_any_case(self):
        self.assertEqual(tier(["README.md"], repo="DFakkeldy/Merge-Gate", base="main", nightly=False)[0], "dan")


class LocalPolicy(unittest.TestCase):
    def local(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_a_local_file_adds_repos_and_list_items(self):
        path = self.local({"repos": {"dfakkeldy/private": {"dan": ["secret/**"], "no_ci_ok": "why"},
                                     "dfakkeldy/merge-gate": {"dan": ["README.md"]}}})
        policy = merge_gate.load_policy(local=path)
        self.assertEqual(policy["repos"]["dfakkeldy/private"]["dan"], ["secret/**"])
        self.assertEqual(policy["repos"]["dfakkeldy/merge-gate"]["dan"], ["**", "README.md"])
        self.assertEqual(merge_gate.load_policy()["repos"].get("dfakkeldy/private"), None)

    def test_a_local_file_cannot_loosen_anything(self):
        for data in ({"dan": {"paths": []}}, {"low": {}},
                     {"repos": {"dfakkeldy/merge-gate": {"note": "replaced"}}},
                     {"repos": {"dfakkeldy/merge-gate": {"dan": "nothing"}}}):
            with self.subTest(data), self.assertRaises(ValueError):
                merge_gate.load_policy(local=self.local(data))

    def test_a_missing_local_file_is_ignored(self):
        self.assertEqual(merge_gate.load_policy(local="/nonexistent/x.json"), POLICY)

    def test_another_spelling_of_a_repo_is_the_same_repo(self):
        with self.assertRaises(ValueError):
            merge_gate.load_policy(local=self.local({"repos": {"DFakkeldy/Merge-Gate": {"note": "x"}}}))
        policy = merge_gate.load_policy(local=self.local({"repos": {"dfakkeldy/Merge-Gate": {"no_ci_ok": "x"}}}))
        self.assertEqual(merge_gate.classify("dfakkeldy/merge-gate", "main", [], files("merge_gate.py"), False,
                                             policy)[0], "dan")


def labelled(name, login, at="2026-11-07T10:00:00Z", event="labeled", eid=1):
    return {"id": eid, "event": event, "label": {"name": name}, "actor": {"login": login}, "created_at": at}


class Approval(unittest.TestCase):
    def approved(self, labels, events, since=PUSHED):
        return merge_gate.dan_approved(REPO, list(labels), events, POLICY, since)[0]

    def test_only_the_owners_own_label_counts(self):
        self.assertTrue(self.approved(["dan-approved"], [labelled("dan-approved", "dfakkeldy")]))
        self.assertFalse(self.approved(["dan-approved"], [labelled("dan-approved", "dfakkeldy-claude[bot]")]))
        self.assertFalse(self.approved(["dan-approved"], []))
        self.assertFalse(self.approved([], [labelled("dan-approved", "dfakkeldy")]))

    def test_the_latest_labelling_decides(self):
        events = [labelled("dan-approved", "dfakkeldy-claude[bot]", "2026-11-07T12:00:00Z", eid=3),
                  labelled("dan-approved", "dfakkeldy", "2026-11-07T10:00:00Z", eid=1),
                  labelled("dan-approved", "dfakkeldy", "2026-11-07T11:00:00Z", "unlabeled", eid=2),
                  labelled("needs-dan", "dfakkeldy", "2026-11-07T13:00:00Z", eid=4)]
        self.assertFalse(self.approved(["dan-approved"], events))
        events.append(labelled("dan-approved", "DFakkeldy", "2026-11-07T14:00:00Z", eid=5))
        self.assertTrue(self.approved(["dan-approved"], events))

    def test_approval_covers_only_commits_pushed_before_it(self):
        dan = [labelled("dan-approved", "dfakkeldy", "2026-11-07T10:00:00Z")]
        self.assertTrue(self.approved(["dan-approved"], dan, since="2026-11-07T09:59:59Z"))
        self.assertFalse(self.approved(["dan-approved"], dan, since="2026-11-07T10:00:01Z"))
        self.assertFalse(self.approved(["dan-approved"], dan, since=None))
        why = merge_gate.dan_approved(REPO, ["dan-approved"], dan, POLICY, "2026-11-07T11:00:00Z")[1]
        self.assertIn("before the latest commit", why)

    def test_needs_dan_holds_after_it_is_removed(self):
        events = [labelled("needs-dan", "dfakkeldy-claude[bot]"),
                  labelled("needs-dan", "dfakkeldy-claude[bot]", "2026-11-07T11:00:00Z", "unlabeled")]
        self.assertEqual(merge_gate.effective_labels([], events, POLICY), ["needs-dan"])
        self.assertEqual(merge_gate.effective_labels(["x"], [], POLICY), ["x"])

    def test_a_force_push_or_new_base_after_approval_needs_a_new_one(self):
        events = [labelled("dan-approved", "dfakkeldy", "2026-11-07T10:00:00Z")]
        self.assertEqual(merge_gate.shaped_since(PUSHED, events), PUSHED)
        for kind in ("head_ref_force_pushed", "base_ref_changed", "automatic_base_change_succeeded"):
            with self.subTest(kind):
                later = events + [{"event": kind, "created_at": "2026-11-07T10:05:00Z"}]
                since = merge_gate.shaped_since(PUSHED, later)
                self.assertEqual(since, "2026-11-07T10:05:00Z")
                self.assertFalse(self.approved(["dan-approved"], later, since=since))
        self.assertIsNone(merge_gate.shaped_since(None, [{"event": "base_ref_changed", "created_at": PUSHED}]))

    def test_first_seen_is_the_earliest_check_suite(self):
        suites = [{"created_at": "2026-11-07T10:00:00Z"}, {"created_at": "2026-11-07T09:00:00Z"}, {}]
        self.assertEqual(merge_gate.first_seen(suites), "2026-11-07T09:00:00Z")
        self.assertIsNone(merge_gate.first_seen([]))


def pr(number=7, base="nightly", labels=(), sha=SHA, user=None):
    return {"number": number, "base": {"ref": base}, "head": {"ref": f"claude/{number}", "sha": sha},
            "labels": [{"name": n} for n in labels], "user": user or {"login": "dfakkeldy", "type": "User"}}


class FakeAPI:
    """Answers the gate's REST calls and records each path. prs: open PRs; files and
    events are per PR number."""

    def __init__(self, prs=None, files=None, events=None, nightly=True, suites=(PUSHED,), fail=None):
        self.prs = list(prs) if prs is not None else [pr()]
        self.files = files or {}
        self.events = events or {}
        self.nightly, self.suites, self.fail, self.calls = nightly, list(suites), fail, []

    def __call__(self, path, paginate=False):
        self.calls.append(path)
        if self.fail and self.fail in path:
            raise urllib.error.HTTPError(path, 502, "Bad Gateway", {}, None)
        if "/pulls?state=open" in path:
            return [dict(p) for p in self.prs]
        if "/pulls/" in path and "/files" in path:
            number = int(path.split("/pulls/")[1].split("/")[0])
            return [{"filename": f, "status": "modified"} for f in self.files.get(number, ["src/app.py"])]
        if path.endswith("/pulls/7"):
            return dict(self.prs[0])
        if path.endswith("/branches/nightly"):
            if self.nightly:
                return {"name": "nightly"}
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if "/check-suites" in path:
            return {"check_suites": [{"created_at": t} for t in self.suites]}
        if "/issues/" in path and "/events" in path:
            return list(self.events.get(int(path.split("/issues/")[1].split("/")[0]), []))
        raise AssertionError(path)


def run_main(fake, argv=("--repo", REPO, "--pr", "7")):
    out = io.StringIO()
    with redirect_stdout(out), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
        code = merge_gate.main(list(argv), get=fake)
    return code, out.getvalue()


WORKFLOW = {7: [".github/workflows/ci.yml"]}


class Gate(unittest.TestCase):
    def test_ordinary_prs_pass(self):
        self.assertEqual(run_main(FakeAPI())[0], 0)
        self.assertEqual(run_main(FakeAPI(files={7: ["README.md"]}))[0], 0)

    def test_stop_list_fails_until_the_owner_approves_after_the_latest_commit(self):
        self.assertEqual(run_main(FakeAPI(files=WORKFLOW))[0], 1)
        bot = FakeAPI(prs=[pr(labels=["dan-approved"])], files=WORKFLOW,
                      events={7: [labelled("dan-approved", "dfakkeldy-claude[bot]")]})
        code, out = run_main(bot)
        self.assertEqual(code, 1)
        self.assertIn("doesn't count", out)
        dan = FakeAPI(prs=[pr(labels=["dan-approved", "needs-dan"])], files=WORKFLOW,
                      events={7: [labelled("dan-approved", "dfakkeldy")]})
        code, out = run_main(dan)
        self.assertEqual(code, 0)
        self.assertIn("approved: dfakkeldy added dan-approved", out)
        pushed_later = FakeAPI(prs=[pr(labels=["dan-approved"])], files=WORKFLOW,
                               events={7: [labelled("dan-approved", "dfakkeldy")]}, suites=["2026-11-07T10:30:00Z"])
        code, out = run_main(pushed_later)
        self.assertEqual(code, 1)
        self.assertIn("before the latest commit", out)
        retargeted = FakeAPI(prs=[pr(base="main", labels=["dan-approved"])], files=WORKFLOW,
                             events={7: [labelled("dan-approved", "dfakkeldy"),
                                         {"event": "base_ref_changed", "created_at": "2026-11-07T10:10:00Z"}]})
        self.assertEqual(run_main(retargeted)[0], 1)

    def test_needs_dan_promotions_and_a_removed_hold_fail(self):
        self.assertEqual(run_main(FakeAPI(prs=[pr(labels=["needs-dan"])]))[0], 1)
        self.assertEqual(run_main(FakeAPI(prs=[pr(base="main")], nightly=True))[0], 1)
        self.assertEqual(run_main(FakeAPI(prs=[pr(base="main")], nightly=False))[0], 0)
        removed = FakeAPI(events={7: [labelled("needs-dan", "dfakkeldy"),
                                      labelled("needs-dan", "dfakkeldy", "2026-11-07T11:00:00Z", "unlabeled")]})
        code, out = run_main(removed)
        self.assertEqual(code, 1)
        self.assertIn("holds until Dan approves", out)

    def test_every_open_pr_on_the_commit_must_pass(self):
        # One head commit, two PRs: back to nightly (normal) and on to main (a promotion).
        shared = [pr(7, "nightly"), pr(8, "main"), pr(9, "nightly", sha="other")]
        code, out = run_main(FakeAPI(prs=shared))
        self.assertEqual(code, 1)
        self.assertIn("#8 into main: waits for Dan", out)
        self.assertNotIn("#9", out)
        approved = [pr(7, "nightly"), pr(8, "main", labels=["dan-approved"])]
        fake = FakeAPI(prs=approved, events={8: [labelled("dan-approved", "dfakkeldy")]})
        self.assertEqual(run_main(fake)[0], 0)

    def test_a_commit_that_no_open_pr_heads_fails(self):
        fake = FakeAPI(prs=[pr(sha="newer")])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as event:
            json.dump({"pull_request": {"number": 7, "head": {"sha": SHA}}}, event)
        self.addCleanup(os.unlink, event.name)
        env = {"GITHUB_EVENT_PATH": event.name, "GITHUB_REPOSITORY": REPO, "GITHUB_STEP_SUMMARY": ""}
        out, waits = io.StringIO(), []
        with redirect_stdout(out), mock.patch.dict(os.environ, env), \
                mock.patch.object(merge_gate.time, "sleep", waits.append):
            self.assertEqual(merge_gate.main([], get=fake), 1)
        self.assertIn("no open PR has", out.getvalue())
        self.assertEqual(waits, [5, 5])  # asked three times before failing

    def test_the_commit_comes_from_the_event(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as event:
            json.dump({"pull_request": {"number": 7, "head": {"sha": SHA}}}, event)
        self.addCleanup(os.unlink, event.name)
        env = {"GITHUB_EVENT_PATH": event.name, "GITHUB_REPOSITORY": REPO, "GITHUB_STEP_SUMMARY": ""}
        with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, env):
            self.assertEqual(merge_gate.main([], get=FakeAPI()), 0)

    def test_any_error_fails_the_check(self):
        for where in ("/pulls?state=open", "/files", "/branches/nightly", "/check-suites", "/events"):
            with self.subTest(where):
                code, out = run_main(FakeAPI(fail=where))
                self.assertEqual(code, 1)
                self.assertIn("could not judge", out)
        ghost = FakeAPI(prs=[dict(pr(), user=None)])  # a deleted account
        self.assertEqual(run_main(ghost)[0], 0)
        broken = FakeAPI(prs=[{"number": 7, "head": {"sha": SHA}}])  # a malformed answer
        self.assertEqual(run_main(broken)[0], 1)

    def test_a_workflow_opened_pr_counts_as_the_repos_automation(self):
        self.assertEqual(merge_gate.author_of({"login": "github-actions[bot]", "type": "Bot"}), "app/github-actions")
        self.assertEqual(merge_gate.author_of({"login": "dfakkeldy", "type": "User"}), "dfakkeldy")
        self.assertEqual(merge_gate.author_of(None), "")

    def test_a_file_name_cannot_start_a_line_in_the_log(self):
        code, out = run_main(FakeAPI(files={7: [".github/workflows/x\n::warning::forged.yml"]}))
        self.assertEqual(code, 1)
        self.assertFalse([line for line in out.splitlines() if line.startswith("::warning")])

    def test_the_summary_says_how_to_approve(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md") as summary:
            with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summary.name}):
                merge_gate.main(["--repo", REPO, "--pr", "7"], get=FakeAPI(files={7: ["App/App.entitlements"]}))
            text = summary.read()
        self.assertIn("waits for Dan", text)
        self.assertIn("`dan-approved`", text)


class CallerWorkflow(unittest.TestCase):
    """The workflow every repository copies, and the action it runs. These rules keep
    PR code out of the gate."""

    def setUp(self):
        with open(os.path.join(ROOT, ".github", "workflows", "merge-gate.yml")) as f:
            self.lines = [line for line in f.read().splitlines() if not line.lstrip().startswith("#")]
        self.text = "\n".join(self.lines)

    def test_it_runs_from_the_base_branch_on_every_pr_change(self):
        self.assertIn("pull_request_target:", self.text)
        self.assertNotIn("pull_request:", self.text.replace("pull_request_target:", ""))
        for kind in ("opened", "synchronize", "reopened", "edited", "labeled", "unlabeled", "ready_for_review"):
            self.assertIn(kind, self.text)

    def test_it_never_checks_out_skips_or_writes(self):
        self.assertNotIn("checkout", self.text)
        self.assertFalse([line for line in self.lines if line.strip().startswith("if:")])
        self.assertNotIn("write", self.text)

    def test_runs_for_one_commit_share_a_queue(self):
        self.assertIn("group: merge-gate-${{ github.event.pull_request.head.sha }}", self.text)

    def test_the_check_is_named_merge_gate_and_uses_this_action(self):
        self.assertIn("    name: Merge gate", self.text)
        self.assertIn("uses: dfakkeldy/merge-gate@main", self.text)

    def test_the_action_runs_python_isolated(self):
        with open(os.path.join(ROOT, "action.yml")) as f:
            action = f.read()
        self.assertIn('python3 -I "$GITHUB_ACTION_PATH/merge_gate.py"', action)


if __name__ == "__main__":
    unittest.main()
