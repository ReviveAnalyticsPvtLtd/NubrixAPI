"""Contract tests for the manual monthly billing expansion/transaction migrations.

These tests assert the *content* of the generated migration SQL. They run
without a live database; actual mutation coverage lives in the opt-in
integration suite (test_manual_billing_transactions_integration.py).
"""

from pathlib import Path


def _migrationText(pattern: str) -> str:
    matches = list(Path("supabase/migrations").glob(pattern))
    assert len(matches) == 1, f"expected exactly one migration matching {pattern}"
    return matches[0].read_text(encoding="utf-8")


def _normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def _expansionSql() -> str:
    return _migrationText("*_expand_manual_monthly_billing.sql")


def _transactionsSql() -> str:
    return _migrationText("*_add_manual_billing_transactions.sql")


# --- expansion migration ---------------------------------------------------


def test_expansionAddsMonthlyPrepaidBillingMode():
    sql = _expansionSql()
    assert "monthly_prepaid" in sql
    assert "billing_mode" in sql
    # Legacy monthly_recurring remains allowed during rollout.
    assert "monthly_recurring" in sql


def test_expansionAddsRenewalOptOutColumn():
    sql = _normalized(_expansionSql())
    assert "renewal_opt_out" in sql
    assert "boolean not null default false" in sql


def test_expansionAddsCanonicalFlagWithoutImmediatelyEnforcingUniqueness():
    sql = _expansionSql()
    normalized = _normalized(sql)
    assert "is_canonical" in sql
    assert "boolean not null default false" in normalized
    # The partial unique index on (user_id) WHERE is_canonical is created in
    # the *transactions* migration after operator-reviewed backfill; the
    # expansion must not enforce it yet.
    lowered = normalized
    assert "create unique index idx_subscriptions_one_canonical" not in lowered


def test_expansionAddsCreditIdentityColumns():
    sql = _expansionSql()
    assert "lifecycle_id" in sql
    assert "credit_period_id" in sql
    assert "balance_version" in sql
    assert "check (balance_version >= 0)" in sql.lower()


def test_expansionPreservesLegacyPaymentAttemptTypes():
    sql = _expansionSql()
    # token_debit history stays readable
    assert "token_debit" in sql
    assert "authenticated_checkout" in sql


def test_expansionNormalizesInvoiceStatusStorage():
    sql = _expansionSql()
    normalized = _normalized(sql)
    assert "set status = upper(status)" in normalized
    assert "paying_pending_placeholder_not_expected" not in normalized
    assert "'payment_pending'" in normalized or "'PAYMENT_PENDING'" in sql
    assert "set status = 'expired' where status in ('failed')" not in normalized


def test_expansionIsAdditiveOnlyForContractedColumns():
    sql = _expansionSql()
    lowered = sql.lower()
    for dropped in (
        "drop column subscriptions.razorpay_customer_id",
        "drop column subscriptions.razorpay_token_id",
        "drop column subscriptions.subscription_anchor_day",
        "drop column subscriptions.recurring_failures",
    ):
        assert dropped not in lowered, "expansion must not drop contracted columns"


# --- transactions migration -------------------------------------------------


def test_transactionsEnforceOneCanonicalRowPerUser():
    sql = _transactionsSql()
    assert "create unique index" in sql.lower()
    assert "is_canonical" in sql
    assert "user_id" in sql


def test_transactionsAddLiveAttemptIdentitySupport():
    sql = _transactionsSql()
    # one live attempt per invoice revision via stored cycle key
    assert "billing_events" in sql
    assert "manual_billing" in sql.lower() or "manualbilling" in sql.lower()


def test_transactionsAddInvoiceGrantAndActivationUniqueness():
    sql = _transactionsSql()
    lowered = sql.lower()
    assert "operation_key" in lowered
    assert "unique" in lowered


def test_transactionsMigrationDropsNothing():
    sql = _transactionsSql()
    lowered = sql.lower()
    assert "drop column" not in lowered
