# METR Time Horizons — Webpage Change Monitor

A GitHub Action that checks [https://metr.org/time-horizons/](https://metr.org/time-horizons/) once a day for changes and publishes the results to an RSS feed.

## What it does

Every day at 09:00 UTC the workflow:

1. **Fetches** the METR Time Horizons page.
2. **Extracts** the "LAST UPDATED" date from the page header (primary signal).
3. **Hashes** the visible text and raw HTML as fallback signals.
4. **Compares** all three signals against the previous run's snapshot.
5. **Appends** an RSS item titled either **`CHANGED`** or **`UNCHANGED`** (or **`INITIAL`** on first run).
6. **Commits** the updated snapshot and RSS feed back to the repo.

### Detection layers (most-specific → broadest)

| Layer | What it checks | Catches |
|-------|---------------|---------|
| `last_updated_date` | The text inside `<span class="post-date">` within `<div class="header-date">` | The exact update the page authors intend to signal |
| `text_hash` | SHA-256 of all visible text on the page | Any content change, even if the date field isn't updated |
| `html_hash` | SHA-256 of the entire raw HTML response | Structural/code changes that don't affect visible text |

## Setup

1. **Create a new repo** and copy this project's contents into it.

2. **Enable GitHub Pages** so the RSS feed is publicly accessible:
   - Go to **Settings → Pages**.
   - Set **Source** to `Deploy from a branch`.
   - Set the branch to `main` and the folder to `/docs`.
   - Save. Your feed will be at `https://<you>.github.io/<repo>/feed.xml`.

3. **Trigger the first run** manually:
   - Go to **Actions → "Check Webpage for Changes"** → **Run workflow**.
   - This creates the initial snapshot and seeds the RSS feed.

4. **Subscribe** to the RSS feed URL in your reader of choice.

## RSS feed

The feed lives at `docs/feed.xml` and is served by GitHub Pages. Each item includes:

- **Title:** `CHANGED — https://metr.org/time-horizons/`, `UNCHANGED — …`, or `INITIAL — Monitoring started`
- **Description:** Details about what changed (e.g. the old and new "Last Updated" date).
- **Link:** Always points to the monitored page.
- **pubDate:** When the check ran.

Up to 90 items are retained (~3 months).

## Manual trigger

You can run the check at any time from **Actions → Run workflow**, or via the GitHub CLI:

```bash
gh workflow run check-page.yml
```

## Files

```
.github/workflows/check-page.yml   — the workflow definition
scripts/check_page.py              — change detection + RSS logic
data/snapshot.json                  — last-seen page state (auto-generated)
docs/feed.xml                      — the RSS feed (auto-generated)
```
