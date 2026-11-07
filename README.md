# Merge gate

A required GitHub check that holds risky pull requests for Dan.

AI agents open and land most pull requests in Dan's repositories. A short stop list
still waits for Dan: CI and workflow files, release and signing config, secrets,
agent permissions, promotions past `nightly`, and deleting more than 25 files.
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

The tiers are in [`merge_policy.json`](merge_policy.json); [`merge_gate.py`](merge_gate.py)
applies them. The client-side tool that lands agents' PRs uses byte-identical copies
of both, so they agree on every repository that uses only this policy.

## How it stays safe

- It runs on `pull_request_target`, so GitHub runs the workflow file from the base
  branch. A PR can't edit its own gate.
- It never checks out or runs the PR's code. It reads PRs, files, label history and
  check suites through the REST API with a read-only token, and runs Python in
  isolated mode (`-I`), so no file beside the script can stand in for a library.
- A check belongs to a commit, and several PRs can share a head commit. The gate
  judges every open PR whose head is that commit and passes only if all of them pass.
- Dan's label counts only if it was added after GitHub first saw the head commit (its
  earliest check suite, on GitHub's clock). A push after approval needs a new one.
- Any error fails the check. Re-run it.
- Every repository runs this repository's `main`, so a change here applies everywhere
  at once. Every path in this repository is on its own stop list.

## Add it to a repository

1. Copy [`.github/workflows/merge-gate.yml`](.github/workflows/merge-gate.yml) unchanged
   into every branch that will require the check, and merge it. `pull_request_target`
   runs the file from the PR's base, so a PR into a branch without it gets no run,
   and a retargeted PR keeps its old result.
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

- Where branch protection doesn't bind admins, an admin's direct merge (the web
  "bypass" box, `gh pr merge --admin`, a raw API merge) still skips required checks.
  The gate stops the auto-merge paths, not a deliberate override.
- An agent that uses the owner's own token can add `dan-approved` as the owner. The
  gate can't tell that apart from Dan; the agents' rules and their Bash guard forbid it.
- A PR can add its own workflow with a job named `Merge gate` that passes. It runs
  from the PR's branch, comes from the same GitHub Actions app, and can be the latest
  check of that name. Only a dedicated GitHub App posting the check would close this.
- The gate sees only this public policy. Per-repo extras that name private
  repositories stay with the client-side tool and aren't enforced here.
