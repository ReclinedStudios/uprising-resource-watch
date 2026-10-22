import copy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import monitor
from resource_rating import CLASSES, POLICY, category, rating, winners

CONFIG = json.loads((monitor.ROOT / 'config.json').read_text())


def resource(rid='1', kind='Corellian Conifer Wood', value=500, active=True):
    return {'id': rid, 'name': 'Resource' + rid, 'type': kind,
        'stats': {s: cap * value // 1000 for s, cap in CLASSES[kind]['caps'].items()},
        'active': active, 'planets': ['Corellia'] if active else [],
        'url': 'https://swgaide.com/resources/view.php?rid=' + rid}


def feed(records, seconds=0, server='182'):
    root = ET.Element('resource_data', server_id=server, server_name='SWG Uprising',
        generated=(monitor.utcnow() + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S UTC'))
    resources = ET.SubElement(root, 'resources')
    for r in records:
        node = ET.SubElement(resources, 'resource', swgaide_id=r['id'])
        for key in ('name', 'type'):
            ET.SubElement(node, key).text = r[key]
        stats = ET.SubElement(node, 'stats')
        for key, value in r['stats'].items():
            ET.SubElement(stats, key.lower()).text = str(value)
        planets = ET.SubElement(node, 'planets')
        for name in r['planets']:
            ET.SubElement(ET.SubElement(planets, 'planet'), 'name').text = name
    return ET.tostring(root)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.state = monitor.load_state(Path('nonexistent-test-state.json'))
        self.config = copy.deepcopy(CONFIG)
        self.wood = resource()
        (monitor.ROOT / 'tmp').mkdir(exist_ok=True)
        self.verifier = patch.object(monitor.harvester, 'detail', return_value={'status': 'matched',
            'percentages': {}, 'icon': 'https://galaxyharvester.net/images/resources/wood.png',
            'url': 'https://galaxyharvester.net/resource.py/161/Resource1'})
        self.verifier.start()
        self.addCleanup(self.verifier.stop)

    def baseline(self, records=None):
        monitor.collect(self.state, self.config, feed(records or [self.wood]))
        self.state['outbox'].clear()

    def deliver(self):
        with tempfile.TemporaryDirectory(dir=monitor.ROOT / 'tmp') as directory:
            path = Path(directory) / 'state.json'
            count = monitor.deliver(self.state, path, self.config, 'https://discord.com/api/webhooks/123/test-token')
            return count, monitor.load_state(path)

    def test_first_install_one_intro(self):
        self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood, resource('2', 'Beyrllius Copper')])), 0)
        self.assertEqual([e['kind'] for e in self.state['outbox']], ['baseline'])
        self.assertEqual(len(self.state['leaders']), 2)

    def test_only_highest_new_record_per_broad_category(self):
        self.baseline()
        raw = feed([self.wood, resource('2', 'Dantooine Deciduous Wood', 600), resource('3', 'Endorian Evergreen Wood', 700)], 2)
        self.assertEqual(monitor.collect(self.state, self.config, raw), 1)
        event = self.state['outbox'][0]
        self.assertEqual((event['kind'], event['resource_id'], event['category']), ('record', '3', 'wood'))
        self.assertEqual(monitor.collect(self.state, self.config, raw), 0)
        self.assertEqual(len(self.state['outbox']), 1)

    def test_despawned_historical_best_blocks_lower_active_best(self):
        self.state['resources']['0'] = resource('0', value=900, active=False)
        self.baseline()
        self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=800)], 2)), 0)
        self.assertEqual(self.state['leaders']['wood']['id'], '0')
        self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=950)], 4)), 1)
        self.assertEqual(self.state['outbox'][0]['previous']['value'], 900)

    def test_despawn_silent_and_preserved_after_two_distinct_exports(self):
        self.baseline([self.wood, resource('2', value=800)])
        for seconds in (2, 2):
            self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood], seconds)), 0)
            self.assertTrue(self.state['resources']['2']['active'])
        self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood], 4)), 0)
        self.assertFalse(self.state['resources']['2']['active'])
        self.assertEqual(self.state['leaders']['wood']['id'], '2')
        self.assertEqual(self.state['outbox'], [])

    def test_tie_and_equal_replacement_silent(self):
        other = resource('0', 'Dantooine Deciduous Wood')
        self.baseline()
        self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood, other], 2)), 0)
        self.assertEqual(self.state['leaders']['wood']['id'], '1')
        for seconds in (4, 6):
            self.assertEqual(monitor.collect(self.state, self.config, feed([other], seconds)), 0)

    def test_individual_stat_record_not_overall_record(self):
        self.baseline()
        r = resource('2', value=100); r['stats']['OQ'] = 1000
        self.assertLess(rating(r), rating(self.wood))
        self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood, r], 2)), 0)

    def test_migration_same_export_is_silent_and_preserves_archive(self):
        self.baseline()
        self.state.pop('alert_policy')
        self.state['leaders'] = {'old-stat-key': {'id': '1', 'value': 999}}
        self.state['outbox'] = [{'kind': 'leader'}, {'kind': 'summary'}, {'kind': 'baseline'}]
        before = copy.deepcopy(self.state['resources'])
        self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood])), 0)
        self.assertEqual(self.state['outbox'], [])
        self.assertEqual(self.state['resources'], before)
        self.assertEqual(self.state['alert_policy'], POLICY)
        self.assertEqual(set(self.state['leaders']), {'wood'})

    def test_migration_new_export_baselines_new_winners_without_flood(self):
        self.baseline(); self.state.pop('alert_policy')
        self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=800)], 2)), 0)
        self.assertEqual(self.state['leaders']['wood']['value'], 800)
        self.assertEqual(self.state['outbox'], [])

    def test_unseen_category_first_resource_is_quiet_baseline(self):
        self.baseline()
        self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood, resource('2', 'Beyrllius Copper', 800)], 2)), 0)
        self.assertIn('copper', self.state['leaders'])

    def test_correction_no_false_replacement_or_repeat(self):
        self.baseline([self.wood, resource('2', value=900)])
        for seconds in (2, 4):
            self.assertEqual(monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=100)], seconds)), 0)
        self.assertEqual(self.state['leaders']['wood']['id'], '1')

    def test_bad_feed_leaves_state_untouched(self):
        self.baseline([self.wood, resource('2'), resource('3')])
        unknown = resource('4'); unknown['type'] = 'Unrecognized Wood'
        incomplete = resource('4'); incomplete['stats'].pop('OQ')
        before = copy.deepcopy(self.state)
        for raw in (feed([self.wood], server='161'), feed([]), feed([self.wood], -200000), feed([self.wood], 2),
            feed([self.wood, unknown], 2), feed([self.wood, incomplete], 2)):
            with self.assertRaises(ValueError): monitor.collect(self.state, self.config, raw)
            self.assertEqual(self.state, before)

    def test_delivery_valid_record_saves_progress(self):
        self.baseline()
        monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=800)], 2))
        with patch.object(monitor, 'send_webhook', return_value='message1') as send, patch.object(monitor.time, 'sleep'):
            count, saved = self.deliver()
        self.assertEqual(count, 1); self.assertEqual(saved['outbox'], [])
        self.assertIn('NEW SERVER BEST', send.call_args.args[1]['embeds'][0]['title'])
        self.assertEqual(send.call_args.args[1]['allowed_mentions'], {'parse': []})

    def test_failed_delivery_retains_record(self):
        self.baseline()
        monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=800)], 2))
        with patch.object(monitor, 'send_webhook', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError): self.deliver()
        self.assertEqual(len(self.state['outbox']), 1)

    def test_retry_drops_superseded_record(self):
        self.baseline()
        monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=800)], 2))
        monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=800), resource('3', value=900)], 4))
        with patch.object(monitor, 'send_webhook', return_value='message1') as send, patch.object(monitor.time, 'sleep'):
            self.assertEqual(self.deliver()[0], 1)
        self.assertIn('Resource3', send.call_args.args[1]['embeds'][0]['description'])

    def test_retry_drops_missing_or_corrected_record(self):
        for change in ('despawn', 'correction'):
            self.setUp(); self.baseline()
            monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=800)], 2))
            records = [self.wood] if change == 'despawn' else [self.wood, resource('2', value=100)]
            monitor.collect(self.state, self.config, feed(records, 4))
            with patch.object(monitor, 'send_webhook') as send: self.assertEqual(self.deliver()[0], 0)
            send.assert_not_called()

    def test_delivery_drops_old_policy_without_collection(self):
        self.baseline(); self.state['outbox'] = [{'kind': 'leader'}, {'kind': 'summary'}, {'kind': 'baseline'}]
        with patch.object(monitor, 'send_webhook') as send: self.assertEqual(self.deliver()[0], 0)
        send.assert_not_called()

    def test_conflicting_secondary_source_holds_alert_until_match(self):
        self.baseline()
        monitor.collect(self.state, self.config, feed([self.wood, resource('2', value=800)], 2))
        with patch.object(monitor.harvester, 'detail', return_value={'status': 'conflict'}), patch.object(monitor, 'send_webhook') as send:
            count, saved = self.deliver()
        self.assertEqual(count, 0); send.assert_not_called()
        self.assertEqual(saved['outbox'][0]['crosscheck_status'], 'conflict')
        with patch.object(monitor, 'send_webhook', return_value='message1'), patch.object(monitor.time, 'sleep'):
            self.assertEqual(self.deliver()[0], 1)

    def test_summary_only_one_active_historical_record_per_category(self):
        self.baseline([self.wood, resource('2', 'Dantooine Deciduous Wood', 800), resource('3', 'Beyrllius Copper', 700)])
        self.state['resources']['0'] = resource('0', 'Diatium Copper', 900, active=False)
        fields = monitor.summary_payload(self.state, self.config)['embeds'][0]['fields']
        self.assertEqual(len(fields), 1); self.assertIn('Wood', fields[0]['name']); self.assertIn('Resource2', fields[0]['value'])


