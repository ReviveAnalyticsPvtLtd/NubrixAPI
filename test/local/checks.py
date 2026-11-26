"""Run the test-image checks and require their JUnit reports to contain executed cases."""

import os
import subprocess
import sys


POSTGRES = (
    "test_manual_billing_postgres_integration",
    "test_catalog_invoice_postgres",
    "test_admin_credit_reset_postgres",
    "test_gap_remediation_postgres",
    "test_live_billing_schema_postgres",
    "test_pricing_catalog_postgres",
    "test_subscription_model_postgres",
    "test_subscription_model_repository",
    "test_subscription_model_finalization",
    "test_subscription_erasure_postgres",
    "test_subscription_model_cutover_postgres",
    "test_subscription_model_contract",
    "test_purchase_checkout_postgres",
    "test_payment_refunds_postgres",
    "test_payment_replacement_postgres",
    "test_capture_finalization_postgres",
    "test_payment_fix_regressions_postgres",
)


def run_suite(name, modules, *, variables=None, allowed_skips=()):
    report = f"/app/logs/check-reports/{name}.xml"
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", f"--junitxml={report}",
         *(f"test/{module}.py" for module in modules)],
        env=os.environ | (variables or {}),
        check=True,
    )
    subprocess.run(
        [sys.executable, "scripts/check_ci_junit.py", report,
         *(argument for test in allowed_skips for argument in ("--allow-skip", test))],
        check=True,
    )


def integration():
    billing_opt_in = {"RUN_MANUAL_BILLING_INTEGRATION": "1", "RUN_LIVE_SCHEMA_INTEGRATION": "1"}
    run_suite("app-checks", ("test_api_readiness", "test_logtail_configuration"))
    run_suite(
        "redis-and-credit-recovery",
        ("test_credit_lua_integration", "test_manual_credit_recovery_integration"),
        variables=billing_opt_in | {"RUN_REDIS_INTEGRATION": "1"},
    )
    run_suite("website-visits", ("test_website_visit_repository_integration",))
    run_suite("website-visit-migration", ("test_website_visit_migration_integration",), variables={
        "WEBSITE_VISIT_TEST_DATABASE_URL": os.environ["WEBSITE_VISIT_MIGRATION_TEST_DATABASE_URL"],
    })
    run_suite("migration-rehearsal", ("test_migration_rehearsal_integration",))
    run_suite("postgres", POSTGRES, variables=billing_opt_in)


def sandbox():
    run_suite("sandbox", (
        "test_sandbox_storage_reads", "test_sandbox_run_limits", "test_sandbox_seccomp", "test_stored_tables",
    ), allowed_skips=(
        "test.test_sandbox_storage_reads.test_sameUidRunsNeedAnExplicitOptIn",
        "test.test_sandbox_storage_reads.test_parentThatCantChangeUidRefusesToRunWhenTheImageRequiresIt",
    ))


if __name__ == "__main__":
    {"integration": integration, "sandbox": sandbox}[sys.argv[1]]()
