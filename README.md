# Merge gate

A required GitHub check that holds risky pull requests for Dan.

AI agents open and land most pull requests in Dan's repositories. A short stop list
still waits for Dan: workflows that release, deploy, hold an agent's prompt or hold an
API key, the gate itself, signing and release lanes, secrets, agent permissions, promotions past
`nightly`, and deleting more than 25 files. Ordinary CI workflows and fastlane's store
text and screenshots aren't on it: an independent reviewer and CI clear those.
GitHub's native auto-merge (`gh pr merge --auto`, apps that arm it, the merge
button) waits only for required checks. So the stop list has to be a required check.

```
pull request ──► Merge gate ──► stop list or needs-dan?
                                   │ no  → pass
                                   │ yes → fail ──► Dan adds dan-approved ──► pass
```

| PR | Merge gate |
|---|---|
| Code, docs, images | passes |
| Touches a stop-list path, targets `weekly`/`main` in a repo with `nightly`, deletes more than 25 files, or has had `needs-dan` (removing the label doesn't clear it) | fails |
| Any of those, with `dan-approved` added by the repository owner after the latest commit | passes |
| Any of those, with `dan-approved` added before the latest commit | fails until Dan removes and adds it again |
| Any of those, with `dan-approved` added by an agent's App | fails |
| A stop-list file that can't change what runs (see below), and nothing else on the stop list | passes |

The tiers are in [`merge_policy.json`](merge_policy.json); [`merge_gate.py`](merge_gate.py)
applies them. The client-side tool that lands agents' PRs uses byte-identical copies
of both, so they agree on every repository that uses only this policy.

## Stop-list changes that pass without Dan

Some stop-list changes can't alter what runs. `dan.relief` in the policy lets three
kinds through; remove a key to switch its rule off. `needs-dan` still holds all of them.

| Rule | Passes | Still waits for Dan |
|---|---|---|
| `reference` | a file byte-identical to the same path in `dfakkeldy/merge-gate` at `main` (the caller workflow every repo copies) | any other edit to that file; a rename or removal; a change to the reference itself (once, here) |
| `comment_only` | a `.yml`/`.yaml` file where only whole-line comments or blank lines changed, outside block scalars | a `#` line inside a block scalar (`prompt: \|`, `run: \|`), which is content; a trailing comment on a value line; anything the scanner can't read with confidence (a multi-line quoted or flow value, a quoted key opening a block, a tag before a block indicator, a tab in indentation) |
| `back_merge` | in a PR into `nightly`, a stop-list file that is exactly `main`'s (or `weekly`'s) copy, where `nightly` hadn't changed it since the two last shared history. If every changed file passes, the whole PR is low tier | a file that mixes both sides after a conflict, or that would replace `nightly`'s own newer work with `main`'s copy |

What these rules can't see. `comment_only` reads YAML with a small scanner, not a
parser, so it refuses anything unusual rather than guess: any whitespace but a plain
space, a character YAML can't print, a value that might span lines, a block indicator
it doesn't know. A test checks
it against PyYAML on every comment edit it accepts; on 53 real workflow files it
read 37 and accepted 32,684 edits, and PyYAML agreed with every one. Files are read from git trees,
so a symlink or a mode change never passes, and branches are read as branches, so a tag
of the same name can't stand in. `reference` and `back_merge` trust the branch they
compare with (`main` here, `main` in the app repo): where that branch doesn't bind
admins, Dan's token, which agents share, can push to it directly. Any error while
checking means no relief, never a pass.

## How it stays safe

- It runs on `pull_request_target`, so GitHub runs the workflow file from the
  repository's default branch, whatever the PR's base. A PR can't edit its own gate.
- It never checks out or runs the PR's code. It reads PRs, files, label history and
  check suites through the REST API with a read-only token, and runs Python in
  isolated mode (`-I`), so no file beside the script can stand in for a library.
- A check belongs to a commit, and several PRs can share a head commit. The gate
  judges every open PR whose head is that commit and passes only if all of them pass.
- Dan's label counts only if it was added after the PR took its current shape: after
  GitHub first saw the head commit (its earliest check suite) and after the PR's last
  force-push or change of base, all on GitHub's clock. Any of those after approval
  needs a new one.
- `needs-dan` holds once added, even after someone removes it. A PR that later drops
  its stop-list change still needs `dan-approved`, or a fresh PR.
- Any error fails the check. Re-run it.
- Every repository runs this repository's `main`, so a change here applies everywhere
  at once. Every path in this repository is on its own stop list.

## Add it to a repository

1. Copy [`.github/workflows/merge-gate.yml`](.github/workflows/merge-gate.yml) unchanged
   into the repository's **default branch** and merge it. GitHub starts
   `pull_request_target` from the default branch for PRs into any branch, so a copy
   only on another branch (say `nightly`, where `main` is the default) never runs
   ([#4](https://github.com/dfakkeldy/merge-gate/pull/4) showed a PR into a branch
   without the file still running the gate). Where the
   integration branch isn't the default, put the same file on both, so promotions
   merge it without conflict.
2. Then require the check (a check that can't run yet would block every PR):

   ```bash
   gh api -X POST repos/OWNER/REPO/branches/BRANCH/protection/required_status_checks/contexts \
     -f 'contexts[]=Merge gate'
   ```

   Or pin it to the GitHub Actions app (id 15368) with a `PATCH` of
   `required_status_checks` that lists every existing check plus
   `{"context": "Merge gate", "app_id": 15368}`.
3. Create the labels: `needs-dan` and `dan-approved`.
4. Nudge open PRs once (an edit of the body is enough), so each gets a result.

## Limits

The gate guards against mistakes, not against someone with write access who sets
out to get round it.

- Workflows are held by name. On 8 Nov 2026 every live workflow in the gated repos that
  used a secret other than `GITHUB_TOKEN` was on the list. A new workflow with an ordinary
  name that deploys or holds a secret isn't held; the reviewer has to catch it. So does an
  edit to `ci.yml` that adds a write permission or weakens a required check (a skipped
  test, `continue-on-error`).
- Where branch protection doesn't bind admins, an admin's direct merge (the web
  "bypass" box, `gh pr merge --admin`, a raw API merge) still skips required checks.
  The gate stops the auto-merge paths, not a deliberate override.
- An agent that uses the owner's own token can add `dan-approved` as the owner. The
  gate can't tell that apart from Dan. The agents' rules forbid it, and their Bash
  guard flags the usual `gh` forms (a warning, unless the guard is set to block). The
  one sanctioned exception: Dan's 👍 on the PR steward's daily digest approves the PRs
  it lists, and the steward adds the label with his token to each PR whose head is
  still the commit the digest listed.
- A commit pushed to some other branch before Dan's approval, then made the PR's head,
  still counts as seen before the approval.
- Creating a `nightly` branch later doesn't re-judge open PRs into `main`; nudge them.
- A PR can add its own workflow with a job named `Merge gate` that passes. It runs
  from the PR's branch, comes from the same GitHub Actions app, and can be the latest
  check of that name. Only a dedicated GitHub App posting the check would close this.
- The gate sees only this public policy. Per-repo extras that name private
  repositories stay with the client-side tool and aren't enforced here.
