"""Merge gate: Dan's merge stop list, enforced on GitHub's side.

GitHub's native auto-merge (gh pr merge --auto, the merge button, apps that arm it)
waits only for required checks. This is one. A pull request that touches the stop
list, or has had the needs-dan label, fails it until the repository owner adds the
dan-approved label after its latest commit.

  python3 -I merge_gate.py                       # in Actions: judge the event's commit
  python3 -I merge_gate.py --repo o/r --pr 42    # locally (GH_TOKEN for a private repo)

action.yml runs it on pull_request_target. It reads PRs, files, label history and
check suites through the REST API and never checks out or runs the PR's code. A check
belongs to a commit, not to a PR, so it judges every open PR whose head is that commit
and passes only if all of them pass. Any error fails the check.

The tiers come from merge_policy.json. classify() is shared with land_pr.py, the tool
that lands agents' PRs, which keeps a byte-identical copy of this file and the policy.
Python 3.9 or newer, standard library only.
"""
import argparse, json, os, re, sys, urllib.error, urllib.parse, urllib.request

POLICY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge_policy.json")
BOT_AUTHORS = {"app/github-actions", "github-actions"}
MAX_FILES = 3000  # GitHub's PR files API stops here


def load_policy(path=POLICY_FILE, local=None):
    """The policy, plus the per-repo entries of an optional local file. A local file can
    only add: new repos, new keys, more list items. It can't change or remove anything."""
    with open(path) as f:
        policy = json.load(f)
    if not local or not os.path.exists(local):
        return policy
    with open(local) as f:
        extra = json.load(f)
    unknown = sorted(set(extra) - {"_doc", "repos"})
    if unknown:
        raise ValueError(f"{local} may only hold per-repo entries, not {unknown}")
    repos = policy.setdefault("repos", {})
    for repo, keys in extra.get("repos", {}).items():
        merged = dict(repos.get(repo, {}))
        for key, value in keys.items():
            if key not in merged:
                merged[key] = value
            elif isinstance(value, list) and isinstance(merged[key], list):
                merged[key] = merged[key] + [v for v in value if v not in merged[key]]
            else:
                raise ValueError(f"{local}: {repo} {key} would replace a value in {path}")
        repos[repo] = merged
    return policy


def glob_re(pattern):
    """A glob as a regex: ** crosses folders, * and ? stay inside one. DOTALL, so a
    line break in a file name can't slip past the stop list."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z", re.DOTALL)


def matches(path, patterns):
    return next((p for p in patterns if glob_re(p).match(path)), None)


def classify(repo, base, labels, files, has_nightly, policy, head="", author=""):
    """(tier, reasons). files: [{"path", "previous", "status"}]; previous is set on renames.
    A repo can name automation branches whose PRs, opened by a workflow, are low tier."""
    dan, low = policy["dan"], policy["low"]
    extra = {k.lower(): v for k, v in policy.get("repos", {}).items()}.get(repo.lower(), {})
    dan_paths = dan["paths"] + extra.get("dan", [])
    reasons = []
    if dan["label"] in labels:
        reasons.append(f"labelled {dan['label']}")
    if has_nightly and base in dan["promotion_bases"]:
        reasons.append(f"targets {base}, a promotion past nightly")
    if len(files) >= MAX_FILES:
        reasons.append(f"{len(files)} files or more, past what GitHub lists")
    paths = sorted({p for f in files for p in (f["path"], f.get("previous")) if p})
    hits = {}
    for path in paths:
        hit = matches(path, dan_paths)
        if hit:
            hits.setdefault(hit, []).append(path)
    for pattern, hit_paths in hits.items():
        more = f" and {len(hit_paths) - 1} more" if len(hit_paths) > 1 else ""
        reasons.append(f"{hit_paths[0]}{more} on the stop list ({pattern})")
    removed = sum(1 for f in files if f.get("status") == "removed")
    if removed > dan["max_removed_files"]:
        reasons.append(f"deletes {removed} files (more than {dan['max_removed_files']})")
    if reasons:
        return "dan", reasons
    if paths and all(matches(p, low["paths"]) and not matches(p, low["except"]) for p in paths):
        return "low", ["only prose and images"]
    if head and author in BOT_AUTHORS and matches(head, extra.get("low_heads", [])):
        return "low", [f"opened by the repo's automation ({head})"]
    return "normal", ["code or config outside the stop list"]


def _labelled(events, label):
    """The PR's 'labeled' events for one label, oldest first."""
    return sorted((e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label),
                  key=lambda e: (e.get("created_at") or "", e.get("id") or 0))


def effective_labels(labels, events, policy):
    """The labels to judge by. needs-dan, once added, holds until Dan approves:
    removing the label doesn't release the PR."""
    hold = policy["dan"]["label"]
    if hold not in labels and _labelled(events, hold):
        return list(labels) + [hold]
    return list(labels)


def first_seen(suites):
    """When GitHub first saw a commit: its earliest check suite (GitHub's clock), or None."""
    times = [s["created_at"] for s in suites if s.get("created_at")]
    return min(times) if times else None


