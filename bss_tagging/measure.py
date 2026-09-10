#!/usr/bin/env python3
"""Run the explicitly authorized BSS measurement experiment and keep evidence."""
import argparse
import json
from pathlib import Path

from benchmark import ROOT, prepare, queries, run_stage, statement


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--vcluster', default='DEFAULT')
    parser.add_argument('--source-run', default='bootstrap_01')
    parser.add_argument('--scale', type=int, choices=(1, 10), default=1)
    args = parser.parse_args()
    if args.phase == 'collect':
        for source in sorted((ROOT / 'evidence/bss_tagging').glob(args.source_run + '*')):
            manifest_path = source / 'manifest.json'
            if not manifest_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text())
            if not manifest or manifest[-1]['label'] != 'job_metrics' or not all(r['ok'] for r in manifest):
                continue
            target = source.parent / ('profiles_' + source.name)
            if target.exists():
                continue
            steps = [dict(statement(row['label'], '', row['category'], False), profile_job=row['job_ids'][0])
                     for row in manifest if row['category'] in {'incremental', 'full', 'source_change', 'consumption', 'seed', 'bootstrap_control'}
                     and len(row['job_ids']) == 1]
            if steps:
                print(f'PROFILES {source.name}', flush=True)
                run_stage({'stages': {'profiles': steps}}, 'profiles', 'cz', args.vcluster, target)
        return
    schema = 'bss_tagging_100k' if args.scale == 1 else 'bss_tagging_1m'
    plan = prepare(customers=100000 * args.scale, subscriptions=150000 * args.scale, schema=schema)
    plan['stages']['explain'] = [statement('explain_' + name, 'EXPLAIN ' + sql, 'explain', False)
                               for name, sql in queries(schema).items()] + plan['stages']['explain'][-1:]
    plan['stages']['preflight'] = [
        statement('clusters', 'SHOW VCLUSTERS', 'environment', False),
        statement('schemas', "SHOW SCHEMAS LIKE 'bss_%'", 'environment', False),
        statement('job_history_columns', 'DESC sys.information_schema.job_history', 'environment', False),
    ]
    plan['stages']['setup_measurement'] = [
        statement('role', 'SELECT CURRENT_ROLES() AS active_roles', 'environment', False),
        statement('usage_columns', 'DESC sys.information_schema.instance_usage', 'environment', False),
        statement('storage_columns', 'DESC sys.information_schema.storage_metering', 'environment', False),
        statement('table_columns', 'DESC sys.information_schema.tables', 'environment', False),
        statement('create_benchmark_cluster',
                  'CREATE VCLUSTER BSS_BENCH VCLUSTER_SIZE=1 VCLUSTER_TYPE=GENERAL '
                  'AUTO_SUSPEND_IN_SECOND=60 AUTO_RESUME=TRUE QUERY_RUNTIME_LIMIT_IN_SECOND=300', 'setup'),
    ]
    plan['stages']['metering'] = [
        statement('usage', 'SELECT sku_category, sku_code, sku_name, measurement_start, measurement_end, '
                  'measurements_unit, measurements_consumption, price_rate, amount, discount_rate, '
                  'total_after_discount, billing_unit, billing_mode FROM sys.information_schema.instance_usage '
                  "WHERE measurement_start >= '2026-09-10' ORDER BY measurement_start DESC LIMIT 1000", 'metering', False),
        statement('storage', 'SELECT sku_category, sku_name, measurement_start, measurement_end, '
                  'measurements_unit, measurements_consumption, price_rate, amount, total_after_discount '
                  "FROM sys.information_schema.storage_metering WHERE measurement_start >= '2026-09-09' "
                  'ORDER BY measurement_start DESC LIMIT 1000', 'metering', False),
        statement('table_sizes', 'SELECT table_schema, table_name, table_type, row_count, bytes FROM sys.information_schema.tables '
                  "WHERE table_schema LIKE 'bss_tagging_%' AND delete_time IS NULL", 'storage', False),
        statement('all_benchmark_jobs', 'SELECT job_id, job_type, job_sub_type, status, cru, start_time, end_time, '
                  'execution_time, input_bytes, output_bytes, rows_produced, rows_inserted, rows_updated, rows_deleted, '
                  'cache_hit, input_tables, output_tables, pt_date FROM sys.information_schema.job_history '
                  "WHERE pt_date >= '2026-09-10' AND virtual_cluster='BSS_BENCH' ORDER BY start_time", 'telemetry', False),
        statement('cluster_state', 'DESC VCLUSTER BSS_BENCH', 'environment', False),
    ]
    plan['stages']['jobs_probe'] = [
        statement('recent_jobs', 'SHOW JOBS IN VCLUSTER BSS_BENCH LIMIT 1000', 'telemetry', False),
        statement('history_dates', 'SELECT pt_date, COUNT(*) AS jobs FROM sys.information_schema.job_history '
                  "WHERE pt_date >= '2026-09-09' GROUP BY pt_date", 'telemetry', False),
    ]
    plan['stages']['jobs_snapshot'] = plan['stages']['jobs_probe'][:1]
    plan['stages']['prices'] = [
        statement('observed_rates', 'SELECT DISTINCT sku_category, sku_code, sku_name, price_rate, '
                  'discount_rate, measurements_unit, billing_unit, billing_mode '
                  "FROM sys.information_schema.instance_usage WHERE measurement_start >= '2026-09-03'",
                  'metering', False),
    ]
    plan['stages']['shutdown'] = [
        statement('before_shutdown', 'DESC VCLUSTER BSS_BENCH', 'environment', False),
        statement('suspend_benchmark', 'ALTER VCLUSTER BSS_BENCH SUSPEND', 'cleanup'),
        statement('after_shutdown', 'DESC VCLUSTER BSS_BENCH', 'environment', False),
    ]
    plan['stages']['final_capture'] = plan['stages']['metering'] + plan['stages']['jobs_snapshot'] + plan['stages']['shutdown']
    if args.phase == 'profile_probe':
        manifest = json.loads((ROOT / 'evidence/bss_tagging' / args.source_run / 'manifest.json').read_text())
        selected = next(row for row in manifest if row['label'] == 'full_recompute')
        jid = selected['job_ids'][0]
        plan['stages']['profile_probe'] = [
            dict(statement('status', '', 'telemetry', False), status_job=jid),
            dict(statement('profile', '', 'telemetry', False), profile_job=jid),
        ]
    if args.phase == 'profiles':
        manifest = json.loads((ROOT / 'evidence/bss_tagging' / args.source_run / 'manifest.json').read_text())
        steps = []
        for row in manifest:
            if row['category'] not in {'incremental', 'full', 'source_change', 'consumption', 'seed', 'bootstrap_control'}:
                continue
            if not row['ok'] or len(row['job_ids']) != 1:
                continue
            steps.append(dict(statement(row['label'], '', row['category'], False), profile_job=row['job_ids'][0]))
        plan['stages']['profiles'] = steps
    if args.phase == 'scale_suite':
        cases = [('init', 'init'), ('bootstrap', 'bootstrap'), ('delta', 'delta_warmup')]
        cases += [('delta', f'delta_{i:02}') for i in range(1, 6)]
        cases += [('noop', 'noop'), ('consume', 'consume')]
        for stage, suffix in cases:
            print(f'STAGE {suffix}', flush=True)
            run_stage(plan, stage, 'cz', args.vcluster,
                      ROOT / 'evidence/bss_tagging' / f'{args.run_id}_{suffix}')
    elif args.phase == 'suite':
        cases = [('explain', 'explain'), ('delta', 'delta_warmup')]
        cases += [('delta', f'delta_{i:02}') for i in range(1, 6)]
        cases += [('noop', f'noop_{i:02}') for i in range(1, 6)]
        cases += [(name, name) for name in ('payment', 'care_close', 'aging', 'terminate',
                                          'catalog_fanout', 'delete_customer', 'consume')]
        for stage, suffix in cases:
            print(f'STAGE {suffix}', flush=True)
            run_stage(plan, stage, 'cz', args.vcluster,
                      ROOT / 'evidence/bss_tagging' / f'{args.run_id}_{suffix}')
    else:
        run_stage(plan, args.phase, 'cz', args.vcluster,
                  ROOT / 'evidence/bss_tagging' / args.run_id)


if __name__ == '__main__':
    main()
