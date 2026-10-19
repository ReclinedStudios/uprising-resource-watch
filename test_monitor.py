import copy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import monitor


CONFIG = json.loads((monitor.ROOT / 'config.json').read_text())


def feed(records, seconds=0, server='182'):
    root = ET.Element('resource_data', server_id=server, server_name='SWG Uprising',
        generated=(monitor.utcnow() + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S UTC'))
    resources = ET.SubElement(root, 'resources')
    for rid, name, oq, cd in records:
        resource = ET.SubElement(resources, 'resource', swgaide_id=rid)
        for key, value in [('name', name), ('type', 'Axidite Iron'), ('swgaide_type_id', 'axirn')]:
            ET.SubElement(resource, key).text = value
        stats = ET.SubElement(resource, 'stats')
        for key, value in [('oq', oq), ('cd', cd)]:
            ET.SubElement(stats, key).text = str(value)
        planets = ET.SubElement(resource, 'planets')
        planet = ET.SubElement(planets, 'planet')
        ET.SubElement(planet, 'name').text = 'Corellia'
    return ET.tostring(root)


A = ('1', 'Old Iron', 900, 200)
B = ('2', 'Other Iron', 800, 300)
C = ('3', 'New Iron', 950, 150)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.state = monitor.load_state(Path('nonexistent-test-state.json'))
        self.config = copy.deepcopy(CONFIG)
        (monitor.ROOT / 'tmp').mkdir(exist_ok=True)

    def baseline(self, records=(A, B)):
        monitor.collect(self.state, self.config, feed(records))
        self.state['outbox'].clear()

    def test_initial_run_has_one_summary_and_no_alert_flood(self):
        monitor.collect(self.state, self.config, feed([A, B]))
        self.assertEqual([e['kind'] for e in self.state['outbox']], ['baseline'])
        self.assertEqual(len(self.state['leaders']), 2)

    def test_stat_winners_are_independent(self):
        self.baseline()
        self.assertEqual(self.state['leaders'][json.dumps(['axidite iron', 'OQ'])]['id'], '1')
        self.assertEqual(self.state['leaders'][json.dumps(['axidite iron', 'CD'])]['id'], '2')

    def test_new_winner_alert_and_unchanged_poll_no_duplicate(self):
        self.baseline()
        raw = feed([A, B, C], seconds=2)
        self.assertEqual(monitor.collect(self.state, self.config, raw), 1)
        event = self.state['outbox'][0]
        self.assertEqual(event['winner_values'], {'OQ': 950})
        self.assertEqual(monitor.collect(self.state, self.config, raw), 0)
        self.assertEqual(len(self.state['outbox']), 1)

    def test_expired_winner_preserved_and_replacement_after_two_exports(self):
        self.baseline()
        self.assertEqual(monitor.collect(self.state, self.config, feed([B], seconds=2)), 0)
        self.assertTrue(self.state['resources']['1']['active'])
        self.assertEqual(monitor.collect(self.state, self.config, feed([B], seconds=4)), 1)
        self.assertFalse(self.state['resources']['1']['active'])
        history = monitor.leaders(self.state['resources'], self.config, active_only=False)
        self.assertEqual(history[json.dumps(['axidite iron', 'OQ'])]['value'], 900)
        message = json.dumps(self.state['outbox'][0]['payload'])
        self.assertIn('despawned / inactive', message)
        self.assertIn('replacement active best', message)

    def test_tie_does_not_displace_existing_active_winner(self):
        self.baseline()
        tied = ('0', 'Tied Iron', 900, 100)
        self.assertEqual(monitor.collect(self.state, self.config, feed([A, B, tied], seconds=2)), 0)

    def test_tie_replacement_when_incumbent_despawns(self):
        self.baseline([A, ('2', 'Tied Iron', 900, 200)])
        remaining = [('2', 'Tied Iron', 900, 200)]
        monitor.collect(self.state, self.config, feed(remaining, seconds=2))
        self.assertEqual(monitor.collect(self.state, self.config, feed(remaining, seconds=4)), 1)

    def test_wrong_server_and_empty_and_stale_feed_leave_state_untouched(self):
        self.baseline()
        before = copy.deepcopy(self.state)
        for raw in [feed([A], server='161'), feed([]), feed([A], seconds=-200000)]:
            with self.assertRaises(ValueError):
                monitor.collect(self.state, self.config, raw)
            self.assertEqual(self.state, before)

    def test_partial_feed_is_rejected(self):
        self.baseline([A, B, C])
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            monitor.collect(self.state, self.config, feed([A], seconds=2))
        self.assertEqual(self.state, before)

    def test_imported_despawned_best_beats_new_active_best(self):
        self.state['resources']['0'] = {'id': '0', 'name': 'Ancient Iron', 'type': 'Axidite Iron',
            'stats': {'OQ': 999}, 'active': False, 'url': 'https://swgaide.com/resources/view.php?rid=0'}
        self.baseline()
        monitor.collect(self.state, self.config, feed([A, B, C], seconds=2))
        payload = json.dumps(self.state['outbox'][0]['payload'])
        self.assertIn('999', payload)
        self.assertIn('Ancient Iron', payload)

    def test_failed_delivery_retains_pending_alert_without_exposing_secret(self):
        monitor.collect(self.state, self.config, feed([A]))
        with tempfile.TemporaryDirectory(dir=monitor.ROOT / 'tmp') as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(monitor.ROOT.resolve()))
            path = Path(directory) / 'state.json'
            with patch.object(monitor, 'send_webhook', side_effect=RuntimeError('failed')):
                with self.assertRaises(RuntimeError):
                    monitor.deliver(self.state, path, self.config, 'https://discord.com/api/webhooks/123/test-token')
            self.assertEqual(len(self.state['outbox']), 1)

    def test_successful_delivery_is_saved(self):
        monitor.collect(self.state, self.config, feed([A]))
        with tempfile.TemporaryDirectory(dir=monitor.ROOT / 'tmp') as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(monitor.ROOT.resolve()))
            path = Path(directory) / 'state.json'
            with patch.object(monitor, 'send_webhook', return_value='message1'), patch.object(monitor.time, 'sleep'):
                self.assertEqual(monitor.deliver(self.state, path, self.config, 'https://discord.com/api/webhooks/123/test-token'), 1)
            self.assertEqual(monitor.load_state(path)['outbox'], [])

    def test_history_html_parser_keeps_stat_header_mapping_and_inactive_status(self):
        parser = monitor.HistoryTable()
        parser.feed('<table id="restree"><thead><tr><th>Resource</th><th>Class</th><th>OQ</th><th>Age</th></tr></thead>'
            '<tr><td><a href="/resources/view.php?rid=12">Old &amp; Good</a></td><td>Iron</td><td>999</td>'
            '<td><span class="inactive">10d</span></td></tr></table>')
        self.assertEqual(parser.rows[0]['id'], '12')
        self.assertEqual(parser.rows[0]['cells'], ['Old & Good', 'Iron', '999', '10d'])
        self.assertFalse(parser.rows[0]['active'])

    def test_records_different_resource_types_are_not_compared(self):
        self.baseline()
        other = copy.deepcopy(self.state['resources']['1'])
        other.update(id='99', type='Other Type', stats={'OQ': 1000})
        self.state['resources']['99'] = other
        history = monitor.leaders(self.state['resources'], self.config, active_only=False)
        self.assertEqual(history[json.dumps(['axidite iron', 'OQ'])]['value'], 900)


if __name__ == '__main__':
    unittest.main()