class RatingTests(unittest.TestCase):
    def test_categories(self):
        for kind, expected in [('Corellian Conifer Wood', 'Wood'), ('Dantooine Deciduous Wood', 'Wood'),
            ('Dantooine Berry Fruit', 'Fruit'), ('Nabooian Flower Fruit', 'Fruit'), ('Beyrllius Copper', 'Copper'),
            ('Diatium Copper', 'Copper'), ('Carbonite Steel', 'Steel')]:
            self.assertEqual(category(resource(kind=kind)), expected)

    def test_cap_normalization_and_half_up_rounding(self):
        # Real SWGAide cards that catch raw averaging and floating-point rounding errors.
        r = resource(kind='Corellian Water Vapor'); r['stats'] = {'DR': 956, 'OQ': 813}
        self.assertEqual(rating(r), 885)
        r = resource(kind='Corellian Domesticated Milk'); r['stats'] = {'DR': 931, 'FL': 175, 'PE': 543, 'OQ': 214}
        self.assertEqual(rating(r), 485)
        r = resource(kind='Axidite Iron')
        r['stats'] = {'CR': 329, 'CD': 169, 'DR': 586, 'HR': 642, 'MA': 217, 'OQ': 713, 'SR': 564, 'UT': 545}
        self.assertEqual(rating(r), 876)  # Rounding before truncating would incorrectly yield 877.

    def test_broad_category_filter(self):
        resources = {r['id']: r for r in [resource(), resource('2', 'Beyrllius Copper')]}
        self.assertEqual(set(winners(resources, {**CONFIG, 'resource_types': ['WOOD']})), {'wood'})


