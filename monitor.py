"""SWG Uprising active-best monitor. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import gzip
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, parse_qsl, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
STATS = ('ER', 'CR', 'CD', 'DR', 'FL', 'HR', 'MA', 'PE', 'OQ', 'SR', 'UT')
STAT_NAMES = dict(zip(STATS, ('Entangle Resistance', 'Cold Resistance', 'Conductivity',
    'Decay Resistance', 'Flavor', 'Heat Resistance', 'Malleability', 'Potential Energy',
    'Overall Quality', 'Shock Resistance', 'Unit Toughness')))
USER_AGENT = 'UprisingGuildResourceMonitor/1.0 (+https://swgaide.com/)'
HOME_URL = 'https://swgaide.com/'
RECORDS_URL = 'https://github.com/ReclinedStudios/uprising-resource-watch/blob/main/data/records.md'


def utcnow():
    return datetime.now(timezone.utc)


def stamp():
    return utcnow().isoformat(timespec='seconds')


def fetch(url, form=None):
    data = urlencode(form).encode() if form is not None else None
    request = Request(url, data=data, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=45) as response:
        raw = response.read(12_000_001)
    if len(raw) > 12_000_000:
        raise ValueError('Feed exceeds the 12 MB limit')
    if raw.startswith(b'\x1f\x8b'):
        raw = gzip.decompress(raw)
    if len(raw) > 30_000_000:
        raise ValueError('Uncompressed feed exceeds the 30 MB limit')
    return raw


def load_state(path):
    if not path.exists():
        return {'version': 1, 'initialized': False, 'resources': {}, 'leaders': {},
                'outbox': [], 'history_imports': []}
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('version') != 1:
        raise ValueError('Unsupported state version; preserve the existing archive')
    return state


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temporary.replace(path)


def parse_feed(raw, config):
    root = ET.fromstring(raw)
    if root.tag != 'resource_data' or root.get('server_id') != str(config['server_id']):
        raise ValueError('Wrong feed format or server ID; refusing to change active resources')
    if root.get('server_name') != config['server_name']:
        raise ValueError('Wrong server name in feed')
    generated = datetime.strptime(root.attrib['generated'], '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=timezone.utc)
    if utcnow() - generated > timedelta(hours=config['max_feed_age_hours']):
        raise ValueError('SWGAide feed is stale; keeping previous active resources')
    if generated > utcnow() + timedelta(minutes=15):
        raise ValueError('Feed timestamp is in the future')
    resources = {}
    for node in root.findall('./resources/resource'):
        rid = node.attrib['swgaide_id']
        stats = {n.tag.upper(): int(n.text) for n in node.findall('./stats/*')}
        if not stats or any(k not in STATS or v < 0 or v > 1000 for k, v in stats.items()):
            raise ValueError(f'Invalid stats for resource {rid}')
        planets = [n.text.strip() for n in node.findall('./planets/planet/name') if n.text]
        resources[rid] = {'id': rid, 'name': node.findtext('name', '').strip(),
            'type': node.findtext('type', '').strip(), 'type_id': node.findtext('swgaide_type_id'),
            'stats': stats, 'planets': planets, 'available_timestamp': node.findtext('available_timestamp'),
            'url': f'https://swgaide.com/resources/view.php?rid={rid}',
            'active': bool(planets), 'source': 'SWGAide'}
        if not resources[rid]['name'] or not resources[rid]['type']:
            raise ValueError('Resource name or class missing')
    if not resources:
        raise ValueError('Empty feed; keeping previous active resources')
    return generated.isoformat(), resources


def key_for(resource, stat):
    # Compare the exact SWGAide class, e.g. Axidite Iron, rather than all metals.
    return json.dumps([resource['type'].casefold(), stat])


def leaders(resources, config, previous=None, active_only=True):
    groups = defaultdict(list)
    selected = {t.casefold() for t in config.get('resource_types', [])}
    for rid, resource in resources.items():
        if active_only and (not resource.get('active') or resource.get('missing_snapshots', 0)):
            continue
        if selected and resource['type'].casefold() not in selected:
            continue
        for stat in config['stats']:
            if stat in resource['stats']:
                groups[key_for(resource, stat)].append((resource['stats'][stat], rid))
    result = {}
    for key, entries in groups.items():
        best = max(value for value, _ in entries)
        tied = sorted(rid for value, rid in entries if value == best)
        old_id = (previous or {}).get(key, {}).get('id')
        rid = old_id if old_id in tied else tied[0]
        result[key] = {'id': rid, 'value': best, 'tied_ids': tied}
    return result


def text_safe(value):
    return re.sub(r'([\\`*_~|<>])', r'\\\1', str(value)).replace('@', '@\u200b')


def make_alert(resource, changes, state, history):
    fields = []
    for stat, old, current, is_new in changes:
        record = history[key_for(resource, stat)]
        record_resource = state['resources'][record['id']]
        suffix = 'active' if record_resource.get('active') else 'despawned / inactive'
        if record_resource.get('active') and record_resource.get('missing_snapshots'):
            suffix = 'not in latest feed; awaiting confirmation'
        previous = f"{old['value']} ({text_safe(state['resources'][old['id']]['name'])})" if old else 'none tracked'
        reason = 'Newly reported resource' if is_new else 'Active leader changed / stats updated'
        if old and current['value'] < old['value']:
            reason = 'Previous leader left spawn; replacement active best'
        fields.append({'name': f'{stat} - {STAT_NAMES[stat]}', 'inline': False,
            'value': f"**Active best: {current['value']}** | Previous: {previous}\n"
                     f"Historical best: **{record['value']}** - {text_safe(record_resource['name'])} ({suffix})\n{reason}"})
    all_stats = ' | '.join(f'{s} {v}' for s, v in resource['stats'].items())
    return {'username': 'Uprising Resource Watch', 'allowed_mentions': {'parse': []},
        'embeds': [{'title': f"New active best: {resource['name']}"[:256], 'url': HOME_URL,
            'description': f"**{text_safe(resource['type'])}**\nPlanets: {text_safe(', '.join(resource['planets']))}\n{all_stats}",
            'color': 0x32B97C, 'fields': fields,
            'footer': {'text': 'SWG Uprising (Pre-CU) | Historical best = imported + observed SWGAide records'},
            'timestamp': stamp()}]}


def intro_payload(state):
    return {'username': 'Uprising Resource Watch', 'allowed_mentions': {'parse': []},
        'content': f"Resource monitoring enabled for **SWG Uprising (Pre-CU)**. Tracking **{state.get('feed_count', 0)}** current resources and **{len(state['resources'])}** imported/observed records. Alerts compare each stat within the same resource class. Historical stats remain saved after despawn.\n"
                   f"**SWGAide Home:** {HOME_URL}\nChoose **SWG Uprising [precu]** in the Home server selector."}


def collect(state, config, raw):
    generated, current = parse_feed(raw, config)
    if state.get('feed_generated') and generated < state['feed_generated']:
        raise ValueError('Feed moved backwards in time')
    prior_count = state.get('feed_count', 0)
    if prior_count and len(current) < prior_count * config['minimum_feed_size_ratio']:
        raise ValueError('Unexpected large feed drop; retaining previous state for review')
    # Do not count a second poll of the same export as a second missing observation.
    if generated == state.get('feed_generated'):
        return 0
    now = stamp()
    old_leaders = state['leaders']
    old_ids = set(state['resources'])
    for rid, resource in state['resources'].items():
        if resource.get('active') and rid not in current:
            resource['missing_snapshots'] = resource.get('missing_snapshots', 0) + 1
            if resource['missing_snapshots'] >= config['missing_snapshots_before_inactive']:
                resource['active'] = False
                resource['inactive_observed_at'] = now
    for rid, resource in current.items():
        first_seen = state['resources'].get(rid, {}).get('first_seen', now)
        state['resources'][rid] = {**resource, 'first_seen': first_seen, 'missing_snapshots': 0}
    # Keep leaders whose disappearance is still unconfirmed; suppress those alerts for one export.
    eligible = {rid: r for rid, r in state['resources'].items() if r.get('active')}
    eligible = {rid: {**r, 'missing_snapshots': 0} for rid, r in eligible.items()}
    new_leaders = leaders(eligible, config, previous=old_leaders)
    history = leaders(state['resources'], config, active_only=False)
    grouped = defaultdict(list)
    if state['initialized']:
        for key, winner in new_leaders.items():
            old = old_leaders.get(key)
            if old and old['id'] == winner['id'] and old['value'] == winner['value']:
                continue
            rid = winner['id']
            resource = state['resources'][rid]
            if resource.get('missing_snapshots'):
                continue
            stat = json.loads(key)[1]
            grouped[rid].append((stat, old, winner, rid not in old_ids))
    else:
        # One setup message instead of thousands of initial per-stat alerts.
        state['outbox'].append({'kind': 'baseline', 'payload': intro_payload({**state, 'feed_count': len(current)})})
    for rid, changes in sorted(grouped.items()):
        state['outbox'].append({'kind': 'leader', 'resource_id': rid,
            'winner_values': {stat: winner['value'] for stat, _, winner, _ in changes},
            'payload': make_alert(state['resources'][rid], changes, state, history)})
    state.update(initialized=True, leaders=new_leaders, feed_generated=generated,
                 feed_count=len(current), last_successful_collection=now)
    return len(grouped)


class HistoryTable(HTMLParser):
    """Parse only SWGAide's public search-result table, not navigation or forms."""
    def __init__(self):
        super().__init__()
        self.inside = False
        self.rows = []
        self.row = None
        self.cell = None
        self.headers = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table' and attrs.get('id') == 'restree':
            self.inside = True
        if not self.inside:
            return
        if tag == 'tr':
            self.row = {'cells': [], 'id': None, 'active': False}
        elif tag in ('td', 'th'):
            self.cell = ''
        elif tag == 'a' and self.row is not None:
            match = re.search(r'view\.php\?rid=(\d+)', attrs.get('href', ''))
            if match:
                self.row['id'] = match[1]
        elif tag == 'span' and self.row is not None and attrs.get('class') == 'active':
            self.row['active'] = True

    def handle_data(self, data):
        if self.inside and self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if not self.inside:
            return
        if tag in ('td', 'th') and self.row is not None and self.cell is not None:
            self.row['cells'].append(self.cell.strip())
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.row['id']:
                self.rows.append(self.row)
            elif self.row['cells'] and not self.headers:
                self.headers = self.row['cells']
            self.row = None
        elif tag == 'table':
            self.inside = False


