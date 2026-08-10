# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Watches Cinema City Prague showings (default: *Odyssea* in the 70mm/IMAX hall at
cinema 1052 / Flora), counts free seats, and emails when something becomes
buyable — a new date appears, or seats free up in a sold-out screening.

## Commands

```powershell
.\run-local.ps1                            # one check
.\run-local.ps1 --watch --interval 600     # loop every 10 min
.\run-local.ps1 --test-email               # verify SMTP only, then exit
.\run-local.ps1 --dump-raw dump\           # save raw API responses for debugging
.\run-local.ps1 --log-level DEBUG
```

**Always use `run-local.ps1`, never `python cinema_watcher.py` directly.** The
wrapper redirects state and log into the gitignored `local/` directory. A direct
run writes `cinema_watcher_state.json`, which GitHub Actions commits to the repo
— that causes pull conflicts *and* makes the local run "eat" changes the
scheduled run should have emailed about. The wrapper also loads `local\.env`
(`KEY=value` lines) for SMTP credentials.

There is no build, no dependency install, no test suite, and no linter. The
script is pure Python stdlib (3.8+); the Netlify functions are dependency-free
ESM on Node 18+. Validate changes with:

```powershell
node --check netlify\functions\toggle.mjs      # any .mjs
python -c "import yaml; yaml.safe_load(open('.github/workflows/cinema-watcher.yml', encoding='utf-8'))"
```

## Architecture

Three parts that each run somewhere else:

| Part | Runs on | Does |
|---|---|---|
| `cinema_watcher.py` | anywhere | the actual check, diffing, and email |
| `.github/workflows/cinema-watcher.yml` | GitHub Actions | cron, holds state, sends mail |
| `web/` + `netlify/` | Netlify | read-only viewer + remote control buttons |

**Email is only ever sent by the Python script running in Actions.** The website
never sends anything — that would put the SMTP password in a browser. The site's
buttons only ask GitHub to run the workflow.

### State lives in git — this drives most of the design

Actions runners are ephemeral, but diffing "what's new" needs the previous run's
data. So the workflow commits `cinema_watcher_state.json` (keyed by event_id)
and `web/data/status.json` back into the repo after every run. Consequences that
are easy to break:

- The workflow needs `permissions: contents: write` and a `concurrency` group, so
  two runs don't race on the same file. Its push step retries with
  `git pull --rebase` up to three times.
- Those commits use `GITHUB_TOKEN`, which by design does not trigger further
  workflows — no infinite loop.
- Local runs must stay out of that file (see `run-local.ps1` above).
- Expect to rebase before pushing: the bot may have committed since you pulled.

### Two data paths to the website

`web/config.js` picks between them, and both are supported:

1. **`/api/status`** (default) — `netlify/functions/status.mjs` reads
   `web/data/status.json` straight from the GitHub API, so the page shows fresh
   data without a redeploy. Works with a private repo.
2. **Deployed file fallback** — the page reads `data/status.json` shipped with
   the site. Used automatically when the function is missing or errors.

`netlify.toml` has an `ignore` rule that cancels the build when *only*
`web/data` changed. Without it, the workflow's state commits would each trigger
a deploy and burn the monthly build minutes. This means: **a change to
`web/data/status.json` alone will not deploy.** Touch something under `web/`
or `netlify/` if you need a deploy.

### Netlify functions

Functions API v2: default-export a handler, route via
`export const config = { path: "/api/…" }`. No dependencies — Node 18+ has
`fetch` built in. All three share `netlify/lib/github.mjs` (config from env,
`ghFetch`, `latestRun`, `workflowState`).

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | data + last run + workflow enabled/disabled state |
| `POST /api/check` | `workflow_dispatch` — run a check now |
| `POST /api/toggle` | enable/disable the workflow (pause the watcher) |

Env vars (Netlify → Site configuration): `GH_REPO`, `GH_TOKEN` (fine-grained PAT
with *Actions: Read and write* + *Contents: Read-only*), `GH_BRANCH`,
`GH_WORKFLOW`, `TRIGGER_PASSWORD`. Without `GH_REPO`/`GH_TOKEN` the functions
return 501 and the page falls back to the deployed file. Netlify functions only
pick up env var changes on a **new deploy**.

`TRIGGER_PASSWORD` guards both mutating endpoints; the page prompts for it and
keeps it in `sessionStorage`. Unset means anyone with the URL can trigger or
disable the watcher.

**A disabled workflow ignores `workflow_dispatch` too**, not just cron. That's
why the page greys out *Skontrolovať teraz* while paused rather than letting the
click fail.

### Scheduling gotchas

Cron is **always UTC and has no DST**. The schedule is Tuesdays every 10 min,
07:00–12:00 Prague, which needs two cron lines (`*/10 5-9 * * 2` stops at 11:50;
`0 10 * * 2` adds noon). Corrected winter-time lines sit in a comment beside the
schedule — bump both hours by one when the clocks change.

`staleAfterMinutes` in `web/config.js` must track the cron cadence. It is 8 days
because the cron is weekly; a shorter value would show a false "data is stale"
warning six days out of seven.

Also: GitHub disables cron after 60 days of repo inactivity (the workflow's own
commits don't count), and it only indexes `.github/workflows/` on push *while
Actions is enabled* — a workflow pushed while Actions was off stays invisible
until the next push.

### Seat counting

`free_bookable` is what matters, not `free_seats`: wheelchair spaces are
subtracted so alerts don't fire on seats nobody can normally buy. Both need a
per-auditorium capacity map, since the API returns an availability *ratio*, not
counts. Defaults live in `DEFAULT_CAPACITY` / `DEFAULT_WHEELCHAIR`
(`cinema_watcher.py`), overridable via `CINEMA_CAPACITY` /
`CINEMA_WHEELCHAIR` as JSON, e.g. `{"IMAX VOLVO": 384}`. An unknown auditorium
yields empty seat counts.

Email fires on change kinds `new` / `freed` / `more` (`TICKET_KINDS`); a
`removed` screening alone does not, unless `--mail-on all`.

## Conventions

- **Code comments, docstrings, log messages, README, and all UI text are in
  Slovak.** Match it. Commit messages are English Conventional Commits.
- All configuration is environment variables with defaults at the top of
  `cinema_watcher.py`; CLI flags override. In Actions, tracking settings are
  repo **Variables** (`CINEMA_*`, `SMTP_SECURITY`) and credentials are
  **Secrets** (`SMTP_*`, `MAIL_*`).
- `.claude/settings.local.json` is tracked but deliberately left uncommitted.