def dan_approved(repo, labels, events, policy, since):
    """(approved, why). Dan approves by adding the approved label from his own account
    (the repo owner) after the PR's head commit appeared (since, from first_seen), so the
    approval covers the commit he saw. A label added by an agent's GitHub App
    (name[bot]) doesn't count. One added with Dan's own token can't be told apart from
    his; the agents' instructions and Bash guard forbid that."""
    label, owner = policy["dan"]["approved_label"], repo.split("/")[0]
    if label not in labels:
        return False, ""
    added = _labelled(events, label)
    last = added[-1] if added else {}
    who = (last.get("actor") or {}).get("login") or ""
    if who.lower() != owner.lower():
        return False, f"{label} was added by {who or 'someone else'}, not by {owner}, so it doesn't count"
    if not since:
        return False, f"{label} can't be tied to a commit (no record of when the head was pushed)"
    if (last.get("created_at") or "") <= since:
        return False, f"{label} was added before the latest commit; Dan removes it and adds it again to approve that"
    return True, f"approved: {who} added {label} after the latest commit"


# --- GitHub ------------------------------------------------------------------

def api(path, token=None, paginate=False):
    """JSON from the REST API; with paginate, the items of every page as one list."""
    url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/{path}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "merge-gate"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    items = []
    while url:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            data, link = json.load(r), r.headers.get("Link") or ""
        if not paginate:
            return data
        items.extend(data)
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = nxt.group(1) if nxt else None
    return items


def branch_exists(get, repo, branch):
    try:
        get(f"repos/{repo}/branches/{urllib.parse.quote(branch, safe='')}")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def author_of(user):
    """A PR author as gh writes it: app/<slug> for an App, else the login."""
    login = (user or {}).get("login") or ""
    if (user or {}).get("type") == "Bot" and login.endswith("[bot]"):
        return "app/" + login[:-len("[bot]")]
    return login


def judge_pr(repo, pr, policy, get, has_nightly, since):
    """(passed, tier, reasons) for one open PR."""
    number = pr["number"]
    files = [{"path": f["filename"], "previous": f.get("previous_filename"), "status": f["status"]}
             for f in get(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True)]
    events = get(f"repos/{repo}/issues/{number}/events?per_page=100", paginate=True)
    labels = effective_labels([label["name"] for label in pr["labels"]], events, policy)
    tier, reasons = classify(repo, pr["base"]["ref"], labels, files, has_nightly, policy,
                             pr["head"]["ref"], author_of(pr.get("user")))
    if tier != "dan":
        return True, tier, reasons
    if policy["dan"]["label"] not in [label["name"] for label in pr["labels"]] and policy["dan"]["label"] in labels:
        reasons.append(f"{policy['dan']['label']} was removed, but it holds until Dan approves")
    approved, why = dan_approved(repo, labels, events, policy, since)
    return approved, tier, reasons + ([why] if why else [])


def gate(repo, sha, policy, get):
    """(passed, lines) for one commit. Branch protection reads the check from the commit,
    so every open PR whose head is this commit must pass."""
    prs = [p for p in get(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True) if p["head"]["sha"] == sha]
    if not prs:
        return False, [f"no open PR has {sha[:10]} as its head; the run for the PR's new head decides"]
    has_nightly = branch_exists(get, repo, "nightly")
    since = first_seen(get(f"repos/{repo}/commits/{sha}/check-suites?per_page=100").get("check_suites", []))
    passed, lines = True, []
    for pr in sorted(prs, key=lambda p: p["number"]):
        ok, tier, reasons = judge_pr(repo, pr, policy, get, has_nightly, since)
        passed = passed and ok
        lines.append(f"#{pr['number']} into {pr['base']['ref']}: {'passes' if ok else 'waits for Dan'} "
                     f"(tier {tier}: {'; '.join(reasons)})")
    return passed, lines


def one_line(text):
    """No line breaks: a file name can't start a workflow command in the log."""
    return " ".join(str(text).splitlines())


def report(passed, lines, sha):
    """Print the verdict and write the job summary, so the reason shows on a phone."""
    lines = [one_line(line) for line in lines]
    title = f"Merge gate {'passed' if passed else 'failed'} for {sha[:10]}"
    print(title)
    for line in lines:
        print(f"- {line}")
    if not passed:
        message = "; ".join(lines).replace("%", "%25")
        print(f"::error title=Merge gate::{message}. Dan approves a stop-list PR by adding the dan-approved label himself.")
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a") as f:
            f.write(f"### {title}\n\n" + "".join(f"- {line}\n" for line in lines))
            if not passed:
                f.write("\nTo let a stop-list PR merge, Dan adds the `dan-approved` label from his own account, "
                        "after its latest commit. A label added by an agent's App doesn't count.\n")


def main(argv=None, get=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    ap.add_argument("--pr", type=int, help="PR number (default: the Actions event's PR)")
    ap.add_argument("--policy", default=POLICY_FILE, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if get is None:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        get = lambda path, paginate=False: api(path, token, paginate)  # noqa: E731
    sha = ""
    try:
        if a.pr is not None:
            sha = get(f"repos/{a.repo}/pulls/{a.pr}")["head"]["sha"]
        elif os.environ.get("GITHUB_EVENT_PATH"):
            with open(os.environ["GITHUB_EVENT_PATH"]) as f:
                sha = json.load(f)["pull_request"]["head"]["sha"]
        if not a.repo or not sha:
            ap.error("needs --repo and --pr outside a pull_request_target run")
        passed, lines = gate(a.repo, sha, load_policy(a.policy), get)
    except Exception as e:  # noqa: BLE001  (any failure fails the check)
        passed, lines = False, [f"could not judge the commit ({type(e).__name__}: {e}); re-run this check"]
    report(passed, lines, sha or "unknown")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
