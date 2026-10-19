"""Read-only model inventory. Private review output belongs outside Git.

Requires SUBSCRIPTION_MODEL_BACKFILL_DATABASE_URL; never loads dotenv or the
application DSN. No command in this file activates a phase or changes a row.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def explicitFactory():
    url = os.environ.get('SUBSCRIPTION_MODEL_BACKFILL_DATABASE_URL')
    if not url:
        print('SUBSCRIPTION_MODEL_BACKFILL_DATABASE_URL is required; nothing read or written.', file=sys.stderr)
        return None
    import psycopg2
    return lambda: psycopg2.connect(url, application_name='subscription-model-review')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review-output', type=Path, help='Explicit private file for proposed decisions; never stage it')
    parser.add_argument('--trial-reviews', type=Path, help='Reviewed subscription-id -> consumed-proof JSON mapping')
    parser.add_argument('--shadow', action='store_true', help='Compare reviewed V2 facts without mutating access or credits')
    args = parser.parse_args(argv)
    factory = explicitFactory()
    if factory is None:
        return 2
    from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
    try:
        reviews = json.loads(args.trial_reviews.read_text(encoding='utf-8')) if args.trial_reviews else None
        if reviews is not None and not isinstance(reviews, dict):
            raise ValueError('INVALID_TRIAL_REVIEWS')
        repo = SubscriptionModelRepository(factory)
        report = repo.inventory(trialReviews=reviews)
        summary = {k: v for k, v in report.items() if k != 'proposals'}
        if args.shadow:
            comparisons = [repo.shadow(user) for user in sorted({p['userId'] for p in report['proposals']})]
            summary['shadow'] = {'unavailable': sum(not s['available'] for s in comparisons),
                'different': sum(s['available'] and bool(s['differences']) for s in comparisons),
                'matched': sum(s['available'] and not s['differences'] for s in comparisons)}
        if args.review_output:
            # Refuse overwriting a prior reviewed decision or other local file.
            with args.review_output.open('x', encoding='utf-8') as target:
                json.dump(report, target, indent=2)
                target.write('\n')
        print(json.dumps(summary, sort_keys=True))
        return 0
    except Exception:
        print('Subscription model inventory unavailable; no database writes were requested.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