class ResourceCards(HTMLParser):
    """Read displayed SWGAide stat values and cap percentages for a manual summary."""
    def __init__(self):
        super().__init__()
        self.cards = {}
        self.table = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self.table = {'rows': [], 'id': None}
        if self.table is None:
            return
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th'):
            self.cell = ''
        elif tag == 'br' and self.cell is not None:
            self.cell += ' '
        elif tag == 'a':
            match = re.search(r'view\.php\?rid=(\d+)', attrs.get('href', ''))
            if match:
                self.table['id'] = match[1]

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if self.table is None:
            return
        if tag in ('td', 'th') and self.cell is not None and self.row is not None:
            self.row.append(self.cell.strip())
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.table['rows'].append(self.row)
            self.row = None
        elif tag == 'table':
            if self.table['id']:
                rows = self.table['rows']
                if len(rows) >= 3 and rows[1] == list(STATS) and len(rows[2]) == len(STATS):
                    stats, percentages = {}, {}
                    for stat, text in zip(STATS, rows[2]):
                        if not text:
                            continue
                        match = re.fullmatch(r'(\d+)\s+(\d+(?:\.\d+)?)%', text)
                        if not match:
                            raise ValueError('Unrecognized SWGAide stat percentage')
                        stats[stat] = int(match[1])
                        percentages[stat] = float(match[2])
                    heading = ' '.join(rows[0])
                    rating = re.search(r'Rate:\s*(\d+)', heading)
                    age = re.search(r'Age:\s*([\w ]+)', heading)
                    self.cards[self.table['id']] = {'stats': stats, 'percentages': percentages,
                        'rating': int(rating[1]) if rating else 0,
                        'age': age[1].strip() if age else 'not reported'}
            self.table = None


