# SWG Uprising Resource Watch

Discord alerts for the best currently active resource **per stat and exact resource class** on SWG Uprising (Pre-CU). Includes a persistent archive of imported and observed resource stats, even after despawn.

## Free cloud setup

Runs on GitHub Actions every 30 minutes (at minutes 17 and 47). Your PC can be off. Standard GitHub-hosted runners in a **public repository** are free under GitHub's current policy. This is a scheduled monitor, not a continuously connected Discord application. Schedules can be delayed; this is not instant notification or guaranteed uptime.

1. Create a **public** GitHub repository named `uprising-resource-watch`. Upload this folder's contents to its default branch. Include `.github/workflows/resources.yml`, `monitor.py`, `config.json`, `tests/`, and `data/`.
2. In Discord, create a normal text channel such as `#resource-alerts`. Open **Edit Channel > Integrations > Webhooks > New Webhook**, name it `Uprising Resource Watch`, select that channel, and copy its webhook URL. You need Manage Webhooks permission.
3. In your GitHub repository, open **Settings > Secrets and variables > Actions > New repository secret**. Name it `DISCORD_WEBHOOK_URL`, paste the webhook URL as the value, and save. Keep the URL out of source files, screenshots and chat: it allows posting in the channel.
4. Open **Actions > Uprising resource watch > Run workflow**. Leave `import_history` unchecked when the included history archive is present. Check it for a fresh import or if starting without the included archive.
5. Confirm the workflow succeeds and the channel receives one setup message. Subsequent runs alert only when active leaders change. If Actions is disabled, enable it in the repository settings. The workflow requests repository content write permission to save its archive.

The monitor has not been connected to a Discord channel until the secret is added and a workflow runs successfully.

## What it remembers

- `data/state.json`: resource names, classes, all reported stats, planets, active/inactive state, first observation, current winners, and pending Discord alerts. **Keep this file** to retain the history.
- `data/records.md`: readable table of active and historical bests, with SWGAide links and active/despawned status. GitHub renders it as a table.
- Previously despawned resources can be seeded from SWGAide's public history search. Coverage is limited to records reported on that site; it cannot reconstruct missing reports or every resource since the server launched.
- Resources disappearing from two distinct exports are marked inactive. Their stats remain in the archive. Their exact despawn time is unknown; the monitor records when absence was confirmed.

## Alert rules

- All eleven stats are tracked: ER, CR, CD, DR, FL, HR, MA, PE, OQ, SR and UT.
- Each exact resource class is compared separately, e.g. Axidite Iron against Axidite Iron, rather than all iron or all minerals.
- Highest raw value wins. These are **stat leaders**, not crafting-specific scores or cap-normalized ratings.
- One resource winning multiple stats produces one grouped alert.
- Ties keep the existing active winner to avoid noise. A new equal-value resource does not generate a tie-only alert. If the current winner despawns, a tied replacement is announced.
- A resource can become active best with lower stats when the previous leader despawns. The message labels this as a replacement and still shows the historical best.
- Resource types or stats absent from the current feed cannot have a harvestable winner. Resources with no reported planets are excluded from active rankings.
- A first run sends one setup message, not hundreds of alerts. The initial records are visible in `data/records.md`.
- Discord alert titles and the setup link open **SWGAide Home**. Home remembers its server selection through the site form; its `?server=182` URL parameter does not preselect Uprising. Choose **SWG Uprising [precu]** in the Home server selector when needed.
- To post a current shortlist, open **Actions > Uprising resource watch > Run workflow** and check **Post a current strong-stat resource shortlist to Discord**. This queues one rich message with up to 12 different resource types, all stats, planets, age, and historical comparisons. Picks lead their exact type in one or more stats with a **SWGAide quality percentage of 95%+**. More qualifying stats rank first, with SWGAide's displayed average rating breaking ties. This is a harvest shortlist, not a universal crafting or recipe ranking. It uses the percentages displayed on SWGAide (relative to resource type ranges, not simply raw stat divided by 1000) and skips cards whose stats do not match the current XML data.
- The optional `intro_message_id` workflow input updates an existing setup message posted by this webhook to use the Home link. It does not change other members' messages.
- Historical stats are retained, and current resource stat corrections update their records. They are not treated as immutable high-water marks when source values are corrected.

## Reliability and free-plan limits

- The source is SWGAide's documented Uprising XML export (server **182**), not a page scraper for regular checks. SWGAide aggregates upstream/user reports; in-game spawn changes may take time to appear. The Galaxy Harvester ID **161** is a different site's numbering.
- Rejects stale exports, wrong server data, empty feeds, time reversals and sudden feed-size drops greater than 50%, preserving the last good state. A rejected check fails the Actions run; review GitHub failure notifications.
- Pending alerts and the archive are committed **before** delivery, then delivery progress is committed. Failed sends can be retried on a later run. A crash after Discord accepts a message but before delivery progress reaches GitHub can cause a duplicate; exactly-once delivery across the two services is not guaranteed.
- Discord mentions are disabled. The webhook can only post; no bot token, gateway process, slash commands or read access to your Discord chat is needed.
- The code, reported resource archive and report are public. The webhook is stored in a GitHub Actions secret and never written to these files.
- GitHub may disable scheduled workflows after 60 days without repository activity. Normal archive commits produce activity, but check the Actions page if alerts stop. Keep GitHub failure notifications enabled. Free hosting policies can change.
- This workflow uses standard `ubuntu-latest` runners, no paid hosting, no large runners, no external database, no cache or artifact storage. It refuses to run in a private repo to avoid using a paid minutes allowance inadvertently.

## Configuration

Edit `config.json`. An empty `resource_types` list means every exact class; populate it with exact SWGAide class names to narrow alerts. Remove unwanted stat abbreviations from `stats` to reduce alerts. Adding a new stat or class after initialization can trigger an initial leader alert for that category.

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
- [SWGAide data exports](https://swgaide.com/resources/exports.php)
- [SWGAide history search](https://swgaide.com/resources/find.php?server=182)
- [Discord webhooks](https://docs.discord.com/developers/resources/webhook)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [GitHub scheduled workflow limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
