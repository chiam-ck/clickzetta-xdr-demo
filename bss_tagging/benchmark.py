#!/usr/bin/env python3
"""Prepare reviewable SQL; optionally run individual stages through logged cz-cli.

Standard library only. No SDK writes, credentials, scheduling, or cloud creation.
"""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TAG_COLUMNS = ('customer_id', 'rule_version', 'high_value', 'multi_line',
               'payment_risk', 'retention_priority', 'broadband_cross_sell',
               'campaign_suppressed')


def queries(schema):
    parts = re.split(r'^-- @query (\w+)\n',
                     (HERE / 'sql/features.sql').read_text(), flags=re.M)
    return {parts[i]: parts[i + 1].strip().replace('{{schema}}', schema)
            for i in range(1, len(parts), 2)}


def full_query(schema, definitions):
    ctes = []
    for name, sql in definitions.items():
        for dependency in definitions:
            sql = sql.replace(f'{schema}.{dependency}', dependency)
        ctes.append(f'{name} AS (\n{sql}\n)')
    return 'WITH ' + ',\n'.join(ctes) + '\nSELECT * FROM customer_tags'


def statement(label, sql, category, write=True):
    return dict(label=label, sql=sql.strip().rstrip(';'), category=category, write=write)


def prepare(customers=100000, subscriptions=150000, changed=100,
            schema='bss_tagging_demo', as_of='2026-09-10'):
    if not re.fullmatch(r'bss_[a-z0-9_]+', schema):
        raise ValueError('Schema must start bss_ and contain lowercase letters/digits/underscores')
    if not 10 <= customers <= 10000000 or not customers <= subscriptions <= 50000000:
        raise ValueError('Require 10..10M customers and customers..50M subscriptions')
    if not 1 <= changed <= customers:
        raise ValueError('Changed customers must be between 1 and the customer count')
    date = dt.date.fromisoformat(as_of)
    definitions = queries(schema)
    full = full_query(schema, definitions)
    s = schema
    # Bijective permutation spreads the selected customer keys across the base.
    stride = 7919
    while math.gcd(stride, customers) != 1:
        stride += 2
    party_count = customers + subscriptions
    digits = len(str(party_count - 1))
    digit_sql = '(' + ' UNION ALL '.join(f'SELECT {i} AS d' for i in range(10)) + ')'
    number_expr = ' + '.join(f'd{i}.d * {10**i}' for i in range(digits))
    sequence = (f'SELECT {number_expr} + 1 AS n FROM ' +
                ' CROSS JOIN '.join(f'{digit_sql} d{i}' for i in range(digits)) +
                f' WHERE {number_expr} < {party_count}')
    ddl = (HERE / 'sql/model.sql').read_text().replace('{{schema}}', s)
    ddl = re.sub(r'^--.*$', '', ddl, flags=re.M)
    # Templates contain no semicolons inside strings or comments.
    init = [statement(f'ddl_{i:02}', sql, 'setup') for i, sql in
            enumerate(ddl.split(';')) if sql.strip()]
    init += [statement('sequence', f'CREATE TABLE {s}.seed_numbers AS {sequence}', 'seed'),
             statement('changed_keys', f'CREATE TABLE {s}.changed_customer AS '
                       f'SELECT ((n - 1) * {stride}) % {customers} + 1 AS customer_id '
                       f'FROM {s}.seed_numbers WHERE n <= {changed}', 'seed')]
    seed = {
        'party': f"SELECT n, 'individual', CONCAT('Synthetic party ', CAST(n AS STRING)) FROM {s}.seed_numbers",
        'customer': f"SELECT n, n, 'active', CASE WHEN n % 7 = 0 THEN FALSE ELSE TRUE END FROM {s}.seed_numbers WHERE n <= {customers}",
        'billing_account': f"SELECT n, n, 'SGD' FROM {s}.seed_numbers WHERE n <= {customers}",
        'subscriber': f"SELECT n, n + {customers}, ((n-1) % {customers})+1 FROM {s}.seed_numbers WHERE n <= {subscriptions}",
        'product_offering': "VALUES (1, 'mobile'), (2, 'broadband')",
        'product_inventory': f"SELECT n, ((n-1) % {customers})+1, ((n-1) % {customers})+1, n, CASE WHEN n % 5 = 0 THEN 2 ELSE 1 END, 'active', CAST(40 + n % 100 AS DECIMAL(12,2)), date_add('{as_of}', CAST(n % 60 AS INT)), CASE WHEN n % 60 <= 30 THEN TRUE ELSE FALSE END FROM {s}.seed_numbers WHERE n <= {subscriptions}",
        'customer_bill': f"SELECT n, n, date_add('{as_of}', -CAST(n % 60 AS INT)), CAST(CASE WHEN n % 9 = 0 THEN 75 ELSE 0 END AS DECIMAL(12,2)), CASE WHEN n % 60 >= 30 THEN TRUE ELSE FALSE END FROM {s}.seed_numbers WHERE n <= {customers}",
        'trouble_ticket': f"SELECT n, n, 'open', 'high' FROM {s}.seed_numbers WHERE n <= {customers} AND n % 20 = 0",
    }
    init += [statement('seed_' + table, f'INSERT INTO {s}.{table} {sql}', 'seed')
             for table, sql in seed.items()]
    init += [statement('create_' + name, f'CREATE DYNAMIC TABLE {s}.{name} AS {sql}', 'setup')
             for name, sql in definitions.items()]
    init += [statement('create_full', f'CREATE TABLE {s}.customer_tags_full AS {full}', 'bootstrap_control')]

    def finish():
        steps = [statement('refresh_' + name, f'REFRESH DYNAMIC TABLE {s}.{name}', 'incremental')
                 for name in definitions]
        steps += [statement('history', 'SHOW DYNAMIC TABLE REFRESH HISTORY '
                            f"WHERE schema_name='{s}' LIMIT 10000", 'evidence', False)]
        differences = ' + '.join(f'ABS(COALESCE(n.{c}, 0) - COALESCE(o.{c}, 0))'
                                 for c in TAG_COLUMNS[2:])
        steps += [statement('tag_transitions',
                  f'SELECT COALESCE(SUM({differences}), 0) AS flag_transitions, '
                  'SUM(CASE WHEN o.customer_id IS NULL THEN 1 ELSE 0 END) AS added_customers, '
                  'SUM(CASE WHEN n.customer_id IS NULL THEN 1 ELSE 0 END) AS removed_customers '
                  f'FROM {s}.customer_tags n FULL OUTER JOIN {s}.customer_tags_full o '
                  'ON n.customer_id=o.customer_id', 'validation_measurement', False)]
        # Identical final schema/materialization, reads only base tables via CTEs.
        steps += [statement('full_recompute', f'INSERT OVERWRITE {s}.customer_tags_full {full}', 'full')]
        columns = ', '.join(TAG_COLUMNS)
        steps += [statement('verify',
                   f'SELECT COUNT(*) AS mismatches FROM (SELECT {columns} FROM {s}.customer_tags '
                   f'EXCEPT SELECT {columns} FROM {s}.customer_tags_full) a', 'validation', False),
                  statement('verify_reverse',
                   f'SELECT COUNT(*) AS mismatches FROM (SELECT {columns} FROM {s}.customer_tags_full '
                   f'EXCEPT SELECT {columns} FROM {s}.customer_tags) a', 'validation', False),
                  statement('verify_counts',
                   f'SELECT COUNT(*) AS customers, COUNT(DISTINCT customer_id) AS distinct_customers, '
                   f'(SELECT COUNT(*) FROM {s}.customer) AS expected_customers '
                   f'FROM {s}.customer_tags', 'validation', False),
                  statement('tag_counts', 'SELECT ' + ', '.join(f'SUM({c}) AS {c}' for c in TAG_COLUMNS[2:]) +
                            f' FROM {s}.customer_tags', 'consumption', False)]
        return steps

    selected = f'customer_id IN (SELECT customer_id FROM {s}.changed_customer)'
    bills = f'account_id IN (SELECT account_id FROM {s}.billing_account WHERE {selected})'
    cases = {
        'bootstrap': [],
        'noop': [],
        'delta': [
            f'UPDATE {s}.customer SET marketing_consent = NOT marketing_consent WHERE {selected}',
            f'UPDATE {s}.product_inventory SET monthly_charge = monthly_charge + 1 WHERE {selected}',
        ],
        'payment': [f'UPDATE {s}.customer_bill SET outstanding_amount=0 WHERE {bills} AND outstanding_amount > 0'],
        'care_close': [f"UPDATE {s}.trouble_ticket SET status='closed' WHERE {selected} AND status='open'"],
        'terminate': [f"UPDATE {s}.product_inventory SET status='terminated' WHERE {selected} AND status='active'"],
        'delete_customer': [f'DELETE FROM {s}.customer WHERE {selected}'],
        # Clock crossings are source changes, never CURRENT_DATE in a DT definition.
        'aging': [
            f"UPDATE {s}.product_inventory SET renewal_due=TRUE WHERE contract_end=date_add('{as_of}', 31) AND renewal_due=FALSE",
            f"UPDATE {s}.product_inventory SET renewal_due=FALSE WHERE contract_end='{as_of}' AND renewal_due=TRUE",
            f"UPDATE {s}.customer_bill SET overdue_30=TRUE WHERE due_date=date_add('{as_of}', -29) AND overdue_30=FALSE",
        ],
        'catalog_fanout': [f"UPDATE {s}.product_offering SET family='broadband' WHERE offering_id=1"],
    }
    stages = {'init': init}
    for name, sqls in cases.items():
        changes = []
        affected = []
        for i, sql in enumerate(sqls):
            table = sql.split()[2] if sql.startswith('DELETE') else sql.split()[1]
            predicate = sql.split(' WHERE ', 1)[1]
            changes.append(statement(f'qualifying_rows_{i}',
                           f'SELECT COUNT(*) AS qualifying_source_rows FROM {table} WHERE {predicate}',
                           'validation_measurement', False))
            changes.append(statement(f'change_{i}', sql, 'source_change'))
            if table.endswith('.customer_bill'):
                affected.append(f'SELECT customer_id FROM {s}.billing_account WHERE account_id IN '
                                f'(SELECT account_id FROM {table} WHERE {predicate})')
            elif table.endswith('.product_offering'):
                affected.append(f'SELECT customer_id FROM {s}.product_inventory WHERE offering_id IN '
                                f'(SELECT offering_id FROM {table} WHERE {predicate})')
            else:
                affected.append(f'SELECT customer_id FROM {table} WHERE {predicate}')
        if affected:
            changes.insert(0, statement('affected_customers',
                           'SELECT COUNT(DISTINCT customer_id) AS affected_customers FROM (' +
                           ' UNION ALL '.join(affected) + ') a', 'validation_measurement', False))
        stages[name] = changes + finish()
    stages['explain'] = [statement('explain_' + name, f'EXPLAIN REFRESH DYNAMIC TABLE {s}.{name}', 'explain', False)
                         for name in definitions]
    stages['explain'].append(statement('explain_full_control', 'EXPLAIN ' + full, 'explain', False))
    stages['consume'] = [statement('campaign_audience',
                          f'SELECT customer_id, rule_version FROM {s}.customer_tags '
                          'WHERE broadband_cross_sell=1 AND campaign_suppressed=0 ORDER BY customer_id LIMIT 100',
                          'consumption', False)]
    return dict(version=1, config=dict(customers=customers, subscriptions=subscriptions,
                changed=changed, schema=s, as_of=date.isoformat(), seed='deterministic-v1'), stages=stages)