def summary_payload(state, config, html):
    parser = ResourceCards()
    parser.feed(html)
    if not parser.cards:
        raise ValueError('No SWGAide resource cards found; summary not posted')
    current = leaders(state['resources'], config, previous=state['leaders'])
    history = leaders(state['resources'], config, active_only=False)
    candidates = []
    for rid, resource in state['resources'].items():
        card = parser.cards.get(rid)
        if not resource.get('active') or resource.get('missing_snapshots') or not card:
            continue
        # Do not combine percentages from an older/corrected page with different XML stats.
        if card['stats'] != resource['stats']:
            continue
        strong = [s for s, p in card['percentages'].items() if p >= 95
            and key_for(resource, s) in current and rid in current[key_for(resource, s)]['tied_ids']]
        if strong:
            candidates.append((len(strong), card['rating'], resource['name'], resource, card, strong))
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    fields, selected_types = [], set()
    for _, _, _, resource, card, strong in candidates:
        if resource['type'] in selected_types:
            continue
        highlights = ' | '.join(f"{s} {card['percentages'][s]:g}%" for s in strong)
        stats = ' | '.join(f'{s} {resource["stats"][s]}' for s in STATS if s in resource['stats'])
        old_records = []
        for stat in strong[:3]:
            best = history[key_for(resource, stat)]
            old = state['resources'][best['id']]
            status = 'active' if old.get('active') else 'despawned'
            old_records.append(f"{stat} {best['value']} ({text_safe(old['name'])}, {status})")
        field = {'name': f"{len(fields)+1}. {resource['name']} - {resource['type']}"[:256],
            'value': f"**Strong leading stats (SWGAide):** {highlights}\n**Stats:** {stats}\n"
                     f"**Planets:** {text_safe(', '.join(resource['planets']))}\n"
                     f"**Age:** {card['age']}\n**Historical best:** {'; '.join(old_records)}",
            'inline': False}
        if len(field['value']) > 1024:
            raise ValueError('Summary resource details exceed Discord field limit')
        if sum(len(f['name']) + len(f['value']) for f in fields + [field]) > 4800:
            break
        fields.append(field)
        selected_types.add(resource['type'])
        if len(fields) == 12:
            break
    if not fields:
        raise ValueError('No matching active leaders with stats at 95% of cap; summary not posted')
    return {'username': 'Uprising Resource Watch', 'allowed_mentions': {'parse': []},
        'content': f"**SWGAide Home:** {HOME_URL}\nChoose **SWG Uprising [precu]** in the Home server selector.\n[Complete active and historical records]({RECORDS_URL})",
        'embeds': [{'title': 'Current resource shortlist - SWG Uprising', 'url': HOME_URL,
            'description': 'Harvest candidates with several strong stats. Highlighted stats lead their own resource type and have a SWGAide quality percentage of at least 95%. More highlighted stats come first; one pick per type. Crafting recipes have different needs, so this is not a universal crafting ranking. All raw stats are shown below.',
            'fields': fields, 'color': 0x42B9AD,
            'footer': {'text': 'Percentages: SWGAide stat quality for this resource type. Historical details show up to 3 highlighted stats.'},
            'timestamp': state['feed_generated']}]}


