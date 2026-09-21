import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('triage', ROOT / 'scripts/triage.py')
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)


def response():
    return {'model': 'test-model', 'usage': {'input_tokens': 100}, 'answers': {
        k: {'type': 'choice', 'choice': v, 'confidence': 0.95} for k, v in
        [('intent', 'rent_supply'), ('price_disclosure', 'exact'), ('inventory', 'single')]}}


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.records = [{'post_id': str(i), 'text': '虚构房源'} for i in range(3)]
        self.env = patch.dict(os.environ, {'TYPESAFE_API_KEY': 'unit-test-only'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_disabled_never_calls_provider(self):
        def fail(*args):
            self.fail('network call while disabled')
        rows = triage.run(self.records, request=fail)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r['review_reasons'] == ['jev_disabled'] for r in rows))

    def test_missing_key_keeps_every_record(self):
        with patch.dict(os.environ, {}, clear=True):
            rows = triage.run(self.records, enabled=True)
        self.assertTrue(all(r['retained'] and r['review_reasons'] == ['missing_api_key'] for r in rows))

    def test_high_confidence_is_not_authenticity(self):
        rows = triage.run(self.records, enabled=True, request=lambda *a: response())
        self.assertTrue(all(r['requires_evidence_review'] and r['authenticity'] == 'not_evaluated' for r in rows))
        self.assertTrue(all(not r['triage_review_required'] for r in rows))
        self.assertNotIn('unit-test-only', json.dumps(rows))

    def test_request_cap_preserves_tail(self):
        calls = []
        def request(*args):
            calls.append(1)
            return response()
        rows = triage.run(self.records, enabled=True, max_requests=1, request=request)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]['review_reasons'], ['request_limit_reached'])

    def test_http_failure_stops_without_retry(self):
        calls = []
        def request(*args):
            calls.append(1)
            raise urllib.error.HTTPError('endpoint', 401, 'secret response', None, None)
        rows = triage.run(self.records, enabled=True, request=request)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rows[0]['review_reasons'], ['provider_http_401'])
        self.assertEqual(rows[-1]['review_reasons'], ['provider_stopped_after_error'])
        self.assertNotIn('secret response', json.dumps(rows))

    def test_timeout_preserves_tail(self):
        def request(*args):
            raise TimeoutError()
        rows = triage.run(self.records, enabled=True, request=request)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['review_reasons'], ['provider_request_failed'])

    def test_bad_provider_shapes_require_review(self):
        questions = json.loads((ROOT / 'assets/jev-questions.json').read_text())
        for raw in (None, [], {}, {'answers': []}, {'answers': {'intent': 'bad'}}):
            with self.subTest(raw=raw):
                self.assertTrue(triage.interpret(raw, questions, .8)[1])

    def test_invalid_and_low_confidence_require_review(self):
        questions = json.loads((ROOT / 'assets/jev-questions.json').read_text())
        for value in (None, True, float('nan'), -1, 2, '0.99', .36):
            with self.subTest(value=value):
                raw = response()
                raw['answers']['price_disclosure']['confidence'] = value
                self.assertTrue(triage.interpret(raw, questions, .8)[1])

    def test_history_and_generic_retained_for_review(self):
        raw = response()
        raw['answers']['price_disclosure']['choice'] = 'historical_only'
        raw['answers']['inventory']['choice'] = 'multiple_or_generic'
        rows = triage.run(self.records, enabled=True, request=lambda *a: raw)
        self.assertTrue(all(r['retained'] and r['triage_review_required'] for r in rows))

    def test_fabricated_label_not_accepted(self):
        raw = response()
        raw['answers']['intent']['choice'] = 'verified_owner'
        rows = triage.run(self.records, enabled=True, request=lambda *a: raw)
        self.assertNotIn('intent', rows[0]['labels'])
        self.assertIn('intent:invalid_answer', rows[0]['review_reasons'])


if __name__ == '__main__':
    unittest.main()
