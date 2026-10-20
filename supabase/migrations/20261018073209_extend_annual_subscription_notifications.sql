-- Add a durable annual T-30 milestone; keep existing delivery identities.
-- No phase activation, customer changes, new grants or provider calls.
SET LOCAL lock_timeout = '5s';
ALTER TABLE public.notification_deliveries DROP CONSTRAINT IF EXISTS notification_deliveries_type_chk;
ALTER TABLE public.notification_deliveries ADD CONSTRAINT notification_deliveries_type_chk
 CHECK (notification_type IN ('trial_expiry_warning','annual_renewal_upcoming',
  'monthly_renewal_ready','monthly_renewal_reminder','monthly_subscription_expired',
  'payment_receipt','monthly_cancellation_confirmation',
  'subscription_refund_initiated','subscription_refund_processed'));
