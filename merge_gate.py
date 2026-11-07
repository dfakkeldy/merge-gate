"""Merge gate: Dan's merge stop list, enforced on GitHub's side.

GitHub's native auto-merge (gh pr merge --auto, the merge button, apps that arm it)
waits only for required checks. This is one. A pull request that touches the stop
list, or has the needs-dan label, fails it until the repository owner adds the
dan-approved label.

  python3 merge_gate.py                       # in Actions: judge the event's PR
  python3 merge_gate.py --repo o/r --pr 42    # locally (GH_TOKEN for a private repo)

action.yml runs it on pull_request_target. It reads the PR's files, labels and label
history through the REST API and never checks out or runs the PR's code. An error
fails the check: a gate that can't see the PR doesn't wave it through.

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
    """A glob as a regex: ** crosses folders, * and ? stay inside one."""
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
    return re.compile(out + r"\Z")


def matches(path, patterns):
    return next((p for p in patterns if glob_re(p).match(path)), None)


def classify(repo, base, labels, files, has_nightly, policy, head="", author=""):
    """(tier, reasons). files: [{"path", "previous", "status"}]; previous is set on renames.
    A repo can name automation branches whose PRs, opened by a workflow, are low tier."""
    dan, low = policy["dan"], policy["low"]
    extra = policy.get("repos", {}).get(repo, {})
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


def approver(events, label):
    """Who last added `label`, from a PR's issue events, or None."""
    who = None
    for e in sorted(events, key=lambda e: (e.get("created_at") or "", e.get("id") or 0)):
        if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label:
            who = (e.get("actor") or {}).get("login")
    return who


def dan_approved(repo, labels, events, policy):
    """(approved, who added the label). Only the repository owner's own label counts.
    Agents label as their GitHub Apps (name[bot]), so an agent can't approve its own PR."""
    label = policy["dan"]["approved_label"]
    if label not in labels:
        return False, None
    who = approver(events, label)
    return bool(who) and who.lower() == repo.split("/")[0].lower(), who


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
    login = user.get("login", "")
    if user.get("type") == "Bot" and login.endswith("[bot]"):
        return "app/" + login[:-len("[bot]")]
    return login


def gate(repo, number, policy, get):
    """(passed, tier, lines) for one PR. get(path, paginate=False) reads the REST API."""
    pr = get(f"repos/{repo}/pulls/{number}")
    files = [{"path": f["filename"], "previous": f.get("previous_filename"), "status": f["status"]}
             for f in get(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True)]
    labels = [label["name"] for label in pr["labels"]]
    tier, reasons = classify(repo, pr["base"]["ref"], labels, files, branch_exists(get, repo, "nightly"),
                             policy, pr["head"]["ref"], author_of(pr["user"]))
    if tier != "dan":
        return True, tier, reasons
    approved_label = policy["dan"]["approved_label"]
    events = get(f"repos/{repo}/issues/{number}/events?per_page=100", paginate=True) \
        if approved_label in labels else []
    ok, who = dan_approved(repo, labels, events, policy)
    if ok:
        return True, tier, reasons + [f"approved: {who} added {approved_label}"]
    if who:
        reasons.append(f"{approved_label} was added by {who}, not by {repo.split('/')[0]}, so it doesn't count")
    return False, tier, reasons


def report(passed, tier, lines, number):
    """Print the verdict and write the job summary, so the reason shows on a phone."""
    stop = not passed and tier == "dan"
    title = (f"Merge gate passed (tier {tier})" if passed else
             f"Merge gate: PR #{number} is on the merge stop list and waits for Dan" if stop else
             f"Merge gate could not judge PR #{number}")
    print(title)
    for line in lines:
        print(f"- {line}")
    if not passed:
        hint = " Dan approves it by adding the dan-approved label himself." if stop else ""
        print(f"::error title=Merge gate::{'; '.join(lines)}.{hint}")
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a") as f:
            f.write(f"### {title}\n\n" + "".join(f"- {line}\n" for line in lines))
            if stop:
                f.write("\nTo let it merge, Dan adds the `dan-approved` label from his own account. "
                        "A label added by an agent's App doesn't count.\n")


def main(argv=None, get=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    ap.add_argument("--pr", type=int, help="PR number (default: the Actions event's PR)")
    ap.add_argument("--policy", default=POLICY_FILE, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    number = a.pr
    if number is None and os.environ.get("GITHUB_EVENT_PATH"):
        with open(os.environ["GITHUB_EVENT_PATH"]) as f:
            number = (json.load(f).get("pull_request") or {}).get("number")
    if not a.repo or not number:
        ap.error("needs --repo and --pr outside a pull_request_target run")
    if get is None:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        get = lambda path, paginate=False: api(path, token, paginate)  # noqa: E731
    try:
        passed, tier, lines = gate(a.repo, number, load_policy(a.policy), get)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        passed, tier, lines = False, "unknown", [f"could not judge the PR ({e}); re-run this check"]
    report(passed, tier, lines, number)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
