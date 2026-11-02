#!/usr/bin/env python3
"""Deterministic listing metrics for housing-quality-radar. Python 3.10+, standard library only.

Computes what SKILL.md says must be computed by code, not guessed by a model:
price normalisation and conflicts, rental necessary monthly cost, unit price on a
consistent area basis, listing-price comparables, payment-term risk flags, listing
verification-code status and censoring-aware exit-time summaries.

Every flag is an evidence label for review, never an authenticity verdict.
Rule sources are listed in references/benchmarks.md (RULES_VERSION below).
"""
import argparse
from datetime import date
import json
import math
from pathlib import Path
import re
import sys

RULES_VERSION = 'metrics-2026-11-v1'

# Area bases that can be compared with each other. A Chinese property certificate
# (产证/不动产权证) records building area, so it maps to "building" while keeping raw text.
AREA_BASIS = {
    '建筑': 'building', '建筑面积': 'building', '产证': 'building', '产证面积': 'building', 'building': 'building',
    '套内': 'inner', '套内面积': 'inner', 'inner': 'inner',
    '使用': 'usable', '使用面积': 'usable', 'usable': 'usable',
}
# Cities whose listing-verification regime is documented in references/benchmarks.md.
VERIFICATION_REGIMES = {
    '上海': {'rent_code_valid_days': 60, 'lookup': 'https://zfzl.fgj.sh.gov.cn/'},
}
DEFAULTS = {
    'area_tolerance': 0.15,          # ±15% area band (existing heuristic, kept)
    'max_comp_age_days': 90,         # widest window the Skill allows for listing comparables
    'min_comps_for_range': 3,        # GB/T 50291-2015 comparison approach: at least three comparables
    'min_comps_for_quantiles': 5,    # quartiles from fewer points are not meaningful
    'max_prepay_months': 3,          # 2021 six-ministry opinion on light-asset rental firms
    'max_deposit_months': 1,
}
_NUM = r'(\d+(?:\.\d+)?)'


def parse_price(raw):
    """Return (yuan or None, precision) from text such as '268万', '6000元/月', '4千多'."""
    if raw is None:
        return None, 'absent'
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return (float(raw), 'exact') if math.isfinite(raw) and raw > 0 else (None, 'invalid')
    text = str(raw).replace(',', '').replace('，', '').replace(' ', '')
    if not text:
        return None, 'absent'
    if re.search(r'[多余左右约起]|x|X|\d+\s*[-~～至到]\s*\d+', text):
        return None, 'approximate'
    match = re.search(_NUM + r'\s*(亿|万|千|k|K|元)?', text)
    if not match:
        return None, 'absent'
    value = float(match.group(1))
    unit = match.group(2) or ''
    value *= {'亿': 1e8, '万': 1e4, '千': 1e3, 'k': 1e3, 'K': 1e3}.get(unit, 1)
    return (value, 'exact') if value > 0 else (None, 'invalid')


def price_from_claims(claims):
    """Reconcile price claims from title/body/comments. Conflicts block exact ranking."""
    parsed = []
    for claim in claims or []:
        value, precision = parse_price(claim.get('raw'))
        parsed.append({'source': claim.get('source'), 'raw': claim.get('raw'), 'yuan': value, 'precision': precision})
    exact = sorted({p['yuan'] for p in parsed if p['precision'] == 'exact'})
    if not exact:
        status = 'approximate' if any(p['precision'] == 'approximate' for p in parsed) else 'absent'
        return {'status': status, 'yuan': None, 'range': None, 'claims': parsed}
    if len(exact) > 1:
        return {'status': 'conflict', 'yuan': None, 'range': [exact[0], exact[-1]], 'claims': parsed}
    return {'status': 'exact', 'yuan': exact[0], 'range': None, 'claims': parsed}


def area_info(area):
    if not isinstance(area, dict) or not isinstance(area.get('value'), (int, float)) or area['value'] <= 0:
        return {'sqm': None, 'basis': 'unknown', 'raw_basis': (area or {}).get('basis') if isinstance(area, dict) else None}
    raw_basis = area.get('basis')
    return {'sqm': float(area['value']), 'basis': AREA_BASIS.get(raw_basis or '', 'unknown'), 'raw_basis': raw_basis}


