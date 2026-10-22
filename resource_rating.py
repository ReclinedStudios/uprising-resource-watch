"""SWGAide's displayed overall Rate and broad resource categories.

Class caps/hierarchy are public data from SWGAide's Resource Class Info page.
Rate = rounded mean of each stat/cap * 1000, truncated to an integer BEFORE
averaging. This matches all 454 displayed Uprising cards checked 2026-10-22.
Use integer arithmetic so half points do not depend on floating-point error.
"""
import json
from pathlib import Path

POLICY = 'broad-category-swgaide-rate-record-v1'
CLASS_DATA = json.loads(Path(__file__).with_name('resource_classes.json').read_text(encoding='utf-8'))
CLASSES = CLASS_DATA['classes']


def category(resource):
    try:
        return CLASSES[resource['type']]['category']
    except KeyError:
        raise ValueError(f"Unknown resource class: {resource.get('type')}; update resource_classes.json before collecting") from None


def rating(resource):
    category(resource)  # Unknown classes must never silently form new alert categories.
    caps = CLASSES[resource['type']]['caps']
    stats = resource['stats']
    if not caps or set(stats) != set(caps):
        raise ValueError(f"Incomplete stats for {resource['type']}; cannot compare overall ratings")
    if any(not isinstance(v, int) or not 0 <= v <= 1000 for v in stats.values()):
        raise ValueError('Invalid resource stats')
    total = sum(value * 1000 // caps[stat] for stat, value in stats.items())
    return (2 * total + len(stats)) // (2 * len(stats))


def winners(resources, config, previous=None, active_only=False):
    groups = {}
    selected = {t.casefold() for t in config.get('resource_types', [])}
    for rid, resource in resources.items():
        label, value = category(resource), rating(resource)
        if selected and label.casefold() not in selected:
            continue
        if active_only and (not resource.get('active') or resource.get('missing_snapshots', 0)):
            continue
        key = label.casefold()
        old = groups.get(key)
        if old is None or value > old['value']:
            groups[key] = {'id': rid, 'value': value, 'category': label, 'tied_ids': [rid]}
        elif value == old['value']:
            old['tied_ids'].append(rid)
    for key, result in groups.items():
        result['tied_ids'].sort()
        incumbent = (previous or {}).get(key, {}).get('id')
        result['id'] = incumbent if incumbent in result['tied_ids'] else result['tied_ids'][0]
    return groups
