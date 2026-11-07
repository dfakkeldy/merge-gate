"""merge_gate.py: the tiers, who may approve, the check's verdict, and the caller workflow.

Offline: a fake REST API stands in for GitHub.
"""
import io, json, os, sys, tempfile, unittest, urllib.error
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import merge_gate  # noqa: E402

POLICY = merge_gate.load_policy()
REPO = "dfakkeldy/example"


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
                     "fastlane/screenshots/en-US/1.png"):
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

    def test_the_gate_guards_its_own_rules(self):
        for path in ("merge_gate.py", "merge_policy.json", "action.yml"):
            with self.subTest(path):
                self.assertEqual(tier([path], repo="dfakkeldy/merge-gate", base="main", nightly=False)[0], "dan")
        self.assertEqual(tier(["tests/test_merge_gate.py"], repo="dfakkeldy/merge-gate", base="main",
                              nightly=False)[0], "normal")


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
        self.assertIn("README.md", policy["repos"]["dfakkeldy/merge-gate"]["dan"])
        self.assertIn("merge_gate.py", policy["repos"]["dfakkeldy/merge-gate"]["dan"])
        self.assertEqual(merge_gate.load_policy()["repos"].get("dfakkeldy/private"), None)

    def test_a_local_file_cannot_loosen_anything(self):
        for data in ({"dan": {"paths": []}}, {"low": {}},
                     {"repos": {"dfakkeldy/merge-gate": {"note": "replaced"}}},
                     {"repos": {"dfakkeldy/merge-gate": {"dan": "nothing"}}}):
            with self.subTest(data), self.assertRaises(ValueError):
                merge_gate.load_policy(local=self.local(data))

    def test_a_missing_local_file_is_ignored(self):
        self.assertEqual(merge_gate.load_policy(local="/nonexistent/x.json"), POLICY)


def labelled(name, login, at="2026-11-07T10:00:00Z", event="labeled", eid=1):
    return {"id": eid, "event": event, "label": {"name": name}, "actor": {"login": login}, "created_at": at}


class Approval(unittest.TestCase):
    def test_only_the_owners_own_label_counts(self):
        self.assertEqual(merge_gate.dan_approved(REPO, ["dan-approved"], [labelled("dan-approved", "dfakkeldy")],
                                                 POLICY), (True, "dfakkeldy"))
        self.assertEqual(merge_gate.dan_approved(REPO, ["dan-approved"],
                                                 [labelled("dan-approved", "dfakkeldy-claude[bot]")], POLICY),
                         (False, "dfakkeldy-claude[bot]"))
        self.assertEqual(merge_gate.dan_approved(REPO, ["dan-approved"], [], POLICY), (False, None))
        self.assertEqual(merge_gate.dan_approved(REPO, [], [labelled("dan-approved", "dfakkeldy")], POLICY),
                         (False, None))

    def test_the_latest_labelling_decides(self):
        events = [labelled("dan-approved", "dfakkeldy-claude[bot]", "2026-11-07T12:00:00Z", eid=3),
                  labelled("dan-approved", "dfakkeldy", "2026-11-07T10:00:00Z", eid=1),
                  labelled("dan-approved", "dfakkeldy", "2026-11-07T11:00:00Z", "unlabeled", eid=2),
                  labelled("needs-dan", "dfakkeldy", "2026-11-07T13:00:00Z", eid=4)]
        self.assertEqual(merge_gate.dan_approved(REPO, ["dan-approved"], events, POLICY)[0], False)
        events.append(labelled("dan-approved", "DFakkeldy", "2026-11-07T14:00:00Z", eid=5))
        self.assertEqual(merge_gate.dan_approved(REPO, ["dan-approved"], events, POLICY)[0], True)


class FakeAPI:
    """Answers the gate's REST calls for PR 7 and records each path."""

    def __init__(self, paths=("src/app.py",), base="nightly", labels=(), events=(), nightly=True,
                 user=None, fail=None):
        self.pr = {"number": 7, "base": {"ref": base}, "head": {"ref": "claude/x"},
                   "labels": [{"name": n} for n in labels], "user": user or {"login": "dfakkeldy", "type": "User"}}
        self.files = [{"filename": p, "status": "modified"} for p in paths]
        self.events, self.nightly, self.fail, self.calls = list(events), nightly, fail, []

    def __call__(self, path, paginate=False):
        self.calls.append(path)
        if self.fail and self.fail in path:
            raise urllib.error.HTTPError(path, 502, "Bad Gateway", {}, None)
        if path.endswith("/pulls/7"):
            return dict(self.pr)
        if "/pulls/7/files" in path:
            return list(self.files)
        if path.endswith("/branches/nightly"):
            if self.nightly:
                return {"name": "nightly"}
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if "/issues/7/events" in path:
            return list(self.events)
        raise AssertionError(path)