def redact(text):
    """Remove local secret values if a CLI error unexpectedly includes them."""
    values = list(os.environ.get(k, '') for k in os.environ
                  if re.search('PASSWORD|SECRET|TOKEN|PAT|API_KEY|CZ_', k))
    env_file = ROOT / '.env.clickzetta'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                values.append(line.split('=', 1)[1].strip().strip('"\''))
    for value in sorted(set(values), key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, '[REDACTED]')
    return text


def failed(payload):
    if isinstance(payload, dict):
        if payload.get('ok') is False or payload.get('success') is False:
            return True
        if str(payload.get('status', payload.get('state', ''))).upper() in {
                'FAILED', 'FAILURE', 'ERROR', 'CANCELLED', 'CANCELED', 'RUNNING', 'PENDING'}:
            return True
        # Returned table rows can legitimately contain historic FAILED/RUNNING
        # states. They are data, not the status of this CLI command.
        return any(failed(payload[k]) for k in ('data', 'result', 'error')
                   if isinstance(payload.get(k), dict))
    return False


def job_ids(payload):
    found = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == 'job_id' and re.fullmatch(r'[0-9]+', str(value)):
                found.add(str(value))
            else:
                found.update(job_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            found.update(job_ids(value))
    return found


def result_rows(payload):
    """Accept CLI rows as objects, or arrays paired with column metadata."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get('rows')
    if isinstance(rows, list):
        if rows and isinstance(rows[0], dict):
            return [{str(k).lower(): v for k, v in row.items()} for row in rows]
        columns = payload.get('columns', [])
        names = [str(c.get('name', c.get('column_name', ''))) if isinstance(c, dict) else str(c)
                 for c in columns]
        return [dict(zip([n.lower() for n in names], row)) for row in rows]
    for value in payload.values():
        found = result_rows(value)
        if found:
            return found
    return []


def run_stage(plan, stage, profile, vcluster, evidence, timeout=300):
    evidence.mkdir(parents=True, exist_ok=False)
    (evidence / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    records = []
    def execute(step, index):
        explain_flag = str(step['sql'].startswith('EXPLAIN REFRESH')).lower()
        cmd = ['cz-cli', '--profile', profile, 'sql', '--sync', '--timeout', str(timeout),
               '--no-limit', '--no-truncate', '-o', 'json', '--vcluster', vcluster,
               '--set', 'cz.optimizer.explain.can.incrementalize=' + explain_flag]
        if step['write']:
            cmd.append('--write')
        if step.get('profile_job'):
            cmd = ['cz-cli', '--profile', profile, 'sql', '--job-profile', step['profile_job'], '-o', 'json']
        if step.get('status_job'):
            cmd = ['cz-cli', '--profile', profile, 'job', 'status', step['status_job'], '-o', 'json']
        start = time.time()
        stem = evidence / f'{index:03}_{step["label"]}'
        stem.with_suffix('.sql').write_text(step['sql'] + ';\n')
        try:
            proc = subprocess.run(cmd, input=step['sql'], capture_output=True, text=True,
                                  timeout=timeout + 30)
            raw, stderr = redact(proc.stdout), redact(proc.stderr)
            stem.with_suffix('.json').write_text(raw)
            stem.with_suffix('.stderr.txt').write_text(stderr)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {'ok': False, 'error': 'CLI did not return valid JSON'}
            error = proc.returncode != 0 or failed(payload)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raw = redact(str(exc))
            payload, error = {'ok': False, 'error': raw}, True
            stem.with_suffix('.json').write_text(json.dumps(payload))
        validation_error = None
        if not error and step['category'] == 'validation':
            rows = result_rows(payload)
            try:
                if step['label'] == 'verify_counts':
                    passed = (int(rows[0]['customers']) == int(rows[0]['distinct_customers'])
                              == int(rows[0]['expected_customers']))
                else:
                    passed = int(rows[0]['mismatches']) == 0
            except (KeyError, IndexError, TypeError, ValueError):
                passed = False
            if not passed:
                error = True
                validation_error = 'Correctness check failed or CLI result shape was not recognized'
        record = dict(label=step['label'], category=step['category'],
                      write=step['write'], start_utc=dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(),
                      client_elapsed_seconds=time.time() - start,
                      job_ids=sorted(job_ids(payload)), ok=not error)
        records.append(record)
        (evidence / 'manifest.json').write_text(json.dumps(records, indent=2) + '\n')
        if error:
            with (ROOT / 'findings.md').open('a') as handle:
                handle.write(f'\n| BSS {stage}/{step["label"]} | Exact error/output: '
                             + ((validation_error + ': ') if validation_error else '')
                             + raw.replace('|', '\\|').replace('\n', '<br>')
                             + '. Minimal next step: inspect the captured SQL and terminal error; '
                             'fix that statement before any retry. Timeout outcome is unknown; '
                             'check job state before resubmitting a write. No automatic retry. | | '
                             + str(stem) + ' |\n')
            raise RuntimeError(f'{step["label"]} failed; recorded in findings.md. No retry performed.')
        print(f'{step["label"]}: captured ({record["client_elapsed_seconds"]:.2f}s client elapsed)', flush=True)
        return payload

    for i, step in enumerate(plan['stages'][stage]):
        execute(step, i)
    # Read-only telemetry, exactly scoped to jobs captured above. Missing telemetry
    # is an evidence gap, never interpreted as zero consumption.
    ids = sorted({jid for row in records if row['category'] not in {'evidence', 'telemetry'}
                  for jid in row['job_ids']})
    if ids:
        date_filter = min(row['start_utc'][:10] for row in records)
        telemetry = ('SELECT job_id, workspace_name, virtual_cluster, start_time, end_time, cru, '
                     'execution_time, input_bytes, output_bytes, rows_inserted, rows_updated, rows_deleted, '
                     'cache_hit, input_tables, status '
                     f"FROM sys.information_schema.job_history WHERE pt_date >= '{date_filter}' AND job_id IN (" +
                     ','.join("'" + jid + "'" for jid in ids) + ')')
        execute(statement('job_metrics', telemetry, 'telemetry', False), len(records))


def cost_model(rate, cru, active_seconds, idle_seconds, cadence_minutes, days):
    if any(not math.isfinite(x) for x in (rate, cru, active_seconds, idle_seconds, cadence_minutes, days)):
        raise ValueError('Cost inputs must be finite')
    if min(rate, active_seconds, idle_seconds) < 0 or min(cru, cadence_minutes, days) <= 0:
        raise ValueError('Invalid cost inputs')
    interval = cadence_minutes * 60
    # Dedicated single-replica VC, regular non-overlapping cycles, documented
    # one-minute minimum per activation. This is a scenario, not metered usage.
    seconds = min(interval, max(60, active_seconds + idle_seconds))
    runs = days * 86400 / interval
    return dict(status='ILLUSTRATIVE_NOT_MEASURED', runs=runs,
                active_work_proxy_per_cycle=rate * cru * active_seconds / 3600,
                vc_cost_per_cycle=rate * cru * seconds / 3600,
                vc_cost_period=rate * cru * seconds * runs / 3600,
                always_on_period=rate * cru * days * 24,
                stable_cadence=active_seconds < interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--customers', type=int, default=100000)
    prep.add_argument('--subscriptions', type=int, default=150000)
    prep.add_argument('--changed', type=int, default=100)
    prep.add_argument('--schema', default='bss_tagging_demo')
    prep.add_argument('--as-of', default='2026-09-10')
    prep.add_argument('--output', type=Path, required=True)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    run.add_argument('--stage', required=True)
    run.add_argument('--profile', default='cz')
    run.add_argument('--vcluster', default='DEFAULT')
    run.add_argument('--evidence', type=Path, required=True)
    run.add_argument('--execute', action='store_true')
    cost = sub.add_parser('cost')
    cost.add_argument('--rate', type=float, required=True, help='USD per CRU-hour; verify your contract')
    cost.add_argument('--cru', type=float, default=1)
    cost.add_argument('--active-seconds', type=float, required=True)
    cost.add_argument('--idle-seconds', type=float, default=60)
    cost.add_argument('--cadence-minutes', type=float, default=15)
    cost.add_argument('--days', type=float, default=30)
    args = vars(parser.parse_args())
    command = args.pop('command')
    if command == 'prepare':
        output = args.pop('output')
        plan = prepare(**args)
        output.mkdir(parents=True, exist_ok=False)
        (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
        for stage, steps in plan['stages'].items():
            (output / f'{stage}.sql').write_text('\n\n'.join(
                f'-- {step["category"]}: {step["label"]}\n{step["sql"]};' for step in steps) + '\n')
        print(json.dumps(plan['config'], indent=2))
        print(f'Prepared {len(plan["stages"])} stages in {output}. No remote operations.')
    elif command == 'run':
        plan = json.loads(args.pop('plan').read_text())
        execute = args.pop('execute')
        if args['stage'] not in plan['stages']:
            parser.error('Unknown stage')
        if not execute:
            print(json.dumps(plan['stages'][args['stage']], indent=2))
        else:
            run_stage(plan, **args)
    else:
        print(json.dumps(cost_model(**args), indent=2))


if __name__ == '__main__':
    main()
