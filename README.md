# Roblox Intel

A personal market-intelligence bot for a Roblox developer. Once a day, on a free GitHub
Actions schedule, it:

1. **Tracks trends.** It snapshots Roblox's Charts (Top Trending, Up-and-Coming, Top Playing Now, ...)
   and the stats of every game on them: players, visits, favorites, likes, genre, creation and update dates.
   It then reports the fastest-growing games over 24h, 7d and 30d, which genres are gaining or losing
   players, and new games (under 60 days old) that broke out.
2. **Tracks top developers.** It watches the studios and users in `config/developers.yml`: new games they
   release, updates to their existing games, and how their player counts move. Examples: "Tycoon Bros
   released a new game: Fresh Hit (Simulation)" or "RP Inc updated Roleplay Town on Mar 2; players
   rose 60% in a week".
3. **Monitors platform changes.** It reads the DevForum Announcements and Release Notes, the Roblox
   Newsroom, Roblox's official Creator Docs (monetization, DevEx, discovery and policy pages) and the
   DevEx help article. It summarizes each new item in 2–3 sentences and rates how much it matters to a
   solo developer trying to grow a game and earn more (high / medium / low).

You get a short **Discord message** every day and a **dashboard on GitHub Pages** with player
trends, genre trends, and logs of developer moves and platform changes. History is stored as
JSON in this repo (`data/`). Nothing here costs money.

---

## Setup (about 10 minutes)

### 1. Put the code in a GitHub repository

Create a new repository on GitHub (for example `roblox-intel`) and push this folder to it:

```bash
git remote add origin https://github.com/<you>/roblox-intel.git
git push -u origin main
```

Make it **public** if you want the free GitHub Pages dashboard. Pages on private repositories
needs a paid GitHub plan. The data in it is public Roblox data, and your Discord webhook stays in
a secret, never in the code.

### 2. Create the Discord webhook

1. In Discord, open the server and channel where the daily brief should go.
2. **Edit Channel** (gear icon) → **Integrations** → **Webhooks** → **New Webhook**.
3. Name it (e.g. "Roblox Intel") and click **Copy Webhook URL**.
   It looks like `https://discord.com/api/webhooks/123.../abc...`. Treat it like a password.

### 3. Add the webhook as a GitHub secret

In your repository: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

* Name: `DISCORD_WEBHOOK_URL`
* Secret: the webhook URL you copied

The workflow reads it at run time. It is never written to the repo, the logs or the dashboard.

### 4. Enable GitHub Pages

**Settings** → **Pages** → **Build and deployment** → **Source: GitHub Actions**.

Your dashboard will be at `https://<you>.github.io/<repo>/` after the first run.

### 5. Enable Actions and run it once

1. Open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Pick **Daily Roblox Intel** → **Run workflow**. A full run takes a few minutes. Untick
   "Send the Discord brief" for a test run that shouldn't post to Discord.
3. Check your Discord channel and the Pages URL.

After that it runs on its own every day at 14:17 UTC. To change the time, edit the `cron:` line in
`.github/workflows/daily.yml` (cron is always UTC; `17 14 * * *` = 14:17).

If the "Commit history" step fails with a permission error, go to **Settings** → **Actions** →
**General** → **Workflow permissions** and choose **Read and write permissions**.

### AI summaries (free, nothing to set up)