class SourceTests(unittest.TestCase):
    def test_history_parser_keeps_header_mapping_and_status(self):
        parser = monitor.HistoryTable()
        parser.feed('<table id="restree"><thead><tr><th>Resource</th><th>Class</th><th>OQ</th></tr></thead>'
            '<tr><td><a href="/resources/view.php?rid=12">Old &amp; Good</a></td><td>Iron</td><td>999</td></tr></table>')
        self.assertEqual(parser.rows[0]['cells'], ['Old & Good', 'Iron', '999'])
        self.assertEqual(parser.rows[0]['id'], '12'); self.assertFalse(parser.rows[0]['active'])

    def test_waypoints_highest_valid_concentration_on_reported_planets(self):
        node = ET.fromstring('<resource><waypoints>'
            '<waypoint timestamp="1"><planet>Corellia</planet><x>1</x><y>2</y><con>60</con></waypoint>'
            '<waypoint timestamp="2"><planet>Corellia</planet><x>-30</x><y>40</y><con>95</con></waypoint>'
            '<waypoint timestamp="3"><planet>Naboo</planet><x>5</x><y>6</y><con>99</con></waypoint>'
            '<waypoint><planet>Corellia</planet><x>1</x><y>2</y><con>NaN</con></waypoint>'
            '</waypoints></resource>')
        text = monitor.waypoint_text({'name': 'Iron', 'waypoints': monitor.parse_waypoints(node, ['Corellia'])})
        self.assertIn('/wp corellia -30 40 Iron 95%', text); self.assertNotIn('Naboo', text)


if __name__ == '__main__':
    unittest.main()
