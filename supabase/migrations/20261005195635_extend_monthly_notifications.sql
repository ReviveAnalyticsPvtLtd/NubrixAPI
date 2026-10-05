-- extend_monthly_notifications
--
-- Extends the durable notification delivery infrastructure with the manual
-- monthly billing types. Retains trial_expiry_warning and its flow; the
-- existing delivery/claim/lease/reconciliation machinery is unchanged.
--
-- The type check is recreated (dropped-if-exists first) because CHECK
-- constraints cannot be altered in place. Existing rows keep their values.

ALTER TABLE public.notification_deliveries
    DROP CONSTRAINT IF EXISTS notification_deliveries_type_chk;

ALTER TABLE public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_type_chk
    CHECK (notification_type IN (
        'trial_expiry_warning',
        'monthly_renewal_ready',
        'monthly_renewal_reminder',
        'monthly_subscription_expired',
        'payment_receipt',
        'monthly_cancellation_confirmation',
        'subscription_refund_initiated',
        'subscription_refund_processed'
    ));

-- period_end remains NOT NULL for all types: billing intents must supply the
-- associated purchased/target period (the repository passes it explicitly;
-- no schema relaxation is needed).