Platform posts are summarized by a **small open AI model that runs on the GitHub Actions machine
itself**. The workflow installs [Ollama](https://ollama.com) and downloads the model
(`qwen2.5:3b`, with `llama3.2:3b` as a backup). There is no account, API key or bill, and no outside
AI service that could change its terms. Public repos get unlimited Actions minutes. This adds a
couple of minutes to each run.

* A small model on a CPU is slower and plainer than a big hosted one, so AI time per run is capped
  (15 minutes by default). Anything over the cap gets a keyword summary today and an AI summary on
  the next run.
* If the model can't start, or its replies can't be used, the run falls back to a built-in keyword
  summarizer. The source-health section says why. The next run that has AI again upgrades the last
  14 days of keyword summaries in place.
* **What counts as "high":** small models call nearly everything important, so a "high" only stands
  when the post itself shows a money, reach or policy change (DevEx, payouts, fees, pricing, discovery,
  policy, shutdowns). For Creator Docs changes only the words that actually changed count: an amount or
  threshold that changed, or a rule or deadline that was added or removed. Rewording, moved text and
  renamed menus don't, and pages about ads tools, creator programs and avatar-item (UGC) rules stop at
  medium. The dashboard shows the reason under each item.
* **Optional, better quality:** point `summarizer.hosted` in `config/settings.yml` at any
  OpenAI-compatible chat API you have a key for (several providers have free tiers, which can change),
  and add the key as a repo secret named `AI_API_KEY`. With `provider: auto`, the local model still
  goes first.
* History: this project first used GitHub Models, GitHub's free AI tier, but GitHub retired it on
  July 30, 2026. It now only returns a placeholder page, which is why summaries run locally.

To turn AI off, set `summarizer.provider: none` in `config/settings.yml`.

---

## Editing the tracked developers

Open `config/developers.yml`. Each entry is one developer, written in any of three ways:

```yaml
developers:
  - game: https://www.roblox.com/games/2753915549/Blox-Fruits   # track whoever owns this game
    expect: Blox Fruits                                          # optional safety check

  - group: 1234567          # a community/group ID, from roblox.com/communities/1234567/...
    name: Some Studio       # optional label

  - user: 7654321           # a user ID, from roblox.com/users/7654321/profile
    name: Some Developer
```

* **To add someone**, paste a link to any of their games (easiest), or their group or user ID.
* **To remove someone**, delete their lines. Their history stays in `data/`.
* Owners are looked up from game links on every run and re-checked weekly, so acquisitions show up.
  If a link stops matching its `expect` name, the entry is skipped and flagged in the run's source
  health instead of silently tracking the wrong studio.
* The default list points at the studios behind the biggest games of 2025–26.
* **Suggestions:** the dashboard's "Studios you might add" panel lists the owners of today's top games
  that you don't track yet, as ready-to-paste YAML. The command below prints the same list.

  ```bash
  python -m roblox_intel suggest-developers
  ```

Commit the file and the next run picks it up.

---

## What's in the reports

**Discord (daily)**: four short sections.

* 📈 **Trends**: the five most telling facts of the day, for example the biggest 24h, 7d and 30d growers,
  the top new breakout, and the genre moving most.
* 🏢 **Developer moves**: new releases, updates of big games, a week-later "update impact", player swings
  of 25% or more, and ownership changes.
* 🛠️ **Platform changes**: only the **high**-importance items (configurable), with a link and summary.
* ⚠️ **Data source issues**: only appears when something failed, with the fallback that was used.

**Dashboard**: KPI tiles; today's brief; a player-trend chart (pick up to 6 games from the top-games
table, as raw players or indexed growth); growth / most-added / declining tables for the selected
window; new breakouts; genre momentum and genre trends; tracked developers with sparklines and a
filterable log of their moves; the platform-changes log (filter by importance and source); data-source
health; and suggestions. Every chart has a table view, and it works in light and dark mode.

### How the numbers work (and their limits)

* **Players** are concurrent players (CCU) at the moment of the daily snapshot. Comparing day to day at
  the same time is fair, but weekends and Roblox events still move numbers.
* **Visits/day** is the change in total visits between snapshots: a steadier measure of real traffic.
* **Growth windows** need history: 24h from the second run, 7d after a week, 30d after a month. Until
  then the report leans on Roblox's own charts and says when each window unlocks. The project does not
  backfill or invent history.
* **Genre changes** compare the same games on both days, so a game entering the charts doesn't look like
  genre growth.
* **Fastest-growing** % rankings ignore games under 1,000 players (configurable) to avoid noise like
  "+900% from 10 players".
* **New games** use the universe's creation date. A studio sometimes creates a place long before it
  launches, so "made an older game public" is reported separately.
* Games stay tracked for 30 days after leaving the charts, so the history doesn't vanish when a game
  drops off a list.

---

## Data sources

