#!/usr/bin/env python3
"""Summarize captured server evidence without issuing any remote queries."""
import csv
import datetime as dt
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / 'evidence/bss_tagging'


def load_rows(path):
    return json.loads(path.read_text()).get('rows', [])


def duration(value):
    # Arrow month/day/nanosecond interval representation from SHOW commands.
    if isinstance(value, list) and len(value) == 3:
        if value[0] != 0:
            raise ValueError('Unexpected month-valued duration')
        return value[1] * 86400 + value[2] / 1e9
    return float(value)


def profile_metrics(path):
    summary = json.loads(path.read_text())['data']['jobSummary']
    stats = summary.get('stats', {}).get('inputOutputStats', {})
    meter = summary.get('meter', {}).get('measurements', [])
    result = {key: int(stats[key]) if key in stats else None for key in
              ('inputRowCount', 'inputBytes', 'inputDeltaBytes', 'inputDiskBytes', 'inputCacheBytes',
               'outputRowCount', 'outputBytes', 'spillingBytes')}
    ns = [float(m['value']) for m in meter if m['key'] == 'cpu_wall_time' and m['unit'] == 'ns']
    cru = [float(m['value']) for m in meter if m['key'] == 'cpu_wall_time' and m['unit'] == 'cru']
    result['cpu_wall_seconds'] = sum(ns) / 1e9 if ns else None
    result['job_meter_cru'] = sum(cru) if cru else None
    return result


def collect():
    jobs = {}
    for path in sorted(EVIDENCE.glob('*/*_recent_jobs.json')):
        for row in load_rows(path):
            jobs[row['job_id']] = row
    output = []
    for folder in sorted(EVIDENCE.iterdir()):
        if not folder.name.startswith(('measured2_', 'scale1m_')) or folder.name.endswith(('explain', 'init')):
            continue
        manifest_path = folder / 'manifest.json'
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text())
        if not any(r['label'] == 'verify_counts' for r in manifest):
            continue
        plan = json.loads((folder / 'plan.json').read_text())
        row = {'run_id': folder.name, 'customers': plan['config']['customers'],
               'subscriptions': plan['config']['subscriptions'], 'selected_customers': plan['config']['changed']}
        history = {}
        history_path = next(folder.glob('*_history.json'), None)
        if history_path:
            history = {r['job_id']: r for r in load_rows(history_path)}
        for label, field in [('affected_customers', 'affected_customers'), ('tag_transitions', 'flag_transitions'),
                             ('verify', 'mismatches'), ('verify_reverse', 'mismatches'),
                             ('verify_counts', 'customers')]:
            path = next(folder.glob('*_' + label + '.json'), None)
            if path:
                values = load_rows(path)
                row[label] = values[0][field] if values else None
        row['changed_source_rows'] = sum(load_rows(p)[0]['qualifying_source_rows']
                                         for p in folder.glob('*_qualifying_rows_*.json'))
        modes = []
        for category in ('incremental', 'full', 'source_change', 'consumption'):
            selected = [r for r in manifest if r['category'] == category]
            profiles = []
            server_seconds = []
            for item in selected:
                jid = item['job_ids'][0]
                if jid in history and category == 'incremental':
                    modes.append(item['label'].removeprefix('refresh_') + ':' + history[jid]['refresh_mode'])
                    server_seconds.append(duration(history[jid]['duration']))
                elif jid in jobs:
                    server_seconds.append(duration(jobs[jid]['execution_time']))
                path = next((EVIDENCE / ('profiles_' + folder.name)).glob('*_' + item['label'] + '.json'), None)
                if path:
                    profiles.append(profile_metrics(path))
            row[category + '_server_seconds'] = sum(server_seconds) if len(server_seconds) == len(selected) else None
            row[category + '_profile_count'] = len(profiles)
            row[category + '_job_count'] = len(selected)
            for metric in ('inputRowCount', 'inputBytes', 'inputDeltaBytes', 'inputDiskBytes', 'inputCacheBytes',
                           'outputRowCount', 'outputBytes', 'spillingBytes', 'cpu_wall_seconds', 'job_meter_cru'):
                row[category + '_' + metric] = (sum(p[metric] for p in profiles)
                    if len(profiles) == len(selected) and all(p[metric] is not None for p in profiles) else None)
        row['refresh_modes'] = ';'.join(modes)
        changes = [r for r in manifest if r['category'] == 'source_change']
        tag_job = next((r['job_ids'][0] for r in manifest if r['label'] == 'refresh_customer_tags'), None)
        if changes and tag_job in history and changes[-1]['job_ids'][0] in jobs:
            committed = dt.datetime.fromisoformat(jobs[changes[-1]['job_ids'][0]]['end_time'])
            ready = dt.datetime.fromisoformat(history[tag_job]['end_time'])
            row['last_source_commit_to_tags_seconds'] = (ready - committed).total_seconds()
        row['all_statements_ok'] = all(r['ok'] for r in manifest)
        output.append(row)
    return output


