#!/usr/bin/env python3
"""Run evals/cases_v1.jsonl against v0.2 documented rules (before) and scripts/listing_metrics.py (after).

v0.2 had no metrics code: the "before" condition re-implements the rules exactly as
written in references/analysis.md at commit 15d4c71, so both conditions see identical
inputs. Output: per-case judgments (JSONL) and an aggregate summary (JSON).
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('listing_metrics', ROOT / 'scripts/listing_metrics.py')
lm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lm)

V02_BASIS = {'建筑': 'building', '建筑面积': 'building', '套内': 'inner', '套内面积': 'inner'}  # data-model.md: 建筑/套内/未知


def before(payload):
    """v0.2 rules: >=5 comps for stats else 'insufficient'; exact-label area basis; no payment,
    verification-code or censoring logic; exited-only median once >=10 exits."""
    rows = []
    listings = [lm.normalise(x) for x in payload.get('listings', [])]
    for item in listings:
        item['area'] = dict(item['area'], basis=V02_BASIS.get(item['area'].get('raw_basis') or '', 'unknown'))
    for item in listings:
        metric = lm.comparable_metric(item)
        comps = {}
        for other in listings:
            if other is item or other.get('transaction_type') != item.get('transaction_type'):
                continue
            if item.get('property_id') and other.get('property_id') == item.get('property_id'):
                continue
            if item['area']['sqm'] and other['area']['sqm'] and abs(other['area']['sqm'] / item['area']['sqm'] - 1) > .15:
                continue
            if item.get('transaction_type') == 'sale' and other['area']['basis'] != item['area']['basis']:
                continue
            if lm.comparable_metric(other) is None:
                continue
            key = other.get('property_id') or other.get('post_id')
            if key not in comps or str(other.get('observed_on')) > str(comps[key].get('observed_on')):
                comps[key] = other
        n = len(comps) if metric is not None else 0
        row = {'price_status': item['price']['status'], 'comp_n': n,
               'comp_status': 'quantiles' if n >= 5 else 'insufficient', 'has_median': n >= 5, 'has_range': n >= 5, 'flags': []}
        if item['price']['status'] == 'conflict':
            row['flags'].append('price_conflict')
        if item.get('transaction_type') == 'rent':
            row.update(_monthly(item))
        rows.append(row)
    exit_rows = payload.get('exit_durations')
    exit_summary = None
    if exit_rows:
        exited = sorted(d['days'] for d in exit_rows if d['exited'])
        exit_summary = {'reported_median': lm.quantile(exited, .5) if len(exited) >= 10 else None,
                        'exited_only_median': lm.quantile(exited, .5) if exited else None}
    return rows, exit_summary


def _monthly(item):
    cost = lm.monthly_cost(item)
    return {'monthly_cost_min': cost['known_minimum'], 'monthly_cost_status': cost['status']}


def after(payload):
    result = lm.analyse(payload)
    rows = []
    for row in result['listings']:
        comps = row['comparables']
        out = {'price_status': row['price']['status'], 'comp_n': comps.get('n', 0), 'comp_status': comps.get('status'),
               'has_median': 'median' in comps, 'has_range': 'range' in comps, 'flags': row['review_flags']}
        if row.get('monthly_cost'):
            out.update(monthly_cost_min=row['monthly_cost']['known_minimum'], monthly_cost_status=row['monthly_cost']['status'])
        rows.append(out)
    exit_summary = None
    if result.get('exit_time'):
        km = result['exit_time']
        exit_summary = {'reported_median': km['km_median_days'], 'exited_only_median': km['exited_only_median_days']}
    return rows, exit_summary


def judge(expect, rows, exit_summary):
    target = rows[0] if rows else {}
    checks = []
    for key, value in expect.items():
        if key == 'flags_include':
            ok = all(v in target.get('flags', []) for v in value)
        elif key == 'flags_exclude':
            ok = not any(v in target.get('flags', []) for v in value)
        elif key == 'exit_median_not_exited_only':
            ok = exit_summary is not None and exit_summary['reported_median'] != exit_summary['exited_only_median']
        else:
            ok = target.get(key) == value
        checks.append({'check': key, 'expected': value, 'passed': ok})
    return checks


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(centre - half, 3), round(centre + half, 3)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--cases', type=Path, default=ROOT / 'evals/cases_v1.jsonl')
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'evals/rules-v0.3')
    args = parser.parse_args()
    raw = args.cases.read_bytes()
    cases = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    judgments, summary = [], {}
    for condition, runner in (('before_v0.2_rules', before), ('after_v0.3_metrics', after)):
        passed = critical = 0
        for case in cases:
            rows, exit_summary = runner(case['input'])
            checks = judge(case['expect'], rows, exit_summary)
            ok = all(c['passed'] for c in checks)
            passed += ok
            critical += (not ok) and case['type'] in ('hard', 'adversarial')
            judgments.append({'case_id': case['id'], 'type': case['type'], 'condition': condition, 'passed': ok, 'checks': checks,
                              'observed': rows[0] if rows else exit_summary})
        summary[condition] = {'passed': passed, 'total': len(cases), 'rate': round(passed / len(cases), 3),
                              'wilson95': wilson(passed, len(cases)), 'failed_hard_or_adversarial': critical}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / 'judgments.jsonl').write_text(''.join(json.dumps(j, ensure_ascii=False) + '\n' for j in judgments), encoding='utf-8')
    summary = {'suite': 'rule-layer-v1', 'cases_sha256': hashlib.sha256(raw).hexdigest(), 'rules_version': lm.RULES_VERSION,
               'python': sys.version.split()[0], 'scope': 'deterministic rule layer only; not Agent end-to-end listing quality',
               'conditions': summary}
    (args.out_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for name, row in summary['conditions'].items():
        lo, hi = row['wilson95']
        print(f"{name:<20} 通过 {row['passed']}/{row['total']} ({row['rate']:.1%}, Wilson95 {lo:.3f}–{hi:.3f})  硬/对抗失败 {row['failed_hard_or_adversarial']}")
        for j in judgments:
            if j['condition'] == name and not j['passed']:
                print(f"    未通过 [{j['type']}] {j['case_id']}")
    failed = [j['case_id'] for j in judgments if j['condition'] == 'after_v0.3_metrics' and not j['passed']]
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
