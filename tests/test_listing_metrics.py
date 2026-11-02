import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('listing_metrics', ROOT / 'scripts/listing_metrics.py')
lm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lm)


def sale(pid, price, area=70, basis='建筑面积', day='2026-09-21', layout='2室'):
    return {'post_id': 'p-' + pid, 'property_id': pid, 'city': '上海', 'transaction_type': 'sale', 'layout': layout,
            'observed_on': day, 'price_claims': [{'source': 'body', 'raw': price}], 'area': {'value': area, 'basis': basis}}


def rent(pid, price, **extra):
    row = {'post_id': 'p-' + pid, 'property_id': pid, 'city': '上海', 'transaction_type': 'rent', 'layout': '1室',
           'observed_on': '2026-09-21', 'price_claims': [{'source': 'body', 'raw': price}], 'area': {'value': 40, 'basis': '建筑'}}
    row.update(extra)
    return row


def by_id(result):
    return {r['property_id']: r for r in result['listings']}


class PriceParsing(unittest.TestCase):
    def test_units_and_precision(self):
        self.assertEqual(lm.parse_price('268万元'), (2680000.0, 'exact'))
        self.assertEqual(lm.parse_price('6,000元/月'), (6000.0, 'exact'))
        self.assertEqual(lm.parse_price('1.2亿'), (120000000.0, 'exact'))
        for raw in ('四千多', '4千多', '8xx万', '5000-6000', '约300万'):
            with self.subTest(raw=raw):
                self.assertEqual(lm.parse_price(raw)[1], 'approximate')
        self.assertEqual(lm.parse_price(None)[1], 'absent')
        self.assertEqual(lm.parse_price('私信询价')[1], 'absent')

    def test_title_body_conflict_blocks_exact_price(self):
        price = lm.price_from_claims([{'source': 'title', 'raw': '225万'}, {'source': 'body', 'raw': '228万元不议价'}])
        self.assertEqual(price['status'], 'conflict')
        self.assertIsNone(price['yuan'])
        self.assertEqual(price['range'], [2250000.0, 2280000.0])

    def test_certificate_area_is_building_basis(self):
        self.assertEqual(lm.area_info({'value': 71, 'basis': '产证面积'})['basis'], 'building')
        self.assertEqual(lm.area_info({'value': 65, 'basis': '自述'})['basis'], 'unknown')


class Comparables(unittest.TestCase):
    def test_fewer_than_three_is_insufficient(self):
        result = by_id(lm.analyse({'listings': [sale('a', '300万'), sale('b', '310万')]}))
        self.assertEqual(result['a']['comparables']['status'], 'insufficient')
        self.assertNotIn('median', result['a']['comparables'])

    def test_three_or_four_gives_range_not_quantiles(self):
        pool = [sale('t', '250万')] + [sale(str(i), f'{300 + i}万') for i in range(3)]
        comps = by_id(lm.analyse({'listings': pool}))['t']['comparables']
        self.assertEqual(comps['status'], 'range_only')
        self.assertNotIn('median', comps)
        self.assertTrue(comps['below_all_comparables'])
        self.assertIn('below_comparable_range_verify_fees_and_currency', by_id(lm.analyse({'listings': pool}))['t']['review_flags'])

    def test_five_gives_type7_quantiles(self):
        pool = [sale('t', '350万')] + [sale(str(i), f'{v}万') for i, v in enumerate([280, 300, 320, 340, 360])]
        comps = by_id(lm.analyse({'listings': pool}))['t']['comparables']
        self.assertEqual(comps['status'], 'quantiles')
        self.assertAlmostEqual(comps['median'], 3200000 / 70, places=1)
        self.assertEqual(comps['price_kind'], 'listing_quote_not_transaction')

    def test_exclusions_basis_area_type_stale(self):
        pool = [sale('t', '300万'), sale('inner', '300万', basis='套内'), sale('big', '300万', area=90),
                rent('r', '6000'), sale('old', '300万', day='2026-05-01'), sale('ok', '300万')]
        comps = by_id(lm.analyse({'listings': pool}))['t']['comparables']
        excluded = {e['property_id']: e['excluded_for'] for e in comps['excluded']}
        self.assertEqual(excluded, {'inner': 'area_basis', 'big': 'area_band', 'r': 'transaction_type', 'old': 'stale_or_undated'})
        self.assertEqual(comps['comparable_ids'], ['ok'])

    def test_one_property_one_quote_latest_wins(self):
        pool = [sale('t', '300万'), sale('x', '310万', day='2026-09-01'), sale('x', '305万', day='2026-09-20')]
        pool[2]['post_id'] = 'p-x2'
        comps = by_id(lm.analyse({'listings': pool}))['t']['comparables']
        self.assertEqual(comps['n'], 1)

    def test_missing_property_ids_do_not_exclude_everything(self):
        pool = [sale('t', '300万')] + [sale(str(i), '310万') for i in range(3)]
        for row in pool:
            row['property_id'] = None
            row['post_id'] = 'p' + str(id(row))
        result = lm.analyse({'listings': pool})
        self.assertEqual(result['listings'][0]['comparables']['n'], 3)


