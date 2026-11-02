import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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

    def test_pii_text_is_never_sent(self):
        def fail(*args):
            self.fail('PII text sent to provider')
        records = [{'post_id': 'a', 'text': '房东直租，电话13812345678'}, {'post_id': 'b', 'text': '加微信: abc_12345 看房'}]
        rows = triage.run(records, enabled=True, request=fail)
        self.assertEqual(rows[0]['review_reasons'], ['pii_suspected_not_sent:mobile'])
        self.assertEqual(rows[1]['review_reasons'], ['pii_suspected_not_sent:wechat_id'])
        self.assertNotIn('13812345678', json.dumps(rows, ensure_ascii=False))

    def test_contradictory_labels_require_review(self):
        raw = response()
        raw['answers']['intent']['choice'] = 'demand'
        labels, reasons = triage.interpret(raw, json.loads((ROOT / 'assets/jev-questions.json').read_text()), .8)
        self.assertIn('label_conflict', reasons)
        raw = response()
        raw['answers']['inventory']['choice'] = 'not_applicable'
        self.assertIn('label_conflict', triage.interpret(raw, json.loads((ROOT / 'assets/jev-questions.json').read_text()), .8)[1])
        self.assertNotIn('label_conflict', triage.interpret(response(), json.loads((ROOT / 'assets/jev-questions.json').read_text()), .8)[1])

    def test_cli_modes_and_legacy_alias_without_credentials(self):
        env = os.environ.copy()
        env.pop('TYPESAFE_API_KEY', None)
        with tempfile.TemporaryDirectory() as directory:
            for i, (args, mode, reason) in enumerate([
                ([], 'agent', 'jev_disabled'),
                (['--mode', 'agent'], 'agent', 'jev_disabled'),
                (['--mode', 'jev'], 'jev', 'missing_api_key'),
                (['--enable-jev'], 'jev', 'missing_api_key'),
            ]):
                output = Path(directory) / f'{i}.jsonl'
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/triage.py'),
                    str(ROOT / 'evals/sanlin-20260921/triage-input.jsonl'),
                    '--output', str(output), *args], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['mode'], mode)
                rows = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual(len(rows), 4)
                self.assertTrue(all(r['mode'] == mode and r['review_reasons'] == [reason] for r in rows))

    def test_cli_rejects_conflicting_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output.jsonl'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/triage.py'),
                str(ROOT / 'evals/sanlin-20260921/triage-input.jsonl'),
                '--output', str(output), '--mode', 'agent', '--enable-jev'],
                capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
