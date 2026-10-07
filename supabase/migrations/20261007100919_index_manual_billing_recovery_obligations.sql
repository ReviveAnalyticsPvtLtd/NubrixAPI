-- IR-10: bound metadata pages, index active recovery work and measurement lookup.
-- Forward-only expansion; no provider, entitlement or financial mutation.
CREATE INDEX IF NOT EXISTS idx_manual_billing_recovery_due
ON public.billing_events (updated_at,id)
WHERE (event_category='payment_attempt' AND metadata_json->'manualBilling' IS NOT NULL
 AND (payment_status IN ('created','pending_provider_ack','authorized') OR
   (payment_status<>'captured' AND metadata_json->'manualBilling'->>'closedAt' IS NOT NULL
    AND metadata_json->'manualBilling'->>'closureReconciledAt' IS NULL)))
 OR (event_type='refund.intent' AND event_status<>'processed');

CREATE INDEX IF NOT EXISTS idx_manual_billing_obligation_page
ON public.billing_events (occurred_at,id)
WHERE (event_category='payment_attempt' AND
 (payment_status IN ('created','pending_provider_ack','authorized') OR
  (payment_status<>'captured' AND metadata_json->'manualBilling'->>'closedAt' IS NOT NULL
   AND metadata_json->'manualBilling'->>'closureReconciledAt' IS NULL)))
 OR (event_type IN ('payment.capture','payment.unmapped') AND event_status='REQUIRES_RECONCILIATION')
 OR (event_type='refund.intent' AND event_status<>'processed')
 OR (event_type='credit.usage_reported' AND event_status='PENDING')
 OR (event_type='credit.operation_settled' AND CAST(COALESCE(metadata_json->>'unfundedTokens','0') AS bigint)>0)
 OR (event_type='credit.operation_admitted' AND event_status<>'MEASURED')
 OR (event_type='email.billing_intent.committed' AND event_status='COMMITTED');

CREATE INDEX IF NOT EXISTS idx_manual_credit_measurement_operation
ON public.billing_events (user_id,(metadata_json->>'operationId'))
WHERE event_type IN ('credit.usage_reported','credit.operation_settled');

CREATE INDEX IF NOT EXISTS idx_manual_delivery_obligation_page
ON public.notification_deliveries (created_at,id)
WHERE status IN ('PENDING','RETRY_PENDING','SENDING','ACCEPTED')
 OR last_error_code IN ('AMBIGUOUS_SEND','AMBIGUOUS_SEND_UNRESOLVED');