| Part | Source | Notes / fallback |
|---|---|---|
| Charts | `apis.roblox.com/explore-api/v1/get-sorts` + `get-sort-content` (the public API behind roblox.com/charts) | If unavailable: keeps refreshing every game it already tracks, and reports the outage |
| Game stats | `games.roblox.com/v1/games`, `/v1/games/votes` | Required for a snapshot. If down, that day has no snapshot; the run says so and exits non-zero (GitHub emails you) |
| Developer games | `games.roblox.com/v2/groups/{id}/gamesV2` (falls back to `/games`), `/v2/users/{id}/games` | Public games only |
| Owner lookup | `apis.roblox.com/universes/v1/places/{id}/universe`, `groups.roblox.com`, `users.roblox.com` | Cached for a week |
| Icons | `thumbnails.roblox.com/v1/games/icons` | Dashboard only |
| DevForum | Discourse JSON for `updates/announcements` (36) and `updates/release-notes` | Category ID auto-resolved; RSS fallback; last resort: "Updates" filtered by title |
| Newsroom | `about.roblox.com/newsroom` (Roblox's official blog) | Feed autodiscovery → links on the page → `sitemap.xml` |
| Creator Docs | `github.com/Roblox/creator-docs` (the official source of create.roblox.com/docs) | Diffs of monetization/DevEx, discovery/promotion and publishing/policy pages; formatting-only edits are ignored |
| DevEx FAQ | Help Center article 203314100 (Zendesk API) | HTML fallback; reports when its text changes |
| Summaries | Local open model via Ollama on the runner; optional hosted API | Keyword summarizer fallback; later upgrade |

All of these are public endpoints that don't need a login. Nothing behind a login is scraped.

**About the "Roblox Creator blog":** Roblox doesn't run a separate creator blog any more. The
official long-form posts live in the **Roblox Newsroom** (about.roblox.com/newsroom, which replaced
blog.roblox.com and corp.roblox.com/newsroom). Creator-facing announcements live on the
**DevForum Announcements** category. Both are monitored. Rule changes on DevEx rates, payouts,
discovery and policy usually also land as edits to the **official Creator Docs**, which the bot
diffs. That catches quiet changes that never get a blog post.

### Rate limits and etiquette

A daily run makes roughly 100–300 requests (the first run a few more). Every request goes through one client that:

* waits at least 1 second between requests to the same host (2s for the DevForum and Newsroom);
* retries 429/5xx and network errors up to 5 times with exponential backoff and jitter, honouring
  `Retry-After`;
* caches responses on disk (6h by default; a week for owner lookups and icons) and revalidates with
  ETag / Last-Modified. A re-run on the same day is nearly free.

To check every source by hand:

```bash
python -m roblox_intel check-sources
```

This prints OK/FAIL per source with a sample of what came back. The workflow runs it before each
daily run, so the Actions log always shows source health.

---

## Running locally

Python 3.10+.

```bash
pip install -r requirements.txt
python -m roblox_intel check-sources            # test every data source
python -m roblox_intel run --no-discord         # full pipeline without sending
python -m http.server -d _site 8000             # dashboard at http://localhost:8000
DISCORD_WEBHOOK_URL=... python -m roblox_intel discord-test   # check the webhook
python -m roblox_intel report --discord-json    # print the latest report + Discord payload
```

Summaries use the keyword summarizer locally unless you run [Ollama](https://ollama.com) yourself
(`ollama pull qwen2.5:3b`). The summarizer picks it up automatically at `http://127.0.0.1:11434`.

## Settings

`config/settings.yml` is commented and covers: thresholds (minimum players for rankings, "new game"
age, breakout size, big-move %), how many charts to read, request pacing and cache, the platform
sources and which docs folders count as monetization/discovery/policy, the summarizer, Discord
limits, and the dashboard history length.

## Repository layout

```
config/            settings.yml, developers.yml       <- the two files you edit
roblox_intel/      the pipeline (python -m roblox_intel ...)
  roblox.py        Roblox API wrappers            trends.py      snapshot + growth analysis
  developers.py    watch list, releases, updates  platform.py    official sources monitor
  summarize.py     local/hosted AI + keyword rubric report.py   daily report
  discord.py       webhook message                dashboard.py   static site data
  http.py          throttle / retry / cache       storage.py     JSON history
site/              dashboard (plain HTML/CSS/JS, no build step, no CDN)
data/              history, committed by the workflow every day
.github/workflows/ daily.yml (schedule) and tests.yml
tests/             unit tests + a 10-day end-to-end run against local mock services
```

History grows by roughly 100–300 KB of JSON per day (one snapshot file plus small logs), so a few
tens of MB per year before git's compression. To keep it smaller, lower `trends.charts.max_sorts` or
`trends.keep_tracking_days` in the settings.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The end-to-end test runs the real CLI for 10 simulated days against local stand-ins for every
service (Roblox APIs, DevForum, Newsroom, Help Center, the local AI model, Discord) and a local git repo
for the docs. It covers growth windows, new releases, update impact, outages and fallbacks, Discord
limits and the dashboard. Its data is synthetic and lives only in a temp folder.

## Troubleshooting

* **No Discord message:** check the run's "Data sources" summary. `DISCORD_WEBHOOK_URL is not set`
  means the secret name is wrong; "webhook was deleted" means you need a new webhook URL.
* **Pages deploy step fails:** Pages isn't set to "GitHub Actions" (setup step 4). The data commit and
  Discord message still happen.
* **A developer shows as skipped:** the game link now points to a different game than `expect`. Fix the
  link or the text.
* **Scheduled runs stopped:** GitHub pauses schedules in repos without activity for 60 days. The daily
  data commit normally prevents that. If it ever happens, re-enable the workflow from the Actions tab.
* **Roblox changed an API:** `check-sources` shows which source broke. Each source has a fallback, and
  the run keeps going and reports the problem rather than inventing data.
