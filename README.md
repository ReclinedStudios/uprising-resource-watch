# SWG Uprising Resource Watch

Discord alerts for **NEW SERVER BEST** resources on SWG Uprising (Pre-CU), ranked by **SWGAide overall rating**, with **one winner per broad category** (Wood, Copper, Steel, Fruit, etc.). Comparisons include despawned resources.

## Free cloud setup

Runs on GitHub Actions every 30 minutes (at minutes 17 and 47). Your PC can be off. Standard GitHub-hosted runners in a **public repository** are free under GitHub's current policy. This is a scheduled monitor, not a continuously connected Discord application. Schedules can be delayed; this is not instant notification or guaranteed uptime.

1. Create a **public** GitHub repository named `uprising-resource-watch`. Upload this folder's contents to its default branch. Include `.github/workflows/resources.yml`, `monitor.py`, `resource_rating.py`, `resource_classes.json`, `galaxy_harvester.py`, `config.json`, `tests/`, and `data/`.
2. In Discord, create a normal text channel such as `#resource-alerts`. Open **Edit Channel > Integrations > Webhooks > New Webhook**, name it `Uprising Resource Watch`, select that channel, and copy its webhook URL. You need Manage Webhooks permission.
3. In your GitHub repository, open **Settings > Secrets and variables > Actions > New repository secret**. Name it `DISCORD_WEBHOOK_URL`, paste the webhook URL as the value, and save. Keep the URL out of source files, screenshots and chat: it allows posting in the channel.
4. Open **Actions > Uprising resource watch > Run workflow**. Leave `import_history` unchecked when the included history archive is present. Check it for a fresh import or if starting without the included archive.
5. Confirm the workflow succeeds and the channel receives one setup message. Subsequent runs alert only when an active resource beats its broad category’s historical rating record. If Actions is disabled, enable it in the repository settings. The workflow requests repository content write permission to save its archive.

The monitor has not been connected to a Discord channel until the secret is added and a workflow runs successfully.

## What it remembers

- `data/state.json`: resource names, classes, all reported stats, planets, active/inactive state, first observation, current winners, and pending Discord alerts. **Keep this file** to retain the history.
- `data/records.md`: readable table of active and historical bests, with SWGAide links and active/despawned status. GitHub renders it as a table.
- Previously despawned resources can be seeded from SWGAide's public history search. Coverage is limited to records reported on that site; it cannot reconstruct missing reports or every resource since the server launched.
- Resources disappearing from two distinct exports are marked inactive. Their stats remain in the archive. Their exact despawn time is unknown; the monitor records when absence was confirmed.

## Alert rules

