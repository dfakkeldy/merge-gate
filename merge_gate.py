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

Three kinds of stop-list change can't alter what runs, so relief() lets them through
without Dan (each one switched on by the policy's dan.relief):
  reference     a file that is an exact copy of the same file in a reference repo (the
                gate's own caller workflow, copied into every repo). Changing the
                reference itself still waits for Dan, once.
  comment_only  a YAML file where only whole-line comments or blank lines changed. Lines
                inside a block scalar (`prompt: |`, `run: |`) are content, `#` or not, and
                anything the scanner can't read with confidence counts as a real change.
  back_merge    in a PR into nightly, a stop-list file that is exactly main's (or weekly's)
                copy, where nightly hadn't changed the file since it last shared history
                with that branch: it only brings nightly what main already has. When every
                changed file passes, the whole PR is low tier. A conflict resolution that
                mixes both sides, or that would drop nightly's own work, fails and waits.
A needs-dan label still holds every one of them.

Python 3.9 or newer, standard library only.
"""
import argparse, base64, binascii, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

POLICY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge_policy.json")
BOT_AUTHORS = {"app/github-actions", "github-actions"}
MAX_FILES = 3000  # GitHub's PR files API stops here
# PR events that change what an approval covered, on GitHub's clock.
RESHAPED = {"head_ref_force_pushed", "base_ref_changed", "automatic_base_change_succeeded"}


def load_policy(path=POLICY_FILE, local=None):
    """The policy, plus the per-repo entries of an optional local file. A local file can
    only add: new repos, new keys, more list items. It can't change or remove anything.
    Repo names are lower-cased first, so a different spelling can't slip past that rule."""
    with open(path) as f:
        policy = json.load(f)
    policy["repos"] = {k.lower(): v for k, v in policy.get("repos", {}).items()}
    if not local or not os.path.exists(local):
        return policy
    with open(local) as f:
        extra = json.load(f)
    unknown = sorted(set(extra) - {"_doc", "repos"})
    if unknown:
        raise ValueError(f"{local} may only hold per-repo entries, not {unknown}")
    repos = policy["repos"]
    for repo, keys in extra.get("repos", {}).items():
        repo = repo.lower()
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


def repo_extra(repo, policy):
    return {k.lower(): v for k, v in policy.get("repos", {}).items()}.get(repo.lower(), {})


def stop_paths(repo, policy):
    return policy["dan"]["paths"] + repo_extra(repo, policy).get("dan", [])


def classify(repo, base, labels, files, has_nightly, policy, head="", author="", exempt=(), back_merge=None):
    """(tier, reasons). files: [{"path", "previous", "status"}]; previous is set on renames.
    A repo can name automation branches whose PRs, opened by a workflow, are low tier.
    exempt and back_merge come from relief(): stop-list paths whose change can't alter what
    runs, and the ladder branch a pure back-merge into nightly already matches."""
    dan, low = policy["dan"], policy["low"]
    extra = repo_extra(repo, policy)
    dan_paths = stop_paths(repo, policy)
    reasons = []
    if dan["label"] in labels:
        reasons.append(f"labelled {dan['label']}")
    if back_merge and not reasons and has_nightly and base == "nightly":
        return "low", [f"a back-merge: every file it changes already matches {back_merge}"]
    if has_nightly and base in dan["promotion_bases"]:
        reasons.append(f"targets {base}, a promotion past nightly")
    if len(files) >= MAX_FILES:
        reasons.append(f"{len(files)} files or more, past what GitHub lists")
    paths = sorted({p for f in files for p in (f["path"], f.get("previous")) if p})
    hits = {}
    for path in paths:
        hit = None if path in exempt else matches(path, dan_paths)
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
    if paths and all(p in exempt or (matches(p, low["paths"]) and not matches(p, low["except"])) for p in paths):
        return "low", ["only prose and images" if not exempt else
                       "only prose and images, and stop-list files that can't change what runs"]
    if head and author in BOT_AUTHORS and matches(head, extra.get("low_heads", [])):
        return "low", [f"opened by the repo's automation ({head})"]
    return "normal", ["code or config outside the stop list"]


# --- comment-only YAML ------------------------------------------------------------

_INDICATOR = r"[|>](?:[1-9][+-]?|[+-][1-9]?)?\s*(?:#.*)?$"
# `key: |`, `- >-`, `- key: |2`: a plain key (no quotes, no flow) then a block indicator.
_BLOCK_OPENER = re.compile(r"^(?:-\s+)*(?:[^\s#'\"{}\[\],&*!|>%@`-][^#'\"{}\[\]]*?:\s+)?" + _INDICATOR)
_ENDS_LIKE_BLOCK = re.compile(_INDICATOR)
_PLAIN_KEY = re.compile(r"[^\s#'\"{}\[\],&*!|>%@`-][^#'\"{}\[\]]*?:(?:\s+|$)")


def _value(stripped):
    """A line's value: what follows its list dashes and its plain key, if any."""
    s = stripped
    while s.startswith("- "):
        s = s[2:].lstrip()
    m = _PLAIN_KEY.match(s)
    return s[m.end():] if m else s


def _closed(value):
    """False when a quoted or flow value doesn't end on its own line (so later lines,
    `#` ones included, could be inside it)."""
    v = value.strip()
    if v.startswith('"'):
        i = 1
        while i < len(v):
            if v[i] == "\\":
                i += 2
                continue
            if v[i] == '"':
                return True
            i += 1
        return False
    if v.startswith("'"):
        i = 1
        while i < len(v):
            if v[i] == "'":
                if v[i + 1:i + 2] == "'":
                    i += 2
                    continue
                return True
            i += 1
        return False
    if v.startswith(("[", "{")):
        return v.count("[") + v.count("{") == v.count("]") + v.count("}")
    return True


def yaml_meaning(text):
    """The lines of a YAML file that can change what it means, or None when the scanner
    isn't sure. Whole-line comments and blank lines outside block scalars drop out. Every
    line inside a block scalar (`prompt: |`, `run: >-`) is kept as it is, `#` lines too:
    there they are content, like a heading in an agent's prompt. A value that might span
    lines (an open quote or flow collection), a block opener it doesn't recognise (a
    quoted key, say) and a tab in indentation all make it give up."""
    out, block = [], None  # block: the indentation of the line that opened a block scalar
    for line in text.splitlines():
        lead = line[:len(line) - len(line.lstrip())]
        if "\t" in lead:
            return None
        stripped, indent = line.strip(), len(lead)
        if block is not None:
            if not stripped or indent > block:
                out.append(line)
                continue
            block = None
        if not stripped or stripped.startswith("#"):
            continue
        if _ENDS_LIKE_BLOCK.search(stripped):  # a block opener, or something it can't tell from one
            if not _BLOCK_OPENER.match(stripped):
                return None
            block = indent
        elif not _closed(_value(stripped)):
            return None
        out.append(line.rstrip())
    return out


def comment_only(before, after):
    """True when two versions of a YAML file differ only in whole-line comments or blank
    lines outside block scalars."""
    if before == after:
        return False
    a, b = yaml_meaning(before), yaml_meaning(after)
    return a is not None and a == b


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


def shaped_since(commit_seen, events):
    """When the PR took its current shape: the later of its head commit's first check
    suite and its last force-push or change of base. None when the commit's time is
    unknown, so an approval is never tied to a guess."""
    if not commit_seen:
        return None
    return max([commit_seen] + [e["created_at"] for e in events
                                if e.get("event") in RESHAPED and e.get("created_at")])


def dan_approved(repo, labels, events, policy, since):
    """(approved, why). Dan approves by adding the approved label from his own account
    (the repo owner) after the PR took its current shape (since, from shaped_since), so
    the approval covers the commit and base he saw. A label added by an agent's GitHub
    App (name[bot]) doesn't count. One added with Dan's own token can't be told apart
    from his; the agents' rules forbid that and their Bash guard flags it. The one
    sanctioned exception: Dan's 👍 on the PR steward's daily digest approves the PRs it
    lists, and the steward then adds this label with his token, to each PR whose head is
    still the commit the digest listed."""
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
        return False, (f"{label} was added before the latest commit, force-push or change of base; "
                       "Dan removes it and adds it again to approve that")
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


def quote(ref):
    return urllib.parse.quote(ref, safe="")


def file_at(get, repo, path, ref):
    """(blob sha, text) of a file at a ref; (None, None) when it isn't there. text is None
    when the contents API won't give it inline (over 1 MB) or it isn't UTF-8."""
    try:
        data = get(f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={quote(ref)}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise
    if not isinstance(data, dict) or data.get("type") != "file":
        raise ValueError(f"{path} at {ref} is not a file")
    text = None
    if data.get("encoding") == "base64" and data.get("content") is not None:
        try:
            text = base64.b64decode(data["content"]).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            text = None
    return data["sha"], text


def tree_at(get, repo, ref):
    """{path: blob sha} of every file at a ref. Refuses a tree GitHub cut short."""
    data = get(f"repos/{repo}/git/trees/{quote(ref)}?recursive=1")
    if data.get("truncated"):
        raise ValueError(f"the tree at {ref} is too big for GitHub to list")
    return {e["path"]: e["sha"] for e in data.get("tree", []) if e.get("type") in ("blob", "commit")}


def merge_base(get, repo, base, head):
    return get(f"repos/{repo}/compare/{quote(base)}...{quote(head)}")["merge_base_commit"]["sha"]


def back_merge_check(repo, base, head_sha, files, policy, get):
    """(source, carried) for a PR into base. A changed file is carried from a ladder branch
    above base (main, then weekly) when the PR's copy equals that branch's and base hadn't
    changed the file since the two last shared history: it only brings base what that
    branch already has. source is the branch that carries every changed file (a pure
    back-merge), or None; carried maps each carried path to its branch. Read from git
    trees, not commits: a back-merge branch has a merge commit of its own, a conflict
    resolution that mixes both sides matches neither branch, and one that took main's copy
    over base's own newer work would drop that work, so it fails the second test."""
    paths = {p for f in files for p in (f["path"], f.get("previous")) if p}
    carried, head, base_tree = {}, None, None
    for upper in reversed(policy["dan"]["promotion_bases"]):  # main first
        try:
            upper_tree = tree_at(get, repo, upper)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # this repo has no such branch
                continue
            raise
        if head is None:
            head, base_tree = tree_at(get, repo, head_sha), tree_at(get, repo, base)
        shared = tree_at(get, repo, merge_base(get, repo, base, upper))
        ok = {p for p in paths if head.get(p) == upper_tree.get(p) and base_tree.get(p) == shared.get(p)}
        if ok == paths:
            return upper, {p: upper for p in paths}
        for p in ok:
            carried.setdefault(p, upper)
    return None, carried


def relief(repo, base, head_sha, files, has_nightly, policy, get, back_merge=True):
    """(exempt, back_merge, notes) for a PR the stop list would hold, from what GitHub
    shows now (see the module docstring). Any doubt, a GitHub error included, means no
    relief: the PR waits for Dan as before, and notes says why."""
    rules = policy["dan"].get("relief") or {}
    exempt, back, notes = set(), None, []
    if len(files) >= MAX_FILES:
        return exempt, back, notes
    stop = stop_paths(repo, policy)
    hits = [f for f in files if matches(f["path"], stop) and f["status"] in ("modified", "added")
            and not f.get("previous")]
    try:
        ref = rules.get("reference") or {}
        for f in hits:
            if ref.get("repo") and f["path"] in ref.get("paths", []):
                mine, _ = file_at(get, repo, f["path"], head_sha)
                theirs, _ = file_at(get, ref["repo"], f["path"], ref.get("ref", "main"))
                if mine and mine == theirs:
                    exempt.add(f["path"])
                    notes.append(f"{f['path']} is an exact copy of {ref['repo']}'s")
        suffixes = tuple(rules.get("comment_only") or ())
        todo = [f for f in hits if suffixes and f["path"] not in exempt and f["status"] == "modified"
                and f["path"].lower().endswith(suffixes)]
        if todo:
            since = merge_base(get, repo, base, head_sha)  # GitHub's diff is against the merge base
            for f in todo:
                _, before = file_at(get, repo, f["path"], since)
                _, after = file_at(get, repo, f["path"], head_sha)
                if before is not None and after is not None and comment_only(before, after):
                    exempt.add(f["path"])
                    notes.append(f"{f['path']}: only comments or blank lines changed")
        if back_merge and rules.get("back_merge") and has_nightly and base == "nightly":
            back, carried = back_merge_check(repo, base, head_sha, files, policy, get)
            if not back:  # a partial back-merge: the stop-list files it only carries from main still pass
                for f in hits:
                    if f["path"] in carried and f["path"] not in exempt:
                        exempt.add(f["path"])
                        notes.append(f"{f['path']} is {carried[f['path']]}'s copy, which {base} hadn't changed")
    except Exception as e:  # noqa: BLE001  (relief is optional: any doubt means the PR waits for Dan)
        return set(), None, [f"could not check for copies, comments or a back-merge "
                             f"({type(e).__name__}: {one_line(e)}), so no relief"]
    return exempt, back, notes


def author_of(user):
    """A PR author as gh writes it: app/<slug> for an App, else the login."""
    login = (user or {}).get("login") or ""
    if (user or {}).get("type") == "Bot" and login.endswith("[bot]"):
        return "app/" + login[:-len("[bot]")]
    return login


def judge_pr(repo, pr, policy, get, has_nightly, commit_seen):
    """(passed, tier, reasons) for one open PR."""
    number = pr["number"]
    files = [{"path": f["filename"], "previous": f.get("previous_filename"), "status": f["status"]}
             for f in get(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True)]
    events = get(f"repos/{repo}/issues/{number}/events?per_page=100", paginate=True)
    labels = effective_labels([label["name"] for label in pr["labels"]], events, policy)
    args = (repo, pr["base"]["ref"], labels, files, has_nightly, policy, pr["head"]["ref"], author_of(pr.get("user")))
    tier, reasons = classify(*args)
    if tier == "dan" and policy["dan"]["label"] not in labels:  # a held PR stays held
        exempt, back, notes = relief(repo, pr["base"]["ref"], pr["head"]["sha"], files, has_nightly, policy, get)
        if exempt or back:
            tier, reasons = classify(*args, exempt=exempt, back_merge=back)
        reasons = reasons + notes
    if tier != "dan":
        return True, tier, reasons
    if policy["dan"]["label"] not in [label["name"] for label in pr["labels"]] and policy["dan"]["label"] in labels:
        reasons.append(f"{policy['dan']['label']} was removed, but it holds until Dan approves")
    approved, why = dan_approved(repo, labels, events, policy, shaped_since(commit_seen, events))
    return approved, tier, reasons + ([why] if why else [])


def gate(repo, sha, policy, get):
    """(passed, lines) for one commit. Branch protection reads the check from the commit,
    so every open PR whose head is this commit must pass."""
    for attempt in range(3):  # the PR list can lag a moment behind a push
        prs = [p for p in get(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True)
               if p["head"]["sha"] == sha]
        if prs:
            break
        if attempt < 2:
            time.sleep(5)
    else:
        return False, [f"no open PR has {sha[:10]} as its head. If one does now, re-run this check"]
    has_nightly = branch_exists(get, repo, "nightly")
    commit_seen = first_seen(get(f"repos/{repo}/commits/{sha}/check-suites?per_page=100").get("check_suites", []))
    passed, lines = True, []
    for pr in sorted(prs, key=lambda p: p["number"]):
        ok, tier, reasons = judge_pr(repo, pr, policy, get, has_nightly, commit_seen)
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