class RentCostAndPayment(unittest.TestCase):
    def test_monthly_service_fee_raises_cost(self):
        row = by_id(lm.analyse({'listings': [rent('a', '3500元/月', fees={'monthly_service': 800}, planned_months=12)]}))['a']
        self.assertEqual(row['monthly_cost'], {'status': 'complete', 'known_minimum': 4300, 'unknown_fees': [], 'deposit_months': None})

    def test_one_time_fee_amortised_and_unknown_fee_marks_at_least(self):
        row = by_id(lm.analyse({'listings': [rent('a', '6000', fees={'one_time_agency': 6000, 'monthly_property': None}, planned_months=12)]}))['a']
        self.assertEqual(row['monthly_cost']['known_minimum'], 6500)
        self.assertEqual(row['monthly_cost']['status'], 'at_least')
        self.assertIn('necessary_fees_unknown', row['review_flags'])

    def test_payment_flags(self):
        row = by_id(lm.analyse({'listings': [rent('a', '5000', payment={'prepay_months': 12, 'deposit_months': 2, 'payee': 'agent',
                                                                         'pay_before_viewing': True, 'rent_loan': True})]}))['a']
        self.assertEqual({f['flag'] for f in row['payment_flags']}, {
            'prepay_over_3_months', 'deposit_over_1_month', 'agent_or_platform_collects_rent_or_deposit',
            'payment_before_viewing', 'rent_loan_or_installment'})

    def test_standard_terms_raise_nothing(self):
        row = by_id(lm.analyse({'listings': [rent('a', '5000', payment={'prepay_months': 3, 'deposit_months': 1, 'payee': 'landlord'})]}))['a']
        self.assertEqual(row['payment_flags'], [])


class Verification(unittest.TestCase):
    def test_agent_without_code_flagged_but_not_fake(self):
        row = by_id(lm.analyse({'listings': [rent('a', '5000', publisher_role='agent')]}))['a']
        self.assertEqual(row['verification_code']['status'], 'not_shown_by_agent_or_institution')
        self.assertIn('不等于假房源', row['verification_code']['note'])

    def test_rent_code_older_than_60_days_possibly_expired(self):
        row = by_id(lm.analyse({'as_of': '2026-11-02', 'listings': [rent('a', '5000', verification_code='shown', published_on='2026-08-01')]}))['a']
        self.assertEqual(row['verification_code']['status'], 'shown_possibly_expired')

    def test_other_city_has_no_regime(self):
        item = rent('a', '5000')
        item['city'] = '成都'
        self.assertEqual(by_id(lm.analyse({'listings': [item]}))['a']['verification_code']['status'], 'no_documented_regime')


class ExitTime(unittest.TestCase):
    def test_censoring_raises_median_versus_exited_only(self):
        durations = [{'days': d, 'exited': True} for d in (5, 8, 10, 12, 15)] + [{'days': d, 'exited': False} for d in (30, 40, 45, 60, 90)]
        km = lm.kaplan_meier(durations)
        self.assertEqual(km['exited_only_median_days'], 10)
        self.assertEqual(km['km_median_days'], 15)
        km = lm.kaplan_meier(durations + [{'days': 120, 'exited': False}])
        self.assertIsNone(km['km_median_days'])
        self.assertEqual(km['km_median_lower_bound_days'], 120)

    def test_km_median_when_curve_crosses_half(self):
        durations = [{'days': d, 'exited': True} for d in (3, 6, 9, 12)] + [{'days': 7, 'exited': False}]
        km = lm.kaplan_meier(durations)
        self.assertEqual(km['km_median_days'], 9)
        self.assertEqual(km['curve'][-1]['survival'], 0.0)


class Settings(unittest.TestCase):
    def test_payload_settings_override_and_unknown_rejected(self):
        pool = [sale('t', '300万')] + [sale(str(i), '310万') for i in range(3)]
        result = lm.analyse({'settings': {'min_comps_for_range': 4}, 'listings': pool})
        self.assertEqual(result['listings'][0]['comparables']['status'], 'insufficient')
        with self.assertRaises(ValueError):
            lm.analyse({'settings': {'min_comps': 1}, 'listings': pool})


class Cli(unittest.TestCase):
    def test_sanlin_replay_and_new_file_only(self):
        source = ROOT / 'evals/sanlin-20260921/v0.3/listings-input.json'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'out.json'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/listing_metrics.py'), str(source), '--output', str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = {r['post_id']: r for r in json.loads(output.read_text())['listings']}
            self.assertIn('price_conflict', rows['6aaa650c0000000025036152']['review_flags'])
            self.assertEqual(rows['6aacf45b000000001203555e']['comparables']['status'], 'insufficient')
            again = subprocess.run([sys.executable, str(ROOT / 'scripts/listing_metrics.py'), str(source), '--output', str(output)],
                                   capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)

    def test_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / 'bad.json'
            bad.write_text('[]')
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/listing_metrics.py'), str(bad)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
