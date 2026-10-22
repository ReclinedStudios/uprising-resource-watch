import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import galaxy_harvester as gh
import monitor
from test_records import resource


class HarvesterTests(unittest.TestCase):
    def test_explicit_type_spelling_aliases(self):
        self.assertEqual(gh.canonical_type('Corellian Fiberplast'), 'Corellia Fiberplast')
        self.assertEqual(gh.canonical_type('Talusian Reptillian Meat'), 'Talusian Reptilian Meat')
        self.assertEqual(gh.canonical_type('Dantooine Egg Meat'), 'Dantooine Egg')

    def test_trailing_comma_parser_preserves_commas_and_brackets_inside_strings(self):
        raw = b'{"response":{"resources":[{"spawn_name":"name,} and \\\"quote\\\"",},],}}'
        parsed = gh.parse_json(raw)
        self.assertEqual(parsed['resources'][0]['spawn_name'], 'name,} and "quote"')

    def test_rejects_malformed_or_error_responses(self):
        for raw in (b'{"response":"Error: wrong galaxy"}', b'{not json}'):
            with self.assertRaises((ValueError, TypeError)):
                gh.parse_json(raw)

    def test_last_short_page_missing_array_terminator(self):
        raw = b'{"response":{"resources":[{"spawn_name":"literal } ]"},\n }\n }'
        self.assertEqual(gh.parse_json(raw)['resources'][0]['spawn_name'], 'literal } ]')

    def test_history_pagination_and_server_validation(self):
        first = [{'spawn_id': n, 'galaxy': 161} for n in range(200, 101, -1)]
        second = [{'spawn_id': 101, 'galaxy': 161}]
        calls = []
        def fetch(url):
            calls.append(url)
            rows = first if len(calls) == 1 else second
            return json.dumps({'response': {'resources': rows, 'last_value': 102}}).encode()
        self.assertEqual(len(gh.history(fetch)), 100)
        self.assertEqual(parse_qs(urlsplit(calls[1]).query)['lastValue'], ['102'])
        bad = lambda url: b'{"response":{"resources":[{"spawn_id":1,"galaxy":162}]}}'
        with self.assertRaisesRegex(ValueError, 'Wrong galaxy'):
            gh.history(bad)

    def test_detail_percentages_use_min_and_max_not_swgaide_caps(self):
        r = resource(kind='Quadranium Steel')
        r['stats'] = {'CR':552, 'CD':518, 'DR':859, 'HR':872, 'MA':279, 'OQ':25, 'SR':879, 'UT':866}
        ranges = {'CR':(500,950), 'CD':(1,650), 'DR':(800,1000), 'HR':(865,1000),
            'MA':(200,650), 'OQ':(1,1000), 'SR':(800,944), 'UT':(600,1000)}
        xml = '<result><spawnName>' + r['name'] + '</spawnName><resourceTypeName>Quadranium Steel</resourceTypeName>'
        for stat,value in r['stats'].items():
            low,high = ranges[stat]
            xml += f'<{stat}>{value}</{stat}><{stat}min>{low}</{stat}min><{stat}max>{high}</{stat}max>'
        xml += '<containerType>steel</containerType><resultText>found</resultText></result>'
        calls = []
        def fetch(url): calls.append(url); return xml.encode()
        result = gh.detail(r, fetch)
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['percentages']['CR'], 12)
        self.assertEqual(result['percentages']['OQ'], 2)
        self.assertEqual(parse_qs(urlsplit(calls[0]).query)['galaxy'], ['161'])
        self.assertTrue(result['icon'].endswith('/steel.png'))
        card = monitor.make_record_alert(r, {'value': 500}, result)['embeds'][0]
        self.assertIn('12%', card['fields'][0]['value'])
        self.assertIn('stats match', card['description'])
        changed = copy.deepcopy(r); changed['stats']['OQ'] = 1000
        self.assertEqual(gh.detail(changed, fetch)['status'], 'conflict')
        self.assertEqual(gh.detail(r, lambda url: xml.replace('</result>', '<unavailable>date</unavailable></result>').encode())['status'], 'inactive')

    def test_history_import_adds_missing_records_but_does_not_overwrite_conflicts(self):
        state = monitor.load_state(Path('absent-test-state.json'))
        r = resource(); state['resources']['1'] = r
        old = resource('2', 'Beyrllius Copper', 800)
        incomplete = resource('3'); incomplete['stats'].pop('OQ')
        rows = []
        for item in (old, incomplete):
            rows.append({'spawn_id': item['id'], 'spawn_name': item['name'], 'resource_type_name': item['type'],
                'unavailable': 'date', **item['stats']})
        conflict = {'spawn_id': '4', 'spawn_name': r['name'], 'resource_type_name': r['type'], **r['stats']}
        conflict['OQ'] = 1000; rows.append(conflict)
        with patch.object(gh, 'history', return_value=rows):
            monitor.sync_harvester_history(state)
        self.assertEqual(state['resources']['1'], r)
        self.assertEqual(state['resources']['gh:2']['stats'], old['stats'])
        self.assertNotIn('gh:3', state['resources'])
        self.assertEqual(state['harvester_history']['conflicts'], [r['name']])

    def test_new_active_gh_spawn_is_not_consumed_before_swgaide_alert(self):
        state = monitor.load_state(Path('absent-test-state.json'))
        state['alert_policy'] = monitor.POLICY
        item = resource('2', 'Beyrllius Copper', 800)
        row = {'spawn_id': '2', 'spawn_name': item['name'], 'resource_type_name': item['type'], **item['stats']}
        with patch.object(gh, 'history', return_value=[row]):
            monitor.sync_harvester_history(state)
        self.assertEqual(state['resources'], {})


if __name__ == '__main__': unittest.main()
