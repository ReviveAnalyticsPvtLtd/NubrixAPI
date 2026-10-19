"""Dry-run by default; --apply requires an explicitly reviewed proposal file.

Production application needs separate operator authorization. This tool never
loads .env, selects the application DSN, activates V2, or applies SQL migrations.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.subscription_model_inventory import explicitFactory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reviewed-file', type=Path)
    parser.add_argument('--apply', action='store_true', help='Apply only exact reviewed decisions; never enables cutover')
    args = parser.parse_args(argv)
    factory = explicitFactory()
    if factory is None:
        return 2
    if args.apply and args.reviewed_file is None:
        parser.error('--apply requires --reviewed-file')
    from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
    try:
        repo = SubscriptionModelRepository(factory)
        if args.reviewed_file is None:
            report = repo.inventory()
            print(json.dumps({'dryRun': True, 'counts': report['counts'], 'authorizesCutover': False}))
            return 0
        manifest = json.loads(args.reviewed_file.read_text(encoding='utf-8'))
        if manifest.get('schemaVersion') != 2 or not isinstance(manifest.get('proposals'), list):
            raise ValueError('INVALID_REVIEW_MANIFEST')
        decisions = manifest['proposals']
        if len({d['subscriptionId'] for d in decisions}) != len(decisions):
            raise ValueError('DUPLICATE_REVIEW_DECISION')
        if args.apply:
            results = [repo.applyReviewedDecision(decision) for decision in decisions]
            counts = {status: sum(r['status'] == status for r in results) for status in ('applied', 'already_applied', 'needs_review')}
            print(json.dumps({'dryRun': False, 'counts': counts, 'authorizesCutover': False}))
            return 1 if counts['needs_review'] else 0
        statuses = [repo.checkReviewedDecision(decision) for decision in decisions]
        stale = statuses.count('stale')
        print(json.dumps({'dryRun': True, 'reviewedDecisions': len(decisions), 'staleDecisions': stale,
            'alreadyAppliedDecisions': statuses.count('already_applied'), 'authorizesCutover': False}))
        return 1 if stale else 0
    except Exception:
        # Connection errors can contain credentials. Keep failure output fixed.
        print('Subscription model backfill failed; uncommitted row work rolled back. Earlier rows may already be committed; replay the same reviewed file safely after checking the private review and database prerequisites.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
