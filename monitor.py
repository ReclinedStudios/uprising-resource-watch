"""SWG Uprising server-record monitor. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import copy
from resource_rating import POLICY, category, rating, winners
import galaxy_harvester as harvester
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import gzip
from html.parser import HTMLParser
import json
import math
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
HOME_URL = 'https://reclinedstudios.github.io/uprising-resource-watch/'
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
            'active': bool(planets), 'source': 'SWGAide', 'waypoints': parse_waypoints(node, planets)}
        if not resources[rid]['name'] or not resources[rid]['type']:
            raise ValueError('Resource name or class missing')
    if not resources:
        raise ValueError('Empty feed; keeping previous active resources')
    return generated.isoformat(), resources


def parse_waypoints(node, planets):
    normalize = lambda p: re.sub(r'[^a-z0-9]', '', p.casefold())
    allowed = {normalize(p) for p in planets}
    points = []
    for wp in node.findall('./waypoints/waypoint'):
        try:
            planet = wp.findtext('planet', '').strip()
            concentration = float(wp.findtext('con', ''))
            x, y = int(wp.findtext('x', '')), int(wp.findtext('y', ''))
            if normalize(planet) not in allowed or not math.isfinite(concentration) or not 0 < concentration <= 100:
                continue
            points.append({'planet': planet, 'x': x, 'y': y, 'concentration': concentration,
                           'reported_at': int(wp.get('timestamp', '0'))})
        except (ValueError, TypeError):
            continue
    return points


def waypoint_text(resource):
    points = resource.get('waypoints', [])
    if not points:
        return 'No concentration waypoint reported on SWGAide yet.'
    best = max(points, key=lambda p: (p['concentration'], p.get('reported_at', 0)))
    planet_code = re.sub(r'[^a-z0-9]', '', best['planet'].casefold())
    command = f"/wp {planet_code} {best['x']} {best['y']} {resource['name']} {best['concentration']:g}%"
    return f"{text_safe(best['planet'])}: **{best['concentration']:g}%** at **{best['x']}, {best['y']}**\n`{command}`"


def text_safe(value):
    return re.sub(r'([\\`*_~|<>])', r'\\\1', str(value)).replace('@', '@\u200b')


def stat_table(resource, verification):
    stats = [s for s in STATS if s in resource['stats']]
    percentages = verification.get('percentages', {})
    lines = []
    for offset in range(0, len(stats), 8):
        group = stats[offset:offset+8]
        lines.append(' '.join(f'{s:>6}' for s in group))
        lines.append(' '.join(f'{resource["stats"][s]:>6}' for s in group))
        lines.append(' '.join(f'{str(percentages[s]) + "%":>6}' if s in percentages else f'{"—":>6}' for s in group))
    return '```\n' + '\n'.join(lines) + '\n```'


def make_record_alert(resource, previous, verification=None):
    value = rating(resource)
    verification = verification or {}
    embed = {'title': f"NEW SERVER BEST — {category(resource).upper()}"[:256],
            'url': resource['url'],
            'description': f"**{text_safe(resource['name'])}** · {text_safe(resource['type'])}\n"
                f"SWGAide rating: **{value} / 1000** (previous record: {previous['value']})\n"
                f"Planets: {text_safe(', '.join(resource['planets']))}",
            'color': 0xF3A638,
            'fields': [{'name': 'Stats · Galaxy Harvester range %', 'value': stat_table(resource, verification), 'inline': False},
                {'name': 'Best reported concentration', 'value': waypoint_text(resource), 'inline': False}],
            'footer': {'text': 'SWG Uprising | SWGAide overall rating · GH range percentages | Imported + observed history'},
            'timestamp': stamp()}
    if verification.get('status') == 'matched':
        embed['thumbnail'] = {'url': verification['icon']}
        embed['description'] += f"\n[SWGAide]({resource['url']}) · [Galaxy Harvester]({verification['url']}) · stats match"
    return {'username': 'Uprising Resource Watch', 'allowed_mentions': {'parse': []}, 'embeds': [embed]}


def sync_harvester_history(state, force=False):
    last = state.get('harvester_history', {}).get('checked_at')
    if last and not force and utcnow() - datetime.fromisoformat(last) < timedelta(hours=24):
        return
    rows = harvester.history(fetch)
    working = copy.deepcopy(state['resources'])
    by_name = {r['name'].casefold(): r for r in sorted(working.values(),
        key=lambda r: r.get('source') != 'Galaxy Harvester history')}
    added, matched, conflicts, skipped = 0, 0, [], []
    baselining = state.get('alert_policy') != POLICY
    for row in rows:
        name, kind = row['spawn_name'], harvester.canonical_type(row['resource_type_name'])
        stats = {s: row[s] for s in STATS if s in row}
        rid = 'gh:' + str(row['spawn_id'])
        record = {'id': rid, 'name': name, 'type': kind, 'stats': stats,
            'active': False, 'planets': [], 'source': 'Galaxy Harvester history',
            'url': 'https://galaxyharvester.net/resource.py/161/' + harvester.quote(name, safe=''),
            'first_seen': stamp()}
        try:
            rating(record)
        except ValueError:
            skipped.append(name)
            continue
        # Refresh a previously imported GH record even if SWGAide now has the same
        # name too; source corrections must not leave a stale duplicate high score.
        if rid in working and working[rid].get('source') == 'Galaxy Harvester history':
            record['first_seen'] = working[rid].get('first_seen', record['first_seen'])
            working[rid] = record
        old = by_name.get(name.casefold())
        if old and old.get('source') != 'Galaxy Harvester history':
            if harvester.canonical_type(old['type']) == kind and old['stats'] == stats:
                matched += 1
            else:
                conflicts.append(name)
            continue
        # Current GH-only spawns are not silently consumed as history before SWGAide
        # can detect their record. Existing records at first upgrade are baselined.
        if old is None and not baselining and 'unavailable' not in row:
            continue
        if old:
            record['first_seen'] = old.get('first_seen', record['first_seen'])
        working[rid] = record
        by_name[name.casefold()] = record
        added += old is None
    state['resources'] = working
    state['harvester_history'] = {'checked_at': stamp(), 'galaxy': '161',
        'records_found': len(rows), 'records_added': added, 'matching_swgaide_records': matched,
        'conflicts': conflicts, 'incomplete_or_unknown': skipped,
        'coverage': 'Public Galaxy Harvester Uprising history; GH-only records retained as historical comparisons'}
    print(f'Galaxy Harvester: {len(rows)} history records, {added} added, {matched} matching, {len(conflicts)} conflicts, {len(skipped)} incomplete/unknown')


def intro_payload(state):
    return {'username': 'Uprising Resource Watch', 'allowed_mentions': {'parse': []},
        'content': 'Resource alerts: **NEW SERVER BEST** only, ranked by **SWGAide overall rating**. '
            'One winner per broad category (Wood, Copper, Steel, Fruit, etc.). '
            'Ties and despawns are silent. History includes SWGAide and Galaxy Harvester reports; '
            'record alerts wait for matching live stats on both sites.\n'
            f'**SWG Uprising Home (selected automatically):** {HOME_URL}'}


def collect(state, config, raw):
    generated, current = parse_feed(raw, config)
    if state.get('feed_generated') and generated < state['feed_generated']:
        raise ValueError('Feed moved backwards in time')
    prior_count = state.get('feed_count', 0)
    if prior_count and len(current) < prior_count * config['minimum_feed_size_ratio']:
        raise ValueError('Unexpected large feed drop; retaining previous state for review')
    # Validate all archived and incoming ratings before mutating state.
    before = winners(state['resources'], config, previous=state.get('leaders'))
    winners(current, config)
    migrating = state.get('alert_policy') != POLICY
    repeated = generated == state.get('feed_generated')
    if repeated and not migrating:
        return 0
    working = copy.deepcopy(state)
    now = stamp()
    if not repeated:
        for rid, resource in working['resources'].items():
            if resource.get('active') and rid not in current:
                resource['missing_snapshots'] = resource.get('missing_snapshots', 0) + 1
                if resource['missing_snapshots'] >= config['missing_snapshots_before_inactive']:
                    resource['active'] = False
                    resource['inactive_observed_at'] = now
        for rid, resource in current.items():
            first_seen = working['resources'].get(rid, {}).get('first_seen', now)
            working['resources'][rid] = {**resource, 'first_seen': first_seen, 'missing_snapshots': 0}
    records = winners(working['resources'], config, previous=before)
    queued = 0
    if migrating:
        # Old queued active/stat leaders and manual summaries do not satisfy this policy.
        working['outbox'] = []
        working['policy_baselined_at'] = now
    elif working['initialized']:
        for key, record in sorted(records.items()):
            old = before.get(key)
            resource = working['resources'][record['id']]
            if old is None or record['value'] <= old['value']:
                continue
            if record['id'] not in current or not resource.get('active'):
                continue
            working['outbox'].append({'kind': 'record', 'policy': POLICY, 'category': key,
                'resource_id': resource['id'], 'value': record['value'], 'previous': old,
                'payload': make_record_alert(resource, old)})
            queued += 1
    # A brand-new install gets one intro; an existing installation migrates silently.
    if not state['initialized']:
        working['outbox'].append({'kind': 'baseline', 'policy': POLICY, 'payload': intro_payload(working)})
    working.update(initialized=True, alert_policy=POLICY, leaders=records,
        feed_generated=generated, feed_count=len(current), last_successful_collection=now)
    state.clear()
    state.update(working)
    return queued


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
    """Read displayed SWGAide ratings for independent calculation verification."""
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


def summary_payload(state, config, html=None):
    # Optional manual summary: one resource per broad category, and only historical winners.
    records = winners(state['resources'], config, previous=state.get('leaders'))
    fields = []
    for key, record in sorted(records.items(), key=lambda pair: (-pair[1]['value'], pair[0])):
        # If a record is tied, an active tied resource can represent the category in a summary.
        eligible = [state['resources'][rid] for rid in record['tied_ids']
            if state['resources'][rid].get('active') and not state['resources'][rid].get('missing_snapshots')]
        if not eligible:
            continue
        resource = next((r for r in eligible if r['id'] == record['id']), eligible[0])
        field = {'name': f"{record['category']} — {record['value']} / 1000"[:256],
            'value': f"[{text_safe(resource['name'])}]({resource['url']}) · {text_safe(', '.join(resource['planets']))}",
            'inline': False}
        if len(fields) == 25 or sum(len(f['name']) + len(f['value']) for f in fields + [field]) > 4800:
            break
        fields.append(field)
    if not fields:
        raise ValueError('No historical record holders are currently active; summary not posted')
    return {'username': 'Uprising Resource Watch', 'allowed_mentions': {'parse': []},
        'embeds': [{'title': 'Harvestable server records — SWG Uprising', 'url': HOME_URL,
            'description': 'SWGAide overall rating. One pick per broad category; up to 25 highest-rated active historical record holders.',
            'fields': fields, 'color': 0xF3A638,
            'footer': {'text': 'Imported + observed SWGAide history, including despawned resources'},
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


def refresh_message_links(raw_webhook, message_id):
    if not re.fullmatch(r'\d{15,25}', message_id):
        raise ValueError('Invalid message ID')
    parts = urlsplit(webhook_url(raw_webhook))
    url = urlunsplit((parts.scheme, parts.netloc, parts.path + '/messages/' + message_id, '', ''))
    try:
        with urlopen(Request(url, headers={'User-Agent': USER_AGENT}), timeout=30) as response:
            message = json.loads(response.read())
    except HTTPError as error:
        raise RuntimeError(f'Discord returned HTTP {error.code} while reading the existing message') from None
    content = '\n'.join(line for line in message.get('content', '').splitlines()
        if 'Home server selector' not in line)
    content = re.sub(r'https://swgaide\.com/(?:resources/\?server=182)?(?=$|[\s)>])', HOME_URL, content)
    content = content.replace('**SWGAide Home:**', '**SWG Uprising Home (selected automatically):**')
    embeds = []
    for original in message.get('embeds', []):
        if original.get('type', 'rich') != 'rich':
            continue
        embed = {k: original[k] for k in ('title','description','color','fields','footer','timestamp') if k in original}
        embed['url'] = HOME_URL
        embeds.append(embed)
    send_webhook(url, {'content': content, 'embeds': embeds, 'allowed_mentions': {'parse': []}}, method='PATCH')


def deliver(state, state_path, config, url):
    valid = webhook_url(url)
    records = winners(state['resources'], config, previous=state.get('leaders'))
    sent = 0
    remaining = len(state['outbox'])
    while state['outbox'] and remaining:
        remaining -= 1
        event = state['outbox'][0]
        resource = state['resources'].get(event.get('resource_id'))
        record = records.get(event.get('category'), {})
        permitted = state.get('alert_policy') == POLICY and event.get('policy') == POLICY
        if event['kind'] == 'record':
            permitted = permitted and bool(resource) and resource.get('active') and not resource.get('missing_snapshots')
            permitted = permitted and record.get('id') == event['resource_id'] and record.get('value') == event['value']
            permitted = permitted and event['value'] > event['previous']['value']
            if permitted:
                verification = harvester.detail(resource, fetch)
                if verification['status'] != 'matched':
                    event['crosscheck_status'] = verification['status']
                    event['last_checked_at'] = stamp()
                    state['outbox'].append(state['outbox'].pop(0))
                    save_state(state_path, state)
                    print(f"Held record alert for {event['category']}: Galaxy Harvester {verification['status']}")
                    continue
                event['payload'] = make_record_alert(resource, event['previous'], verification)
        elif event['kind'] == 'baseline':
            event['payload'] = intro_payload(state)
        elif event['kind'] == 'summary':
            if permitted:
                event['payload'] = summary_payload(state, config)
        else:
            permitted = False
        if not permitted:
            state['outbox'].pop(0)
            save_state(state_path, state)
            continue
        send_webhook(valid, event['payload'])
        state['outbox'].pop(0)
        sent += 1
        save_state(state_path, state)
        time.sleep(0.6)
    return sent


def write_report(state, config, path):
    history = winners(state['resources'], config, previous=state.get('leaders'))
    lines = ['# Uprising server records', '',
        f"Latest SWGAide export: {state.get('feed_generated', 'not collected yet')}", '',
        'One historical winner per broad category, ranked by SWGAide overall rating.',
        'History includes imported + observed SWGAide and Galaxy Harvester reports, including despawned resources; unreported server history is unknown.', '',
        '| Category | Server best | SWGAide rating | Status |', '|---|---|---|---|']
    for key, record in sorted(history.items()):
        resource = state['resources'][record['id']]
        active_ties = [state['resources'][rid] for rid in record['tied_ids']
            if state['resources'][rid].get('active') and not state['resources'][rid].get('missing_snapshots')]
        if active_ties:
            resource = active_ties[0]
        status = 'Active' if resource.get('active') else 'Despawned / inactive'
        if resource.get('source') == 'Galaxy Harvester history':
            status = 'Historical comparison (Galaxy Harvester)'
        if resource.get('active') and resource.get('missing_snapshots'):
            status = 'Awaiting feed confirmation'
        lines.append(f"| {record['category']} | [{resource['name']}]({resource['url']}) | {record['value']} / 1000 | {status} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['seed-history', 'collect', 'deliver', 'report', 'preview', 'queue-summary', 'refresh-intro', 'refresh-home-links'])
    parser.add_argument('--state', type=Path, default=ROOT / 'data/state.json')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--feed', type=Path, help='Use a saved SWGAide XML/GZ file instead of downloading')
    parser.add_argument('--message-id', help='Existing webhook setup message to update')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    state = load_state(args.state)
    if args.command == 'seed-history':
        print(f'Imported {seed_history(state, config)} new historical records')
        save_state(args.state, state)
    elif args.command == 'collect':
        raw = args.feed.read_bytes() if args.feed else fetch(config['feed_url'])
        if raw.startswith(b'\x1f\x8b'):
            raw = gzip.decompress(raw)
        # Saved-feed dry runs remain offline. Cloud checks refresh GH history daily.
        if not args.feed:
            sync_harvester_history(state)
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
        payload = summary_payload(state, config)
        state['outbox'].append({'kind': 'summary', 'policy': POLICY, 'payload': payload})
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
    elif args.command == 'refresh-home-links':
        ids = [s.strip() for s in (args.message_id or '').split(',')]
        if not ids or any(not re.fullmatch(r'\d{15,25}', s) for s in ids):
            raise ValueError('Provide valid comma-separated webhook message IDs')
        for message_id in ids:
            refresh_message_links(os.environ.get('DISCORD_WEBHOOK_URL', ''), message_id)
        state['home_links_refreshed'] = stamp()
        save_state(args.state, state)
        print(f'Updated {len(ids)} existing messages to force SWG Uprising Home')
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
