"""Historical maintenance cannot safely rewrite explicit V2 authority."""
import os


def requireLegacySubscriptionStorage(columns=()):
    phase = os.environ.get('SUBSCRIPTION_MODEL_PHASE','legacy')
    if phase not in ('legacy','shadow') or set(columns) & {
            'plan_code','trial_started_at','trial_ends_at','trial_closed_at','cancellation_requested_at'}:
        raise ValueError('SUBSCRIPTION_MODEL_V2_STORAGE: use the subscription-model review tools')


def checkLegacySubscriptionStorage(cursor):
    requireLegacySubscriptionStorage()
    cursor.execute("""SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='subscriptions'""")
    requireLegacySubscriptionStorage([
        row['column_name'] if isinstance(row,dict) else row[0] for row in cursor.fetchall()])
