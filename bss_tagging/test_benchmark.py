"""Local semantic tests. SQLite is an oracle harness, not a ClickZetta compiler."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import benchmark as b
import analyze_measurements as am


class TaggingTests(unittest.TestCase):
    def setUp(self):
        self.plan = b.prepare(100, 150, 10, 'bss_test')
        self.db = sqlite3.connect(':memory:')
        self.db.create_function('CONCAT', -1, lambda *xs: ''.join(str(x) for x in xs))
        self.db.create_function('date_add', 2, lambda d, n:
                                (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat())
        for step in self.plan['stages']['init']:
            sql = step['sql']
            if sql.startswith('CREATE SCHEMA'):
                continue
            if sql.startswith('CREATE DYNAMIC TABLE') or step['label'] == 'create_full':
                continue
            self.execute(sql)

    def tearDown(self):
        self.db.close()

    def execute(self, sql):
        return self.db.execute(sql.replace('bss_test.', '').replace('AS STRING', 'AS TEXT'))

    def tags(self):
        cursor = self.execute(b.full_query('bss_test', b.queries('bss_test')))
        return {row[0]: dict(zip([c[0] for c in cursor.description], row)) for row in cursor}

    def oracle(self):
        """Independently calculate expected customer tags from raw rows in Python."""
        subs = list(self.execute('SELECT customer_id, status, monthly_charge, offering_id, renewal_due FROM product_inventory'))
        offers = dict(self.execute('SELECT offering_id, family FROM product_offering'))
        accounts = dict(self.execute('SELECT account_id, customer_id FROM billing_account'))
        bills = list(self.execute('SELECT account_id, outstanding_amount, overdue_30 FROM customer_bill'))
        tickets = list(self.execute('SELECT customer_id, status, priority FROM trouble_ticket'))
        expected = {}
        for customer, status, consent in self.execute('SELECT customer_id, status, marketing_consent FROM customer'):
            active = [p for p in subs if p[0] == customer and p[1] == 'active']
            spend = sum(p[2] for p in active)
            debt = sum(amount for account, amount, overdue in bills if accounts[account] == customer and overdue)
            complaint = any(c == customer and state == 'open' and priority == 'high' for c, state, priority in tickets)
            mobile = any(offers[p[3]] == 'mobile' for p in active)
            broadband = any(offers[p[3]] == 'broadband' for p in active)
            renewal = any(p[4] for p in active)
            enabled = status == 'active'
            flags = (enabled and spend >= 100, enabled and len(active) >= 2,
                     enabled and debt >= 50, enabled and spend >= 100 and (renewal or complaint),
                     enabled and bool(consent) and mobile and not broadband and debt == 0 and not complaint,
                     not enabled or not consent or debt > 0 or complaint)
            expected[customer] = dict(zip(b.TAG_COLUMNS, (customer, 'bss-v1', *(int(x) for x in flags))))
        return expected

    def test_seed_cardinality_and_oracle(self):
        self.assertEqual(self.execute('SELECT COUNT(*) FROM customer').fetchone()[0], 100)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM product_inventory').fetchone()[0], 150)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM subscriber').fetchone()[0], 150)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM party').fetchone()[0], 250)
        self.assertEqual(self.execute('SELECT COUNT(DISTINCT customer_id) FROM changed_customer').fetchone()[0], 10)
        self.assertEqual(self.tags(), self.oracle())

    def test_updates_retractions_aging_fanout_and_delete(self):
        for stage in ('delta', 'payment', 'care_close', 'aging', 'terminate', 'catalog_fanout', 'delete_customer'):
            for step in self.plan['stages'][stage]:
                if step['label'] == 'affected_customers' or step['label'].startswith('qualifying_rows_'):
                    self.assertGreaterEqual(self.execute(step['sql']).fetchone()[0], 0)
                if step['category'] == 'source_change':
                    self.execute(step['sql'])
            self.assertEqual(self.tags(), self.oracle(), stage)
        self.assertEqual(len(self.tags()), 90)

    def test_last_subscription_consent_null_and_bill_fanout(self):
        # Customer 1 has two subscriptions. Bill joins must not multiply spend.
        self.execute("INSERT INTO customer_bill VALUES (1001, 1, '2026-01-01', 50, TRUE)")
        self.execute("INSERT INTO trouble_ticket VALUES (1001, 1, 'open', 'high')")
        self.assertEqual(self.tags(), self.oracle())
        self.assertEqual(self.tags()[1]['payment_risk'], 1)
        self.execute('UPDATE customer SET marketing_consent=NULL WHERE customer_id=1')
        self.assertEqual(self.tags()[1]['campaign_suppressed'], 1)
        self.execute('DELETE FROM product_inventory WHERE customer_id=1')
        self.assertEqual(self.tags()[1]['high_value'], 0)
        self.assertEqual(self.tags()[1]['multi_line'], 0)
        self.assertEqual(self.tags(), self.oracle())

    def test_customer_with_no_optional_facts(self):
        self.execute('DELETE FROM customer_bill WHERE account_id=1')
        self.execute('DELETE FROM product_inventory WHERE customer_id=1')
        self.assertEqual(len(self.tags()), 100)
        self.assertEqual(self.tags(), self.oracle())


class HarnessTests(unittest.TestCase):
    def test_server_duration_units(self):
        self.assertAlmostEqual(am.duration([0, 0, 1423000000]), 1.423)
        self.assertEqual(am.duration([0, 1, 0]), 86400)
        with self.assertRaises(ValueError):
            am.duration([1, 0, 0])

    def test_profile_metrics_use_top_level_once_and_preserve_missing(self):
        fixture = {'data': {'jobSummary': {'stats': {'inputOutputStats': {'inputRowCount': '100'}},
                    'stageSummary': {'ignored': {'inputRowCount': '999'}},
                    'meter': {'measurements': [{'key': 'cpu_wall_time', 'unit': 'ns', 'value': '2500000'},
                                               {'key': 'cpu_wall_time', 'unit': 'cru', 'value': '0.000000'}]}}}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'profile.json'
            path.write_text(json.dumps(fixture))
            result = am.profile_metrics(path)
            self.assertEqual(result['inputRowCount'], 100)
            self.assertIsNone(result['inputBytes'])
            self.assertEqual(result['cpu_wall_seconds'], .0025)
            self.assertEqual(result['job_meter_cru'], 0)

    def test_cost_idle_floor_and_always_on(self):
        cost = b.cost_model(1.86, 1, 10, 60, 15, 30)
        self.assertAlmostEqual(cost['vc_cost_period'], 104.16)
        self.assertAlmostEqual(cost['active_work_proxy_per_cycle'], 1.86 * 10 / 3600)
        self.assertAlmostEqual(b.cost_model(1.86, 1, 10, 60, 1, 30)['vc_cost_period'], 1339.2)
        self.assertAlmostEqual(b.cost_model(1.86, 1, 1, 0, 15, 30)['vc_cost_period'], 89.28)

    def test_invalid_inputs(self):
        for kwargs in ({'schema': 'unrelated_demo'}, {'schema': 'bss_x; DROP TABLE x'},
                       {'changed': 0}, {'subscriptions': 1}):
            with self.assertRaises(ValueError):
                b.prepare(**kwargs)
        with self.assertRaises(ValueError):
            b.cost_model(float('nan'), 1, 10, 60, 15, 30)

    def test_response_shapes(self):
        self.assertTrue(b.failed({'ok': True, 'data': {'status': 'FAILED'}}))
        self.assertTrue(b.failed({'ok': True, 'data': {'status': 'RUNNING'}}))
        self.assertFalse(b.failed({'ok': True, 'data': {'status': 'SUCCEED'}}))
        self.assertFalse(b.failed({'ok': True, 'data': {'rows': [{'state': 'FAILED'}]}}))
        self.assertEqual(b.result_rows({'data': {'columns': ['mismatches'], 'rows': [[0]]}}), [{'mismatches': 0}])
        self.assertEqual(b.result_rows({'data': {'rows': [{'mismatches': 0}]}}), [{'mismatches': 0}])

    def test_write_logging_and_failure_stops_without_retry(self):
        plan = {'stages': {'test': [b.statement('one', 'UPDATE bss_test.customer SET status=1', 'source_change'),
                                   b.statement('two', 'SELECT 1', 'validation', False)]}}
        result = type('Result', (), {'returncode': 0, 'stdout': '{"ok":false,"error":"test error"}', 'stderr': ''})()
        with tempfile.TemporaryDirectory() as temp, patch.object(b, 'ROOT', Path(temp)), \
                patch.object(b.subprocess, 'run', return_value=result) as call:
            with self.assertRaises(RuntimeError):
                b.run_stage(plan, 'test', 'cz', 'DEFAULT', Path(temp) / 'evidence')
            self.assertEqual(call.call_count, 1)
            self.assertIn('--write', call.call_args.args[0])
            self.assertIn('test error', (Path(temp) / 'findings.md').read_text())
            self.assertFalse(json.loads((Path(temp) / 'evidence/manifest.json').read_text())[0]['ok'])


if __name__ == '__main__':
    unittest.main()
