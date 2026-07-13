# GitHub Actions — publish workflow

The [publish workflow](../.github/workflows/publish.yml) runs catalog video and daily quote publishing. Like the catalog daily workflow, it is triggered via **`workflow_dispatch`** (manual UI, local script, or [cron-job.org](https://cron-job.org)).

Default mode is **staggered**: Instagram publishes immediately today; YouTube and Facebook schedule tomorrow for review.

## Manual run (GitHub UI)

**Actions → Publish → Run workflow**

| Input | Use |
|-------|-----|
| **mode** | `all`, `videos`, or `quotes` |
| **private** | Enable for a safe test (YouTube private, Facebook +20 days, Instagram skipped) |

## Trigger via API (same as cron-job.org)

Use the same PAT as the catalog cron job (**Actions: Read and write** on this repo). Branch: **`master`**.

### Private test run (PowerShell)

```powershell
$headers = @{
  Authorization = "Bearer YOUR_TOKEN"
  Accept        = "application/vnd.github+json"
  "X-GitHub-Api-Version" = "2022-11-28"
}
$body = @{
  ref = "master"
  inputs = @{
    mode    = "all"
    private = "true"
  }
} | ConvertTo-Json

Invoke-RestMethod `
  -Method POST `
  -Uri "https://api.github.com/repos/thecyberix/media-publisher/actions/workflows/publish.yml/dispatches" `
  -Headers $headers `
  -Body $body `
  -ContentType "application/json"
```

Expected: HTTP **204**. A new run appears under **Actions → Publish** within seconds.

### Local helper script

```powershell
$env:GITHUB_DISPATCH_TOKEN = "YOUR_TOKEN"
python scripts/trigger_github_workflow_dispatch.py publish.yml --private --mode all
```

## cron-job.org jobs (production schedule)

Create separate cron jobs per content type. Reuse the same GitHub token and headers as [catalog-github-actions.md](catalog-github-actions.md).

### Catalog videos (~18:00 Europe/Sofia)

Suggested schedule: `0 18 * * *`, time zone **Europe/Sofia**.

**URL:** `https://api.github.com/repos/thecyberix/media-publisher/actions/workflows/publish.yml/dispatches`

**Body:**

```json
{
  "ref": "master",
  "inputs": {
    "mode": "videos",
    "private": "false"
  }
}
```

### Daily quotes (~08:00 Europe/Sofia)

Suggested schedule: `0 8 * * *`, time zone **Europe/Sofia**.

**Body:**

```json
{
  "ref": "master",
  "inputs": {
    "mode": "quotes",
    "private": "false"
  }
}
```

### One-off private test from cron-job.org

Use **Run now** on a throwaway cron job (or a dedicated “publish private test” job) with:

```json
{
  "ref": "master",
  "inputs": {
    "mode": "all",
    "private": "true"
  }
}
```

Boolean workflow inputs must be the strings `"true"` or `"false"` in JSON.

## Headers (all cron-job.org publish jobs)

| Name | Value |
|------|-------|
| `Authorization` | `Bearer YOUR_TOKEN` |
| `Accept` | `application/vnd.github+json` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |
