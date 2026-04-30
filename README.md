# METR Benchmark Results YAML Monitor

A GitHub Action that checks [`https://metr.org/assets/benchmark_results_1_1.yaml`](https://metr.org/assets/benchmark_results_1_1.yaml) once a day for changes and publishes the results to an RSS feed.

## What it does

Every day at 09:00 UTC the workflow:

1. **Fetches** METR's `benchmark_results_1_1.yaml` file.
2. **Parses** the YAML into a structured data object.
3. **Normalizes and hashes** the parsed YAML so formatting, comments, and key-order-only changes do not look like data changes.
4. **Also hashes** the raw YAML text so formatting/comment-only edits can still be reported.
5. **Compares** the current snapshot against the previous successful run.
6. **Appends** an RSS item titled `CHANGED`, `UNCHANGED`, or `INITIAL`.
7. **Includes a structural diff** in the RSS item when parsed YAML data changed.
8. **Commits** the updated snapshot and RSS feed back to the repo.

## RSS entries

The feed lives at `docs/feed.xml` and is served by GitHub Pages. Each item includes:

- **Title:**
  - `CHANGED — parsed YAML data changed`
  - `CHANGED — raw YAML text changed`
  - `UNCHANGED — YAML unchanged`
  - `INITIAL — Monitoring started`
- **Description:** a summary of what changed since the last check.
- **Link:** the monitored YAML URL.
- **pubDate:** when the check ran.

When parsed YAML data changes, the RSS description includes lines like:

```text
Detected 3 structural YAML change(s) since the last check: 1 added, 2 changed.

Details:
+ $.results[name="new_result"] = {mapping with 5 key(s): ...}
~ $.results[name="existing_result"].score: 0.72 → 0.74
~ $.metadata.updated_at: "2026-04-15" → "2026-04-30"
```

Up to 90 RSS items are retained, which is roughly 3 months of daily checks.

## Detection signals

| Signal | What it checks | What it catches |
|--------|----------------|-----------------|
| `data_hash` | SHA-256 of a canonical JSON representation of the parsed YAML | Meaningful YAML data changes |
| `raw_hash` | SHA-256 of the raw YAML text | Formatting, comments, key order, or other text-only changes |

Parsed YAML data changes are treated as the primary signal. Raw-text-only changes are still reported, but the feed entry explains that parsed data is unchanged.

## Setup

1. **Create a new repo** and copy this project's contents into it.

2. **Enable GitHub Pages** so the RSS feed is publicly accessible:
   - Go to **Settings → Pages**.
   - Set **Source** to `Deploy from a branch`.
   - Set the branch to `main` and the folder to `/docs`.
   - Save. Your feed will be at `https://<you>.github.io/<repo>/feed.xml`.

3. **Trigger the first run** manually:
   - Go to **Actions → "Check YAML for Changes"** → **Run workflow**.
   - This creates the initial snapshot and seeds the RSS feed.

4. **Subscribe** to the RSS feed URL in your reader of choice.

## Manual trigger

You can run the check at any time from **Actions → Run workflow**, or via the GitHub CLI:

```bash
gh workflow run check-page.yml
```

## Files

```text
.github/workflows/check-page.yml   — the workflow definition
check_page.py                      — YAML fetching, diffing, and RSS logic
data/snapshot.json                 — last-seen YAML state (auto-generated)
docs/feed.xml                      — the RSS feed (auto-generated)
```

## Notes

- The script stores the previous parsed YAML data inside `data/snapshot.json` so it can describe exactly what changed on the next run.
- The first run after switching from the old webpage monitor is treated as an initial YAML snapshot, even if an old webpage snapshot is still present.
- The structural diff is capped in the RSS entry to keep feed items readable; the entry says how many additional changes were omitted if it has to truncate.
