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
| Touches a stop-list path, targets `weekly`/`main` in a repo with `nightly`, deletes more than 25 files, or has `needs-dan` | fails |
| Any of those, with `dan-approved` added by the repository owner | passes |
| Any of those, with `dan-approved` added by someone else (an agent's App) | fails |

The tiers are in [`merge_policy.json`](merge_policy.json); [`merge_gate.py`](merge_gate.py)
applies them. The same file and policy also drive the client-side tool that lands
agents' PRs, so the two can't disagree.

## How it stays safe

- It runs on `pull_request_target`, so GitHub runs the workflow file from the base
  branch. A PR can't edit its own gate.
- It never checks out or runs the PR's code. It reads the PR's files, labels and
  label history through the REST API with a read-only token.
- Any error fails the check. Re-run it.
- Every repository runs this repository's `main`, so a change here applies everywhere
  at once. `merge_gate.py`, `merge_policy.json` and `action.yml` are on this
  repository's own stop list.

## Add it to a repository

1. Copy [`.github/workflows/merge-gate.yml`](.github/workflows/merge-gate.yml) unchanged
   into the repository's integration branch and merge it.
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

- Where branch protection doesn't bind admins, an admin's direct merge (the web
  "bypass" box, `gh pr merge --admin`, a raw API merge) still skips required checks.
  The gate stops the auto-merge paths, not a deliberate override.
- `dan-approved` stays on after later pushes. Remove it if a PR changes after approval.
- Anyone with write access could add a workflow that posts a passing check with the
  same name. The gate guards against mistakes, not against a determined insider.