def run_main(fake):
    out = io.StringIO()
    with redirect_stdout(out), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
        code = merge_gate.main(["--repo", REPO, "--pr", "7"], get=fake)
    return code, out.getvalue()


class Gate(unittest.TestCase):
    def test_ordinary_prs_pass_without_reading_label_history(self):
        fake = FakeAPI()
        self.assertEqual(merge_gate.gate(REPO, 7, POLICY, fake)[:2], (True, "normal"))
        self.assertFalse(any("/events" in c for c in fake.calls))

    def test_stop_list_fails_until_the_owner_approves(self):
        self.assertEqual(run_main(FakeAPI(paths=(".github/workflows/ci.yml",)))[0], 1)
        bot = FakeAPI(paths=(".github/workflows/ci.yml",), labels=("dan-approved",),
                      events=[labelled("dan-approved", "dfakkeldy-claude[bot]")])
        code, out = run_main(bot)
        self.assertEqual(code, 1)
        self.assertIn("doesn't count", out)
        dan = FakeAPI(paths=(".github/workflows/ci.yml",), labels=("dan-approved", "needs-dan"),
                      events=[labelled("dan-approved", "dfakkeldy")])
        code, out = run_main(dan)
        self.assertEqual(code, 0)
        self.assertIn("approved: dfakkeldy added dan-approved", out)

    def test_needs_dan_and_promotions_fail(self):
        self.assertEqual(run_main(FakeAPI(labels=("needs-dan",)))[0], 1)
        self.assertEqual(run_main(FakeAPI(base="main", nightly=True))[0], 1)
        self.assertEqual(run_main(FakeAPI(base="main", nightly=False))[0], 0)

    def test_any_error_fails_the_check(self):
        for where in ("/pulls/7/files", "/branches/nightly", "/pulls/7"):
            with self.subTest(where):
                code, out = run_main(FakeAPI(fail=where))
                self.assertEqual(code, 1)
                self.assertIn("could not judge", out)
        approved = FakeAPI(paths=(".github/workflows/ci.yml",), labels=("dan-approved",), fail="/events")
        self.assertEqual(run_main(approved)[0], 1)

    def test_a_workflow_opened_pr_counts_as_the_repos_automation(self):
        self.assertEqual(merge_gate.author_of({"login": "github-actions[bot]", "type": "Bot"}), "app/github-actions")
        self.assertEqual(merge_gate.author_of({"login": "dfakkeldy", "type": "User"}), "dfakkeldy")

    def test_the_summary_says_how_to_approve(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md") as summary:
            with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summary.name}):
                merge_gate.main(["--repo", REPO, "--pr", "7"], get=FakeAPI(paths=("App/App.entitlements",)))
            text = summary.read()
        self.assertIn("waits for Dan", text)
        self.assertIn("`dan-approved`", text)

    def test_the_pr_number_comes_from_the_event(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as event:
            json.dump({"pull_request": {"number": 7}}, event)
        self.addCleanup(os.unlink, event.name)
        env = {"GITHUB_EVENT_PATH": event.name, "GITHUB_REPOSITORY": REPO, "GITHUB_STEP_SUMMARY": ""}
        with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, env):
            self.assertEqual(merge_gate.main([], get=FakeAPI()), 0)


class CallerWorkflow(unittest.TestCase):
    """The workflow every repository copies. These rules keep PR code out of the gate."""

    def setUp(self):
        with open(os.path.join(ROOT, ".github", "workflows", "merge-gate.yml")) as f:
            self.lines = [line for line in f.read().splitlines() if not line.lstrip().startswith("#")]
        self.text = "\n".join(self.lines)

    def test_it_runs_from_the_base_branch_on_every_pr_change(self):
        self.assertIn("pull_request_target:", self.text)
        self.assertNotIn("pull_request:", self.text.replace("pull_request_target:", ""))
        for kind in ("opened", "synchronize", "reopened", "edited", "labeled", "unlabeled", "ready_for_review"):
            self.assertIn(kind, self.text)

    def test_it_never_checks_out_or_skips(self):
        self.assertNotIn("checkout", self.text)
        self.assertFalse([line for line in self.lines if line.strip().startswith("if:")])
        self.assertNotIn("write", self.text)

    def test_the_check_is_named_merge_gate_and_uses_this_action(self):
        self.assertIn("    name: Merge gate", self.text)
        self.assertIn("uses: dfakkeldy/merge-gate@main", self.text)


if __name__ == "__main__":
    unittest.main()
