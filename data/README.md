# data/

History written by the daily GitHub Actions run. Plain JSON, one record per line, so diffs stay readable.

| Path | What it holds |
|---|---|
| `snapshots/YYYY-MM-DD.json` | One file per day: the Charts sorts (ranked universe ids) and players, visits, favorites, likes and dislikes for every tracked game |
| `games.json` | Catalog: latest name, owner, genre, created/updated dates, icon and first/last seen dates per game |
| `events.json` | Developer moves: new games, updates, renames, update impact, player swings, owner changes |
| `platform.json` | Platform changes: every summarized post or docs change with its importance rating |
| `reports/YYYY-MM-DD.{json,md}` | The daily report exactly as sent to Discord |
| `health.json` | Status of every data source on the last run, plus request counts |
| `state/*.json` | Bookkeeping: resolved developer list, each developer's known games, seen posts per source |

Delete a file under `state/` to make that part start fresh on the next run.
