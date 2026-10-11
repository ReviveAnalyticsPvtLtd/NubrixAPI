-- Expired manual coverage uses zero domains. Preserve usage and top-up balances.
ALTER TABLE public.credit_balances
    DROP CONSTRAINT IF EXISTS credit_balances_domain_count_check;
ALTER TABLE public.credit_balances
    ADD CONSTRAINT credit_balances_domain_count_check
    CHECK (domain_count >= 0 AND domain_count <= 4);
