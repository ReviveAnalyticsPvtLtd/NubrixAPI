import sys
import types
import unittest
from datetime import datetime, timedelta, timezone


if "logtail" not in sys.modules:
    logtailStub = types.ModuleType("logtail")

    class _DummyLogtailHandler:
        def __init__(self, *args, **kwargs):
            pass

    logtailStub.LogtailHandler = _DummyLogtailHandler
    sys.modules["logtail"] = logtailStub

if "loguru" not in sys.modules:
    loguruStub = types.ModuleType("loguru")

    class _DummyLogger:
        def remove(self, *args, **kwargs):
            return None

        def add(self, *args, **kwargs):
            return None

        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

    loguruStub.logger = _DummyLogger()
    sys.modules["loguru"] = loguruStub

if "supabase" not in sys.modules:
    supabaseStub = types.ModuleType("supabase")
    supabaseStub.create_client = lambda *args, **kwargs: None
    sys.modules["supabase"] = supabaseStub


class _FakeResponse:
    def __init__(self, data=None):
        self.data = data or []


class _FakeTable:
    def __init__(self, name, state):
        self.name = name
        self.state = state
        self.filters = []
        self.updatePayload = None
        self.insertPayload = None
        self._limit = None
        self._order = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field, value):
        self.filters.append(("in", field, value))
        return self

    def order(self, field, desc=False):
        self._order = (field, desc)
        return self

    def limit(self, value):
        self._limit = value
        return self

    def update(self, payload):
        self.updatePayload = payload
        return self

    def insert(self, payload):
        self.insertPayload = payload
        return self

    def execute(self):
        rows = list(self.state.get("rows", {}).get(self.name, []))
        for op, field, value in self.filters:
            if op == "eq":
                rows = [row for row in rows if row.get(field) == value]
            elif op == "in":
                rows = [row for row in rows if row.get(field) in value]
        if self._order:
            field, desc = self._order
            rows.sort(key=lambda row: row.get(field) or "", reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        if self.updatePayload is not None:
            self.state.setdefault("updates", []).append({
                "table": self.name,
                "filters": list(self.filters),
                "payload": dict(self.updatePayload),
            })
            for row in rows:
                row.update(self.updatePayload)
            return _FakeResponse(rows)
        if self.insertPayload is not None:
            payload = dict(self.insertPayload)
            self.state.setdefault("inserts", []).append({
                "table": self.name,
                "payload": payload,
            })
            self.state.setdefault("rows", {}).setdefault(self.name, []).append(payload)
            return _FakeResponse([payload])
        return _FakeResponse(rows)


class _FakeClient:
    def __init__(self, rows=None):
        self.state = {"rows": rows or {}, "updates": [], "inserts": []}

    def table(self, name):
        return _FakeTable(name, self.state)


class EntitlementBoundaryTaskTests(unittest.TestCase):
    def test_applies_pending_removals_after_invoice_metadata_boundary(self):
        from nubrix.triggers.tasks.entitlementBoundaryTask import EntitlementBoundaryTask

        now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
        boundary = "2026-05-26T11:22:10+00:00"
        client = _FakeClient({
            "subscriptions": [{
                "id": "sub_1",
                "user_id": "u1",
                "is_canonical": True,
                "billing_mode": "annual_prepaid",
                "status": "active",
                "current_period_start": "2025-05-26T11:22:10+00:00",
                "current_period_end": "2026-05-26T11:22:10+00:00",
                "renewal_due_at": "2027-05-26T11:22:10+00:00",
                "auto_renew_enabled": False,
                "payment_collection_mode": "authenticated_checkout",
                "default_currency": "INR",
                "version": 1,
                "subscribed_experts": ["banking", "manufacturing", "supplychain"],
                "domain_count": 3,
                "pending_removals": ["manufacturing"],
                "pending_additions": [],
                "billing_state": {},
                "razorpay_customer_id": "cust_1",
                "razorpay_token_id": None,
                "subscription_anchor_day": 26,
                "recurring_failures": 0,
                "cancellation_reason": None,
            }],
            "Invoices": [{
                "id": "inv_1",
                "subscription_id": "sub_1",
                "billing_reason": "renewal",
                "period_start": boundary,
                "metadata_json": {
                    "entitlementChangeEffectiveAt": boundary,
                },
            }],
        })
        task = EntitlementBoundaryTask(client=client, now=lambda: now)

        result = task.execute()

        self.assertEqual(result, {"applied": 1, "skipped": 0, "errors": 0})
        update = next(item for item in client.state["updates"] if item["table"] == "subscriptions")
        self.assertEqual(update["payload"]["subscribed_experts"], ["banking", "supplychain"])
        self.assertEqual(update["payload"]["domain_count"], 2)
        self.assertEqual(update["payload"]["pending_removals"], [])
        event = next(item for item in client.state["inserts"] if item["table"] == "billing_events")
        self.assertEqual(event["payload"]["event_type"], "subscription.pending_removals_applied")

    def test_skips_pending_removals_before_boundary(self):
        from nubrix.triggers.tasks.entitlementBoundaryTask import EntitlementBoundaryTask

        future = datetime.now(timezone.utc) + timedelta(days=1)
        client = _FakeClient({
            "subscriptions": [{
                "id": "sub_1",
                "user_id": "u1",
                "is_canonical": True,
                "status": "active",
                "current_period_end": future.isoformat(),
                "subscribed_experts": ["banking", "manufacturing"],
                "domain_count": 2,
                "pending_removals": ["manufacturing"],
            }],
            "Invoices": [],
        })
        task = EntitlementBoundaryTask(client=client, now=lambda: datetime.now(timezone.utc))

        result = task.execute()

        self.assertEqual(result, {"applied": 0, "skipped": 1, "errors": 0})
        self.assertEqual(client.state["updates"], [])


if __name__ == "__main__":
    unittest.main()
