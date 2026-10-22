"""Read public Galaxy Harvester Uprising data; never log in or write to GH."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import json
import re
from urllib.parse import urlencode, quote
import xml.etree.ElementTree as ET

GALAXY = '161'
STATS = ('ER', 'CR', 'CD', 'DR', 'FL', 'HR', 'MA', 'PE', 'OQ', 'SR', 'UT')


def canonical_type(name):
    # Explicit spelling differences between the two public resource-class trees.
    aliases = {'Bilal Inert Gas': 'Bilal gas Inert Gas',
        'Corellian Berry Fruit': 'Corellia Berry Fruit',
        'Corellian Flower Fruit': 'Corellia Flower Fruit',
        'Corellian Evergreen Wood': 'Corellia Evergreen Wood',
        'Corellian Fiberplast': 'Corellia Fiberplast',
        'Dathomirian Water Vapor': 'Dathomir Water Vapor',
        'Dathomirian Wild Wheat': 'Dathomir Wild Wheat'}
    return aliases.get(name, name).replace(' Reptillian Meat', ' Reptilian Meat').replace(' Egg Meat', ' Egg')


def parse_json(raw):
    # The public endpoint emits trailing commas. Remove only commas outside strings
    # immediately before ] or }; never rewrite resource names or other string data.
    text = raw.decode('utf-8')
    out, quoted, escaped, stack = [], False, False, []
    for i, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            stack.append(char)
        elif char == ']':
            if stack and stack[-1] == '[':
                stack.pop()
        elif char == '}':
            # GH omits the resources array's closing ] on the last, short page.
            # Repair only that outer response/resources shape, outside strings.
            if stack == ['{', '{', '[']:
                out.append(']')
                stack.pop()
            if stack and stack[-1] == '{':
                stack.pop()
        elif char == ',':
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] in ']}':
                continue
        out.append(char)
    response = json.loads(''.join(out))['response']
    if isinstance(response, dict) and response.get('total_results') == 0 and response.get('resources') == 'No resources found!':
        response['resources'] = []
    if not isinstance(response, dict) or not isinstance(response.get('resources'), list):
        raise ValueError('Unexpected Galaxy Harvester history response')
    return response


def history(fetch):
    """Fetch the public history, with bounded ID-based pagination and server checks."""
    records, last = {}, None
    for page in range(100):
        params = {'galaxy': GALAXY, 'formatType': 'json', 'fetchSize': '99'}
        if last is not None:
            params['lastValue'] = str(last)
        response = parse_json(fetch('https://galaxyharvester.net/getResources.py?' + urlencode(params)))
        rows = response['resources']
        for row in rows:
            if str(row['galaxy']) != GALAXY:
                raise ValueError('Wrong galaxy in Galaxy Harvester history')
            rid = str(int(row['spawn_id']))
            if rid in records:
                raise ValueError('Repeated Galaxy Harvester history page')
            records[rid] = row
        if len(rows) < 99:
            return list(records.values())
        next_id = int(response['last_value'])
        if last is not None and next_id >= last:
            raise ValueError('Galaxy Harvester history pagination did not advance')
        last = next_id
    raise ValueError('Galaxy Harvester history exceeded pagination limit')


def detail(resource, fetch):
    """Compare live GH values with the SWGAide candidate before posting."""
    url = 'https://galaxyharvester.net/getResourceByName.py?' + urlencode({'galaxy': GALAXY, 'name': resource['name']})
    root = ET.fromstring(fetch(url))
    if root.tag != 'result' or root.findtext('spawnName', '').casefold() != resource['name'].casefold():
        raise ValueError('Unexpected Galaxy Harvester resource response')
    result = root.findtext('resultText')
    if result == 'new':
        return {'status': 'not_found'}
    if result != 'found':
        raise ValueError('Galaxy Harvester could not check this resource')
    stats = {s: int(root.findtext(s)) for s in STATS if root.findtext(s) not in (None, '', 'None')}
    if canonical_type(root.findtext('resourceTypeName', '')) != canonical_type(resource['type']) or stats != resource['stats']:
        return {'status': 'conflict'}
    if root.find('unavailable') is not None:
        return {'status': 'inactive'}
    percentages = {}
    for stat, value in stats.items():
        low, high = int(root.findtext(stat + 'min')), int(root.findtext(stat + 'max'))
        if high > low:
            percentages[stat] = int((Decimal(value - low) * 100 / Decimal(high - low)).quantize(Decimal('1'), rounding=ROUND_HALF_EVEN))
    icon = root.findtext('containerType', '')
    if not re.fullmatch(r'[a-z0-9_]+', icon):
        raise ValueError('Unrecognized Galaxy Harvester resource icon')
    return {'status': 'matched', 'percentages': percentages,
        'icon': 'https://galaxyharvester.net/images/resources/' + icon + '.png',
        'url': 'https://galaxyharvester.net/resource.py/' + GALAXY + '/' + quote(resource['name'], safe=''),
        'entered': root.findtext('entered'), 'entered_by': root.findtext('enteredBy'),
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