def main():
    rows = collect()
    destination = ROOT / 'bss_tagging/measured_results.csv'
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with destination.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    summaries = {}
    for prefix in ('measured2_delta_0', 'measured2_noop_0', 'scale1m_delta_0'):
        selected = [r for r in rows if r['run_id'].startswith(prefix)]
        metrics = {}
        for key in ('incremental_server_seconds', 'full_server_seconds', 'incremental_cpu_wall_seconds',
                    'full_cpu_wall_seconds', 'incremental_inputRowCount', 'full_inputRowCount',
                    'incremental_inputBytes', 'full_inputBytes', 'incremental_outputBytes', 'full_outputBytes',
                    'incremental_job_meter_cru', 'full_job_meter_cru'):
            values = [r[key] for r in selected if r.get(key) is not None]
            if values:
                metrics[key] = {'n': len(values), 'median': statistics.median(values),
                                'p95_nearest_rank': sorted(values)[math.ceil(.95 * len(values)) - 1],
                                'min': min(values), 'max': max(values)}
        summaries[prefix] = metrics
    (ROOT / 'bss_tagging/measured_summary.json').write_text(json.dumps(summaries, indent=2) + '\n')
    rates_path = EVIDENCE / 'observed_prices/000_observed_rates.json'
    if rates_path.exists():
        rates = load_rows(rates_path)
        gp = next(r for r in rates if r['sku_code'] == 'vc_gp_event')
        if gp['measurements_unit'] != 'CRU*hour':
            raise ValueError('Unknown account compute billing unit')
        rate = float(gp['price_rate']) * gp['discount_rate']
        costs = {'account_usd_per_cru_hour': rate, 'experiment_invoice_usd': None,
                 'invoice_status': 'Current experiment compute billing has not posted', 'cycles_per_30_days_at_15min': 2880,
                 'projection_assumptions': '1 CRU, 60-second idle tail, server-duration sum as work proxy; excludes ingestion, client gaps, storage and consumption',
                 'scenarios': {}}
        for name, metrics in summaries.items():
            for method in ('incremental', 'full'):
                key = method + '_server_seconds'
                if key not in metrics:
                    continue
                seconds = metrics[key]['median']
                costs['scenarios'][name + method] = {
                    'measured_server_seconds_median': seconds,
                    'busy_capacity_proxy_usd': seconds * rate / 3600,
                    'modeled_vc_usd_per_cycle': max(60, seconds + 60) * rate / 3600,
                    'modeled_vc_usd_per_30_days': max(60, seconds + 60) * rate / 3600 * 2880}
        sizes_path = EVIDENCE / 'final_capture/002_table_sizes.json'
        if sizes_path.exists():
            costs['storage_snapshot'] = {}
            sizes = load_rows(sizes_path)
            for schema in sorted({r['table_schema'] for r in sizes}):
                selected = [r for r in sizes if r['table_schema'] == schema]
                all_bytes = sum(r['bytes'] for r in selected)
                derived_bytes = sum(r['bytes'] for r in selected if r['table_type'] in {'DYNAMIC_TABLE', 'MATERIALIZED_VIEW'})
                costs['storage_snapshot'][schema] = {'all_object_bytes': all_bytes,
                    'derived_and_internal_state_bytes': derived_bytes,
                    'all_objects_live_storage_month_proxy_usd_at_0025': all_bytes / 2**30 * .025,
                    'derived_live_storage_month_proxy_usd_at_0025': derived_bytes / 2**30 * .025}
        stopped_path = EVIDENCE / 'final_capture/008_after_shutdown.json'
        if stopped_path.exists():
            info = {r['info_name']: r['info_value'] for r in load_rows(stopped_path)}
            if info['state'] == 'SUSPENDED':
                seconds = (dt.datetime.fromisoformat(info['last_modified_time']) - dt.datetime.fromisoformat(info['created_time'])).total_seconds()
                costs['dedicated_cluster_creation_to_suspension_seconds'] = seconds
                costs['dedicated_cluster_continuously_running_cost_bound_usd'] = seconds * rate / 3600
        (ROOT / 'bss_tagging/measured_costs.json').write_text(json.dumps(costs, indent=2) + '\n')
    print(json.dumps(summaries, indent=2))
    print(f'{len(rows)} completed/partial cases summarized; missing metrics remain empty.')


if __name__ == '__main__':
    main()