- One historical winner per broad category. All planetary Wood subtypes compete together; all Fruit subtypes compete together. Copper and Steel stay separate. The class hierarchy comes from [SWGAide Resource Class Info](https://swgaide.com/resources/restree.php).
- Ranking uses SWGAide’s displayed overall **Rate**, based on all reported stats normalized to their class caps. Each normalized stat is truncated to an integer on the 0–1000 scale, then their average is rounded half up. This matched all 454 displayed Uprising resource cards checked on 2026-10-22.
- Only a **strictly higher overall rating** than every imported/observed resource in that category triggers an alert. Individual stat records, ties and despawns are silent. Lower-rated active replacements never trigger alerts.
- Records include despawned resources. SWGAide history is supplemented daily with complete Galaxy Harvester Uprising history records missing from the archive. Missing/unreported history cannot be reconstructed. GH-only entries are historical comparisons; SWGAide remains the current-spawn source. New active GH-only spawns after migration wait for SWGAide rather than being silently consumed as historical baselines.
- Before each automatic record post, the live Galaxy Harvester Uprising lookup must match its resource name, type and every raw stat, and not mark the resource inactive. Missing or conflicting reports stay queued for later checks; they are never labelled verified or posted prematurely. Superseded or despawned SWGAide candidates are discarded.
- Card percentages use Galaxy Harvester’s `(value - class minimum) / (class maximum - class minimum)` range calculation, rounded to a whole percent. They are labelled separately from SWGAide’s cap-normalized overall rating. A fixed min=max range has no percentage.
- An existing installation switches silently: all current/archive data becomes the baseline, and legacy queued active-best alerts and old summaries are discarded. No catch-up flood. A completely new category is also baselined silently until a later resource beats its first record.
- A fresh installation sends one setup message. The records table shows every category’s historical winner and whether it is active.
- Source stat corrections update the archive. Correcting a record downward does not announce a replacement. A later genuine improvement is compared against the corrected archive.
- Pending alerts are checked again before delivery; superseded, corrected or missing resource winners are discarded. Failed valid sends remain queued.
- Posts use a compact Discord card: resource icon, name/type, overall rating versus the previous record, raw-stat table with Galaxy Harvester range percentages, planets and any reported concentration waypoint. Mentions are disabled.
- Optional manual summaries show at most 25 active historical record holders, one per broad category. They never run on a normal scheduled check.
- Resource title links open the specific resource. Home links use the existing Uprising selector page at https://reclinedstudios.github.io/uprising-resource-watch/.
- Existing message link/intro refresh commands remain available for explicit maintenance; upgrading does not edit old Discord messages.

## Reliability and free-plan limits

- Current spawns come from SWGAide's documented Uprising XML export (server **182**), with bundled SWGAide class caps for calculating ratings. Galaxy Harvester’s public Uprising API (galaxy **161**) supplies extra historical comparisons and live verification. SWGAide aggregates upstream/user reports; in-game spawn changes may take time to appear. The Galaxy Harvester ID **161** is a different site's numbering.
- Rejects stale exports, wrong server data, empty feeds, time reversals and sudden feed-size drops greater than 50%, preserving the last good state. A rejected check fails the Actions run; review GitHub failure notifications.
- Pending alerts and the archive are committed **before** delivery, then delivery progress is committed. Failed sends can be retried on a later run. A crash after Discord accepts a message but before delivery progress reaches GitHub can cause a duplicate; exactly-once delivery across the two services is not guaranteed.
- Discord mentions are disabled. The webhook can only post; no bot token, gateway process, slash commands or read access to your Discord chat is needed.
- The code, reported resource archive and report are public. The webhook is stored in a GitHub Actions secret and never written to these files.
- GitHub may disable scheduled workflows after 60 days without repository activity. Normal archive commits produce activity, but check the Actions page if alerts stop. Keep GitHub failure notifications enabled. Free hosting policies can change.
- This workflow uses standard `ubuntu-latest` runners, no paid hosting, no large runners, no external database, no cache or artifact storage. It refuses to run in a private repo to avoid using a paid minutes allowance inadvertently.

## Configuration

Edit `config.json`. An empty `resource_types` list means every broad category; use names such as `Wood`, `Copper` or `Steel` to narrow alerts. All stats are required for overall ratings. Unknown classes or incomplete stats fail the check without changing the archive; update `resource_classes.json` from the public SWGAide class tree before retrying.

## Local checks (Python 3.11+; no packages needed)

```sh
python -m unittest discover -s tests -v
python monitor.py seed-history
python monitor.py collect
python monitor.py preview
python monitor.py report
```

`collect` and `preview` never post messages. `deliver` is the only command that posts, and requires the `DISCORD_WEBHOOK_URL` environment variable. Use `--state path/to/test-state.json` with commands to avoid changing the production archive while experimenting.

## Sources

- [SWGAide Uprising resources](https://swgaide.com/resources/?server=182)
- [Galaxy Harvester Uprising](https://galaxyharvester.net/ghHome.py/161)
- [SWGAide data exports](https://swgaide.com/resources/exports.php)
- [SWGAide history search](https://swgaide.com/resources/find.php?server=182)
- [Discord webhooks](https://docs.discord.com/developers/resources/webhook)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [GitHub scheduled workflow limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
