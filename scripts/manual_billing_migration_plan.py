"""Print a reviewed staged migration allowlist; never connect or apply SQL."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.subscription_storage_guard import requireLegacySubscriptionStorage

ROOT=Path(__file__).resolve().parents[1]
EXPAND=(
    '20260917170828_create_notification_deliveries.sql',
    '20260918100000_harden_notification_claims.sql',
    '20261005195608_expand_manual_monthly_billing.sql',
    '20261005195617_add_manual_billing_transactions.sql',
    '20261005195635_extend_monthly_notifications.sql',
    '20261006131717_enforce_manual_checkout_order_identity.sql',
    '20261006173531_fence_billing_notification_revisions.sql',
    '20261007100919_index_manual_billing_recovery_obligations.sql',
    '20261007134945_admin_credit_reset_operations.sql',
    '20261007154210_index_unresolved_cycle_money.sql',
    '20261007194402_allow_inactive_credit_domains.sql',
    '20261007194411_restrict_financial_data_access.sql',
)
CONTRACT='20261005195626_contract_recurring_billing_fields.sql'
RETIREMENT_EVIDENCE=(
    'Old workers and clients are drained and cannot enqueue recurring charges.',
    'Live provider mandates are retired through the supported provider process.',
    'Reviewed canonical/paid-period/credit mappings are applied without losing usage or top-ups.',
    'Pending legacy orders, captures and refund obligations are mapped and reconciled.',
    'The database SQL guards pass; rollback retains manual coverage without recurring debit.',
)


def buildMigrationPlan(appliedVersions:set[str],phase:str,subscriptionColumns=())->list[Path]:
    requireLegacySubscriptionStorage(subscriptionColumns)
    if phase not in ('expand','contract'): raise ValueError('INVALID_MIGRATION_PHASE')
    if phase=='contract':
        missing=[name.split('_')[0] for name in EXPAND if name.split('_')[0] not in appliedVersions]
        if missing: raise ValueError('EXPANSION_NOT_APPLIED:'+','.join(missing))
        names=(CONTRACT,)
    else: names=EXPAND
    # The contraction lives outside supabase/migrations so a blanket push cannot apply it mid-chain.
    folder='supabase/manual_billing_contract' if phase=='contract' else 'supabase/migrations'
    paths=[ROOT/folder/name for name in names if name.split('_')[0] not in appliedVersions]
    if not all(path.is_file() for path in paths): raise ValueError('MIGRATION_FILE_MISSING')
    return paths


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=('expand','contract'),required=True)
    parser.add_argument('--applied-versions',required=True,help='Local JSON list of actual applied version strings')
    parser.add_argument('--subscription-columns',required=True,help='Private JSON list from current schema inventory; this offline tool cannot inspect the database')
    args=parser.parse_args()
    applied=json.loads(Path(args.applied_versions).read_text(encoding='utf-8'))
    if not isinstance(applied,list) or not all(isinstance(version,str) and version.isdigit() for version in applied):
        parser.error('Applied versions must be a JSON list of numeric version strings.')
    columns=json.loads(Path(args.subscription_columns).read_text(encoding='utf-8'))
    if not isinstance(columns,list) or not columns or not all(isinstance(column,str) for column in columns):
        parser.error('Subscription columns must be a nonempty JSON list of column names.')
    try: paths=buildMigrationPlan(set(applied),args.phase,columns)
    except ValueError as error:
        print(json.dumps({'phase':args.phase,'allowlist':[],'blocker':str(error)}))
        return 1
    print(json.dumps({'phase':args.phase,'allowlist':[path.name for path in paths],
        'requiresOperatorRetirementEvidence':list(RETIREMENT_EVIDENCE),
        'certifiesRetirement':False,'appliesSql':False},indent=2))
    return 0


if __name__=='__main__': raise SystemExit(main())
