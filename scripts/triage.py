#!/usr/bin/env python3
"""Optional Jev shadow triage. Python 3.10+, standard library only."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = 'https://api.typesafe.ai/v1/systemone'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def classify(text, key, model, questions):
    payload = {'state': text, 'model': model, 'questions': questions}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
        return json.load(response)


def interpret(response, questions, threshold):
    if not isinstance(response, dict):
        return {}, ['invalid_response']
    answers = response.get('answers')
    if not isinstance(answers, dict):
        return {}, ['missing_answers']
    labels, reasons = {}, []
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get('type') != 'choice' or answer.get('choice') not in question['criteria']:
            reasons.append(name + ':invalid_answer')
            continue
        confidence = answer.get('confidence')
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            reasons.append(name + ':invalid_confidence')
            continue
        labels[name] = {'choice': answer['choice'], 'confidence': confidence}
        if confidence < threshold:
            reasons.append(name + ':low_confidence')
    if labels.get('intent', {}).get('choice') == 'other':
        reasons.append('intent_unclear')
    if labels.get('price_disclosure', {}).get('choice') != 'exact':
        reasons.append('current_exact_price_not_established')
    if labels.get('inventory', {}).get('choice') != 'single':
        reasons.append('single_property_not_established')
    return labels, reasons


def run(records, *, enabled=False, model='jev-latest', threshold=0.8, max_requests=20, request=classify):
    questions = json.loads((ROOT / 'assets/jev-questions.json').read_text())
    key = os.environ.get('TYPESAFE_API_KEY') if enabled else None
    rows, stopped, attempted = [], False, 0
    for record in records:
        text = record['text']
        row = {'post_id': record['post_id'], 'mode': 'jev' if enabled else 'agent', 'triage_policy': 'shadow', 'retained': True,
               'requires_evidence_review': True, 'authenticity': 'not_evaluated',
               'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
               'questions_sha256': hashlib.sha256(json.dumps(questions, sort_keys=True).encode()).hexdigest(),
               'model_requested': model, 'model_resolved': None, 'labels': {}}
        if not enabled:
            row['review_reasons'] = ['jev_disabled']
        elif not key:
            row['review_reasons'] = ['missing_api_key']
        elif stopped:
            row['review_reasons'] = ['provider_stopped_after_error']
        elif attempted >= max_requests:
            row['review_reasons'] = ['request_limit_reached']
        else:
            attempted += 1
            start = time.perf_counter()
            try:
                response = request(text, key, model, questions)
                row['labels'], row['review_reasons'] = interpret(response, questions, threshold)
                if isinstance(response, dict):
                    resolved = response.get('model')
                    row['model_resolved'] = resolved if isinstance(resolved, str) else None
                    usage = response.get('usage', {})
                    if isinstance(usage, dict):
                        row['usage'] = {k: v for k, v in usage.items() if k in ('input_tokens', 'output_tokens') and type(v) is int and v >= 0}
                if row['model_resolved'] is None:
                    row['review_reasons'].append('resolved_model_missing')
            except urllib.error.HTTPError as exc:
                row['review_reasons'] = ['provider_http_' + str(exc.code)]
                stopped = True
            except (OSError, ValueError):
                row['review_reasons'] = ['provider_request_failed']
                stopped = True
            row['elapsed_seconds'] = round(time.perf_counter() - start, 4)
        row['triage_review_required'] = bool(row['review_reasons'])
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='UTF-8 JSONL: post_id, redacted text')
    parser.add_argument('--output', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--mode', choices=('agent', 'jev'), default='agent', help='agent: prepare queue without Jev; jev: classify via TypeSafe')
    modes.add_argument('--enable-jev', dest='mode', action='store_const', const='jev', help='Compatibility alias for --mode jev')
    parser.add_argument('--model', default='jev-latest')
    parser.add_argument('--threshold', type=float, default=0.8)
    parser.add_argument('--max-requests', type=int, default=20)
    args = parser.parse_args()
    if not math.isfinite(args.threshold) or not 0 <= args.threshold <= 1 or args.max_requests < 0:
        parser.error('threshold must be 0..1 and max-requests >= 0')
    if args.input.resolve() == args.output.resolve() or args.output.exists():
        parser.error('output must be a new file distinct from input')
    try:
        records = [json.loads(line) for line in args.input.read_text(encoding='utf-8').splitlines() if line.strip()]
        ids = set()
        for r in records:
            if not isinstance(r, dict) or not isinstance(r.get('post_id'), str) or not r['post_id'].strip() or not isinstance(r.get('text'), str) or not r['text'].strip():
                raise ValueError('each row needs nonempty string post_id and text')
            if r['post_id'] in ids:
                raise ValueError('duplicate post_id: consolidate evidence first')
            ids.add(r['post_id'])
    except (OSError, ValueError):
        parser.error('invalid input; require readable JSONL with unique post_id and nonempty text')
    rows = run(records, enabled=args.mode == 'jev', model=args.model, threshold=args.threshold, max_requests=args.max_requests)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create protects existing evidence; separate runs must use distinct paths.
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    print(json.dumps({'records': len(rows), 'retained': len(rows), 'mode': args.mode, 'triage_policy': 'shadow',
                      'triage_review_required': sum(r['triage_review_required'] for r in rows)}))


if __name__ == '__main__':
    main()
