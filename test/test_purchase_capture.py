"""Synthetic provider faults exercise capture orchestration without real money."""
import importlib
import importlib.util
from types import SimpleNamespace
from uuid import uuid4

import pytest


class SyntheticPurchaseProvider:
    def __init__(self):
        self.orders, self.payments, self.orderCalls, self.captureCalls = {}, {}, [], []
        self.captureFault = None
        self.onCapture = None
        self.order = SimpleNamespace(create=self.createOrder, fetch=lambda key: dict(self.orders[key]),
            payments=self.orderPayments, all=lambda params: {'items': [dict(o) for o in self.orders.values()
                if o['receipt'] == params['receipt']][params['skip']:params['skip']+params['count']]})
        self.payment = SimpleNamespace(fetch=lambda key: dict(self.payments[key]), capture=self.capture)

    def createOrder(self, payload):
        self.orderCalls.append(payload)
        key = 'order-synthetic-' + str(uuid4())
        self.orders[key] = {**payload, 'id': key, 'status': 'created'}
        return dict(self.orders[key])

    def orderPayments(self, key, params):
        return {'items': [dict(p) for p in self.payments.values() if p['order_id'] == key]
            [params['skip']:params['skip']+params['count']]}

    def authorize(self, intent, key=None):
        key = key or 'pay-synthetic-' + str(uuid4())
        self.payments[key] = {'id': key, 'order_id': intent.razorpayOrderId, 'status': 'authorized',
            'amount': intent.amount, 'currency': intent.currency}
        return dict(self.payments[key])

    def capture(self, paymentId, amount, data):
        self.captureCalls.append((paymentId, {'amount': amount, **data}))
        if self.onCapture:
            self.onCapture()
        if self.captureFault == 'before':
            raise TimeoutError('Synthetic response unknown before acceptance')
        self.payments[paymentId]['status'] = 'captured'
        if self.captureFault == 'after':
            raise TimeoutError('Synthetic response lost after acceptance')
        return dict(self.payments[paymentId])


def test_capture_implementation_exposes_execute_and_lookup_recovery():
    name = 'api.services.billing.purchaseCaptureService'
    assert importlib.util.find_spec(name), 'Durable capture service must exist'
    service = importlib.import_module(name).PurchaseCaptureService
    assert callable(service.execute) and callable(service.recover)
