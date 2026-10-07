-- Financial writes are server operations. RLS remains bypassable by the
-- backend owner and service_role; browser roles receive no financial access.
ALTER TABLE public.credit_balances ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public."Invoices" ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.billing_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admin_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admin_credit_reset_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admin_credit_reset_targets ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE public.credit_balances, public.subscriptions,
    public."Invoices", public.billing_events, public.admin_audit_log,
    public.admin_credit_reset_operations, public.admin_credit_reset_targets
    FROM PUBLIC, anon, authenticated, service_role;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.credit_balances,
    public.subscriptions, public."Invoices", public.billing_events TO service_role;
-- Retention deletes remain supported; audit entries cannot be edited or truncated.
GRANT SELECT, INSERT, DELETE ON TABLE public.admin_audit_log TO service_role;
-- Existing default ACLs must not expand the reset migrations' explicit policy.
GRANT SELECT, INSERT ON TABLE public.admin_credit_reset_operations TO service_role;
GRANT SELECT, INSERT, UPDATE ON TABLE public.admin_credit_reset_targets TO service_role;

-- Reconcile every existing overload, including PUBLIC's inherited EXECUTE.
-- Older isolated schemas may not contain these legacy functions at all.
DO $financial_access$
DECLARE
    function_signature text;
BEGIN
    FOR function_signature IN
        SELECT format('%I.%I(%s)', n.nspname, p.proname,
            pg_get_function_identity_arguments(p.oid))
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prokind = 'f'
          AND p.proname IN ('grant_topup_tokens', 'decrement_topup_tokens',
              'clawback_topup_tokens', 'reconcile_credit_balance_if_no_admin_refresh')
    LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION %s FROM PUBLIC, anon, authenticated',
            function_signature);
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', function_signature);
    END LOOP;
END
$financial_access$;
