# Merge gate

A required GitHub check that holds risky pull requests for Dan.

AI agents open and land most pull requests in Dan's repositories. A short stop list
still waits for Dan: workflows with power (a secret, write access, a privileged trigger),
release and signing config, secrets, agent permissions, promotions past `nightly`, and
deleting more than 25 files. A workflow with none of that power and fastlane's store text
and screenshots go to an independent reviewer instead.
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

`dan.relief` in the policy lets four kinds of stop-list change through; remove a key to
switch its rule off. The first three can't alter what runs. The fourth stays normal tier, so
a reviewer still reads it. `needs-dan` still holds all of them.

| Rule | Passes | Still waits for Dan |
|---|---|---|
| `reference` | a file byte-identical to the same path in `dfakkeldy/merge-gate` at `main` (the caller workflow every repo copies) | any other edit to that file; a rename or removal; a change to the reference itself (once, here) |
| `comment_only` | a `.yml`/`.yaml` file where only whole-line comments or blank lines changed, outside block scalars | a `#` line inside a block scalar (`prompt: \|`, `run: \|`), which is content; a trailing comment on a value line; anything the scanner can't read with confidence (a multi-line quoted or flow value, a quoted key opening a block, a tag before a block indicator, a tab in indentation) |
| `back_merge` | in a PR into `nightly`, a stop-list file that is exactly `main`'s (or `weekly`'s) copy, where `nightly` hadn't changed it since the two last shared history. If every changed file passes, the whole PR is low tier | a file that mixes both sides after a conflict, or that would replace `nightly`'s own newer work with `main`'s copy |
| `plain_workflow` | a modified or new workflow whose text before, after and on the base's tip has no secret but `GITHUB_TOKEN`, no write access, no `pull_request_target`, `workflow_run` or `workflow_call`, no self-hosted runner and no name of a check the gates trust, where the edit adds no way to ignore a failure | anything else; a rename or removal; a name in `held_names`; a path on the repo's own list |

`dan.except` frees named paths from one stop-list pattern. It frees fastlane's store text and
screenshots from `**/fastlane/**`; any other pattern still holds a file in those folders.

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

- `plain_workflow` reads a workflow's text, not what it does. It can't see an edit that
  removes a test step or skips a test. Branch protection fails a required check that
  disappears; anything subtler is the reviewer's to catch. It also relies on each repo's
  default workflow token being read-only, which the gate can't read: a repo set to read and
  write gives every workflow write access without asking. It matches only the runner label
  `self-hosted`, so a custom label passes. A plain workflow can also write a cache that a
  release job restores if the two share a key, or join a release's concurrency group and
  cancel it. A backslash-newline inside a quoted value can hide a word from the scan. All
  of these take a deliberate edit, and a reviewer still reads the workflow.
- Where branch protection doesn't bind admins, an admin's direct merge (the web
  "bypass" box, `gh pr merge --admin`, a raw API merge) still skips required checks.
  The gate stops the auto-merge paths, not a deliberate override.
- An agent that uses the owner's own token can add `dan-approved` as the owner. The
  gate can't tell that apart from Dan. The agents' rules forbid it, and their Bash
  guard flags the usual `gh` forms (a warning, unless the guard is set to block). Two
  sanctioned exceptions: Dan's 👍 on the PR steward's daily digest approves the PRs it
  lists, and the steward adds the label with his token to each PR whose head is still
  the commit the digest listed; and Dan's own yes in chat, naming the PRs, lets the agent
  he told add it.
- A commit pushed to some other branch before Dan's approval, then made the PR's head,
  still counts as seen before the approval.
- Creating a `nightly` branch later doesn't re-judge open PRs into `main`; nudge them.
- A PR can add its own workflow with a job named `Merge gate` that passes. It runs
  from the PR's branch, comes from the same GitHub Actions app, and can be the latest
  check of that name. Only a dedicated GitHub App posting the check would close this.
- The gate sees only this public policy. Per-repo extras that name private
  repositories stay with the client-side tool and aren't enforced here.