def seed_history(state, config):
    # Top-level classes on the public site; class 1 is renewable energy.
    classes = {'1': 'Energy', '6': 'Chemical', '35': 'Creature Food', '11': 'Creature Structural',
               '49': 'Flora Food', '50': 'Flora Structural', '7': 'Gas', '8': 'Mineral', '9': 'Water'}
    imported = {}
    for class_id, label in classes.items():
        start = 0
        while True:
            html = fetch('https://swgaide.com/resources/find.php', {
                'server': config['server_id'], 'class': class_id, 'qclass': 'Search by Class',
                'start': start, 'page': start // 50 + 1}).decode('utf-8')
            match = re.search(r'(\d+) Results Found - Page (\d+) of (\d+)', html)
            if not match:
                # Zero-results response has no result table; reject unexpected pages.
                if '0 Results Found' in html or 'No Resources Found' in html:
                    break
                raise ValueError(f'Unrecognized history search response for {label}')
            parser = HistoryTable()
            parser.feed(html)
            if int(match[1]) and not parser.rows:
                raise ValueError('History table could not be parsed')
            for row in parser.rows:
                cells = row['cells']
                if len(cells) != len(parser.headers):
                    raise ValueError('History table column mismatch')
                values = dict(zip(parser.headers, cells))
                stats = {s: int(values[s]) for s in STATS if values.get(s)}
                if not stats or any(v < 0 or v > 1000 for v in stats.values()):
                    raise ValueError('Invalid historical stats')
                rid = row['id']
                imported[rid] = {'id': rid, 'name': values['Resource'], 'type': values['Class'],
                    'stats': stats, 'planets': [], 'active': False,
                    'history_reported_active': row['active'], 'source': 'SWGAide history search',
                    'url': f'https://swgaide.com/resources/view.php?rid={rid}', 'first_seen': stamp()}
            print(f'History: {label} page {match[2]}/{match[3]} ({len(parser.rows)} records)')
            if int(match[2]) >= int(match[3]):
                break
            start += 50
            time.sleep(0.5)
        time.sleep(0.5)
    # Import all pages successfully before touching the archive. Current records take precedence.
    added = 0
    for rid, record in imported.items():
        if rid not in state['resources']:
            state['resources'][rid] = record
            added += 1
    state['history_imports'].append({'time': stamp(), 'source': 'SWGAide public history search',
        'records_found': len(imported), 'records_added': added,
        'coverage': 'Reported records in the searched SWGAide categories; not guaranteed complete server history'})
    return added


def webhook_url(raw):
    parts = urlsplit(raw)
    if parts.scheme != 'https' or parts.hostname not in ('discord.com', 'discordapp.com'):
        raise ValueError('Set DISCORD_WEBHOOK_URL to an HTTPS Discord webhook')
    if not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[\w-]+', parts.path):
        raise ValueError('Invalid Discord webhook path')
    query = dict(parse_qsl(parts.query))
    query['wait'] = 'true'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def send_webhook(url, payload, method='POST'):
    for attempt in range(4):
        request = Request(url, data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT}, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                data = json.loads(response.read())
                if not data.get('id'):
                    raise RuntimeError('Discord did not confirm a message ID')
                return data['id']
        except HTTPError as error:
            if error.code == 429 and attempt < 3:
                delay = float(json.loads(error.read()).get('retry_after', 5))
                if delay > 45:
                    raise RuntimeError('Discord rate limited; pending alerts retained') from None
                time.sleep(max(delay, 1))
                continue
            # Never expose the webhook URL/token through an HTTPError traceback.
            raise RuntimeError(f'Discord returned HTTP {error.code}; pending alerts retained') from None
    raise RuntimeError('Discord retries exhausted')


def deliver(state, state_path, config, url):
    valid = webhook_url(url)
    sent = 0
    while state['outbox']:
        event = state['outbox'][0]
        if event['kind'] == 'baseline':
            event['payload'] = intro_payload(state)
        if event['kind'] == 'leader':
            resource = state['resources'][event['resource_id']]
            if not resource.get('active') or resource.get('missing_snapshots'):
                state['outbox'].pop(0)
                save_state(state_path, state)
                continue
            current_values = {s: v for s, v in event['winner_values'].items()
                if state['leaders'].get(key_for(resource, s), {}).get('id') == resource['id']
                and state['leaders'][key_for(resource, s)]['value'] == v}
            if not current_values:
                state['outbox'].pop(0)
                save_state(state_path, state)
                continue
            # Refresh historical comparison and remove stale stat wins before posting a retry.
            changes = [(s, None, {'id': resource['id'], 'value': v}, False) for s, v in current_values.items()]
            if len(current_values) != len(event['winner_values']):
                event['payload'] = make_alert(resource, changes, state, leaders(state['resources'], config, active_only=False))
            for embed in event['payload'].get('embeds', []):
                embed['url'] = HOME_URL
        send_webhook(valid, event['payload'])
        state['outbox'].pop(0)
        sent += 1
        save_state(state_path, state)
        time.sleep(0.6)
    return sent


def write_report(state, config, path):
    active = leaders(state['resources'], config, previous=state['leaders'])
    history = leaders(state['resources'], config, active_only=False)
    lines = ['# Uprising resource records', '',
        f"Latest SWGAide export: {state.get('feed_generated', 'not collected yet')}", '',
        'Historical best means the best imported or observed record, including despawned resources.',
        'Comparisons use each stat within the exact resource class; these are not crafting scores.', '',
        '| Resource class | Stat | Active best | Historical best | Historical status |',
        '|---|---|---|---|---|']
    for key, record in sorted(history.items()):
        hist = state['resources'][record['id']]
        stat = json.loads(key)[1]
        current = active.get(key)
        value = 'None reported active'
        if current:
            resource = state['resources'][current['id']]
            value = f"[{resource['name']}]({resource['url']}) **{current['value']}**"
        status = 'Active' if hist.get('active') else 'Despawned / inactive'
        if hist.get('active') and hist.get('missing_snapshots'):
            status = 'Awaiting feed confirmation'
        safe_type = hist['type'].replace('|', '\\|')
        lines.append(f"| {safe_type} | {stat} | {value} | [{hist['name']}]({hist['url']}) **{record['value']}** | {status} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['seed-history', 'collect', 'deliver', 'report', 'preview', 'queue-summary', 'refresh-intro'])
    parser.add_argument('--state', type=Path, default=ROOT / 'data/state.json')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--feed', type=Path, help='Use a saved SWGAide XML/GZ file instead of downloading')
    parser.add_argument('--message-id', help='Existing webhook setup message to update')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if not set(config['stats']).issubset(STATS) or not config['stats']:
        raise ValueError('Select valid stat abbreviations in config.json')
    state = load_state(args.state)
    if args.command == 'seed-history':
        print(f'Imported {seed_history(state, config)} new historical records')
        save_state(args.state, state)
    elif args.command == 'collect':
        raw = args.feed.read_bytes() if args.feed else fetch(config['feed_url'])
        if raw.startswith(b'\x1f\x8b'):
            raw = gzip.decompress(raw)
        print(f'Queued {collect(state, config, raw)} resource alerts')
        save_state(args.state, state)
        write_report(state, config, args.state.parent / 'records.md')
    elif args.command == 'deliver':
        url = os.environ.get('DISCORD_WEBHOOK_URL', '')
        if not url:
            raise ValueError('Add the DISCORD_WEBHOOK_URL Actions secret before enabling alerts')
        print(f'Sent {deliver(state, args.state, config, url)} messages')
    elif args.command == 'report':
        write_report(state, config, args.state.parent / 'records.md')
    elif args.command == 'queue-summary':
        html = fetch(f"https://swgaide.com/resources/?server={config['server_id']}").decode('utf-8')
        payload = summary_payload(state, config, html)
        state['outbox'].append({'kind': 'summary', 'payload': payload})
        save_state(args.state, state)
        print(f"Queued one summary with {len(payload['embeds'][0]['fields'])} resources")
    elif args.command == 'refresh-intro':
        if not args.message_id or not re.fullmatch(r'\d{15,25}', args.message_id):
            raise ValueError('Provide a valid existing webhook message ID')
        parts = urlsplit(webhook_url(os.environ.get('DISCORD_WEBHOOK_URL', '')))
        url = urlunsplit((parts.scheme, parts.netloc, parts.path + '/messages/' + args.message_id, '', ''))
        send_webhook(url, intro_payload(state), method='PATCH')
        state['intro_home_link_updated'] = stamp()
        save_state(args.state, state)
        print('Updated the existing setup message to link to SWGAide Home')
    else:
        # Preview locally without posting or changing state.
        print(json.dumps([e['payload'] for e in state['outbox']], indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Avoid printing request objects, URLs or environment values from network errors.
        if isinstance(error, (ValueError, RuntimeError)):
            print(f'ERROR: {error}', file=sys.stderr)
        else:
            print(f'ERROR: {type(error).__name__}; state retained. Check feed/Discord connectivity.', file=sys.stderr)
        sys.exit(1)