def monthly_cost(listing):
    """月均必要成本 = 月租 + 固定月费 + 不可退一次性费用 / 计划居住月数。押金单列。"""
    rent = listing['price']
    fees = listing.get('fees') or {}
    months = listing.get('planned_months')
    unknown = [k for k, v in fees.items() if v is None]
    if rent['status'] != 'exact':
        return {'status': 'rent_' + rent['status'], 'known_minimum': None, 'unknown_fees': unknown}
    monthly = sum(v for k, v in fees.items() if k.startswith('monthly_') and isinstance(v, (int, float)))
    one_time = sum(v for k, v in fees.items() if k.startswith('one_time_') and isinstance(v, (int, float)))
    if one_time and not (isinstance(months, (int, float)) and months > 0):
        unknown.append('planned_months')
    amortised = one_time / months if one_time and isinstance(months, (int, float)) and months > 0 else 0
    total = round(rent['yuan'] + monthly + amortised, 2)
    return {'status': 'complete' if not unknown else 'at_least', 'known_minimum': total, 'unknown_fees': unknown,
            'deposit_months': listing.get('payment', {}).get('deposit_months')}


def quantile(sorted_values, q):
    """Hyndman–Fan type 7 (linear interpolation), the default in R and numpy."""
    position = (len(sorted_values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def _days_between(a, b):
    if not a or not b:
        return None
    return abs((date.fromisoformat(str(a)[:10]) - date.fromisoformat(str(b)[:10])).days)


def comparable_metric(listing):
    """Unit price for sale (same area basis only); rent compares necessary monthly cost, else monthly rent."""
    if listing['price']['status'] != 'exact':
        return None
    if listing.get('transaction_type') == 'sale':
        area = listing['area']
        if area['sqm'] is None or area['basis'] == 'unknown':
            return None
        return round(listing['price']['yuan'] / area['sqm'], 2)
    return listing['price']['yuan']


def comparables(target, pool, settings):
    """Select listing-price comparables and summarise them. Listing prices are not transaction prices."""
    reasons, chosen = [], {}
    metric = comparable_metric(target)
    if metric is None:
        return {'status': 'target_not_comparable', 'n': 0, 'reason': 'target price or area basis not established'}
    for other in pool:
        if other is target or (target.get('property_id') and other.get('property_id') == target.get('property_id')):
            continue
        why = None
        if other.get('transaction_type') != target.get('transaction_type'):
            why = 'transaction_type'
        elif target.get('layout') and other.get('layout') and other['layout'] != target['layout']:
            why = 'layout'
        elif target['area']['sqm'] and other['area']['sqm'] and abs(other['area']['sqm'] / target['area']['sqm'] - 1) > settings['area_tolerance']:
            why = 'area_band'
        elif target.get('transaction_type') == 'sale' and other['area']['basis'] != target['area']['basis']:
            why = 'area_basis'
        elif comparable_metric(other) is None:
            why = 'price_or_area_unknown'
        else:
            age = _days_between(target.get('observed_on'), other.get('observed_on'))
            if age is None or age > settings['max_comp_age_days']:
                why = 'stale_or_undated'
        if why:
            reasons.append({'property_id': other.get('property_id'), 'excluded_for': why})
            continue
        key = other.get('property_id') or other.get('post_id')
        previous = chosen.get(key)  # one property contributes one current quote: keep the latest observation
        if previous is None or str(other.get('observed_on')) > str(previous.get('observed_on')):
            chosen[key] = other
    values = sorted(comparable_metric(c) for c in chosen.values())
    n = len(values)
    result = {'metric': 'unit_price_yuan_per_sqm' if target.get('transaction_type') == 'sale' else 'monthly_rent_yuan',
              'target': metric, 'n': n, 'comparable_ids': sorted(chosen), 'excluded': reasons,
              'price_kind': 'listing_quote_not_transaction'}
    if n < settings['min_comps_for_range']:
        result.update(status='insufficient', note='可比样本不足：仅逐套对照，不给区间或折价结论')
        return result
    below = sum(v < metric for v in values)
    equal = sum(v == metric for v in values)
    result['percentile_rank'] = round((below + 0.5 * equal) / n * 100, 1)
    result['range'] = [values[0], values[-1]]
    result['below_all_comparables'] = metric < values[0]
    if n < settings['min_comps_for_quantiles']:
        result.update(status='range_only', note='达到 3 个可比实例下限，只给区间和位次，不给分位数和折价率')
        return result
    median = quantile(values, .5)
    p25 = quantile(values, .25)
    result.update(status='quantiles', median=round(median, 2), p25=round(p25, 2), p75=round(quantile(values, .75), 2),
                  vs_median_pct=round((metric / median - 1) * 100, 1), below_p25=metric < p25,
                  note='挂牌报价分位；不是成交价，也不证明低于市场')
    return result


def payment_flags(listing, settings):
    """Payment-term evidence labels. Each flag needs review; none proves fraud."""
    terms = listing.get('payment') or {}
    flags = []
    if listing.get('transaction_type') == 'rent':
        prepay = terms.get('prepay_months')
        if isinstance(prepay, (int, float)) and prepay > settings['max_prepay_months']:
            flags.append({'flag': 'prepay_over_3_months', 'basis': 'rental_firm_prepay_cap_2021'})
        deposit = terms.get('deposit_months')
        if isinstance(deposit, (int, float)) and deposit > settings['max_deposit_months']:
            flags.append({'flag': 'deposit_over_1_month', 'basis': 'rental_firm_deposit_cap_2021'})
        if terms.get('rent_loan'):
            flags.append({'flag': 'rent_loan_or_installment', 'basis': 'rental_firm_rent_loan_ban_2021'})
    if terms.get('payee') in ('agent', 'platform'):
        flags.append({'flag': 'agent_or_platform_collects_rent_or_deposit', 'basis': 'housing_rental_regulation_2025_art25_36'})
    if terms.get('pay_before_viewing'):
        flags.append({'flag': 'payment_before_viewing', 'basis': 'ftc_rental_listing_scams'})
    if terms.get('untraceable_payment'):
        flags.append({'flag': 'untraceable_payment', 'basis': 'ftc_rental_listing_scams'})
    return flags


def verification_status(listing, as_of):
    """Listing verification code (房源核验码) status where a documented regime exists."""
    regime = VERIFICATION_REGIMES.get(listing.get('city') or '')
    if not regime:
        return {'status': 'no_documented_regime'}
    code = listing.get('verification_code')
    if not code:
        status = 'not_shown'
        if listing.get('publisher_role') in ('agent', 'institution'):
            status = 'not_shown_by_agent_or_institution'
        return {'status': status, 'lookup': regime['lookup'], 'note': '未展示核验码不等于假房源；请向发布方索取并在官方平台查验'}
    result = {'status': 'shown_unverified', 'lookup': regime['lookup']}
    age = _days_between(as_of, listing.get('published_on'))
    if listing.get('transaction_type') == 'rent' and age is not None and age > regime['rent_code_valid_days']:
        result['status'] = 'shown_possibly_expired'
    return result


def kaplan_meier(durations):
    """Censoring-aware exit time. durations: [{'days': int, 'exited': bool}].

    Listings still active, deleted, or invisible are right-censored at their last
    observed day; dropping them biases the median downward.
    """
    rows = [(float(d['days']), bool(d['exited'])) for d in durations if isinstance(d.get('days'), (int, float)) and d['days'] >= 0]
    events = sum(e for _, e in rows)
    naive = sorted(t for t, e in rows if e)
    naive_median = quantile(naive, .5) if naive else None
    survival, at_risk, curve, median = 1.0, len(rows), [], None
    for t in sorted({t for t, e in rows if e}):
        exits = sum(1 for d, e in rows if d == t and e)
        at_risk = sum(1 for d, _ in rows if d >= t)
        survival *= 1 - exits / at_risk
        curve.append({'day': t, 'at_risk': at_risk, 'exits': exits, 'survival': round(survival, 4)})
        if median is None and survival <= .5:
            median = t
    longest = max((t for t, _ in rows), default=None)
    return {'n': len(rows), 'exits': events, 'censored': len(rows) - events, 'curve': curve,
            'km_median_days': median,
            'km_median_lower_bound_days': None if median is not None else longest,
            'exited_only_median_days': naive_median,
            'note': '公开标记退出≠成交；中位数未达到时只报告下界'}


def normalise(listing):
    item = dict(listing)
    item['price'] = price_from_claims(listing.get('price_claims'))
    item['area'] = area_info(listing.get('area'))
    return item


def analyse(payload, settings=None):
    unknown = set(payload.get('settings') or {}) - set(DEFAULTS)
    if unknown:
        raise ValueError('unknown settings: ' + ', '.join(sorted(unknown)))
    settings = {**DEFAULTS, **(payload.get('settings') or {}), **(settings or {})}
    as_of = payload.get('as_of')
    listings = [normalise(x) for x in payload.get('listings', [])]
    out = []
    for item in listings:
        row = {'post_id': item.get('post_id'), 'property_id': item.get('property_id'), 'transaction_type': item.get('transaction_type'),
               'price': {k: item['price'][k] for k in ('status', 'yuan', 'range')}, 'area': item['area'],
               'unit_price_yuan_per_sqm': comparable_metric(item) if item.get('transaction_type') == 'sale' else None,
               'comparables': comparables(item, listings, settings),
               'payment_flags': payment_flags(item, settings),
               'verification_code': verification_status(item, as_of)}
        if item.get('transaction_type') == 'rent':
            row['monthly_cost'] = monthly_cost(item)
        row['review_flags'] = review_flags(row)
        out.append(row)
    result = {'rules_version': RULES_VERSION, 'as_of': as_of, 'settings': settings, 'listings': out}
    if payload.get('exit_durations'):
        result['exit_time'] = kaplan_meier(payload['exit_durations'])
    return result


def review_flags(row):
    flags = []
    if row['price']['status'] == 'conflict':
        flags.append('price_conflict')
    elif row['price']['status'] in ('absent', 'approximate'):
        flags.append('price_' + row['price']['status'])
    if row['transaction_type'] == 'sale' and row['area']['basis'] == 'unknown':
        flags.append('area_basis_unknown')
    comps = row['comparables']
    if comps.get('below_p25') or (comps.get('status') == 'range_only' and comps.get('below_all_comparables')):
        flags.append('below_comparable_range_verify_fees_and_currency')
    flags.extend(f['flag'] for f in row['payment_flags'])
    if row['verification_code']['status'] in ('not_shown_by_agent_or_institution', 'shown_possibly_expired'):
        flags.append('verification_code_' + row['verification_code']['status'])
    cost = row.get('monthly_cost')
    if cost and cost['status'] == 'at_least':
        flags.append('necessary_fees_unknown')
    return flags


COMP_LABEL = {'insufficient': '可比不足', 'range_only': '仅区间', 'quantiles': '分位可用', 'target_not_comparable': '无法比较'}
PRICE_LABEL = {'exact': '报价明确', 'conflict': '报价冲突', 'approximate': '报价模糊', 'absent': '未报价'}


def summary_lines(result):
    lines = [f"规则 {result['rules_version']} · 观察日 {result.get('as_of') or '未知'} · {len(result['listings'])} 条"]
    for row in result['listings']:
        price = row['price']
        if price['status'] == 'exact':
            shown = f"{price['yuan'] / 1e4:g}万" if row['transaction_type'] == 'sale' else f"{price['yuan']:g}元/月"
        elif price['status'] == 'conflict':
            shown = f"{price['range'][0] / 1e4:g}–{price['range'][1] / 1e4:g}万"
        else:
            shown = '—'
        unit = row.get('unit_price_yuan_per_sqm')
        comps = row['comparables']
        parts = [str(row['post_id'])[:8], '出售' if row['transaction_type'] == 'sale' else '出租',
                 PRICE_LABEL.get(price['status'], price['status']) + ' ' + shown,
                 f"单价{unit:,.0f}元/㎡" if unit else '单价—',
                 f"{COMP_LABEL.get(comps['status'], comps['status'])}(n={comps.get('n', 0)})",
                 '核验码:' + row['verification_code']['status']]
        lines.append(' | '.join(parts))
        lines.append('    待核验: ' + (', '.join(row['review_flags']) or '无规则旗标（仍需人工核验）'))
    if result.get('exit_time'):
        km = result['exit_time']
        lines.append(f"退出时长: n={km['n']} 退出={km['exits']} 删失={km['censored']} KM中位={km['km_median_days']} 下界={km['km_median_lower_bound_days']}")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('input', type=Path, help='UTF-8 JSON with as_of, listings[] and optional exit_durations[]')
    parser.add_argument('--output', type=Path, help='new JSON file; prints to stdout when omitted')
    parser.add_argument('--summary', action='store_true', help='also print a one-line-per-listing Chinese summary to stderr')
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('listings'), list):
            raise ValueError
        result = analyse(payload)
    except (OSError, ValueError, TypeError, KeyError):
        parser.error('invalid input; require JSON object with a listings array (see references/data-model.md)')
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.summary:
        print('\n'.join(summary_lines(result)), file=sys.stderr)
    if args.output:
        if args.output.exists():
            parser.error('output must be a new file')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(text)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
