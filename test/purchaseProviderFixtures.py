"""Synthetic Razorpay provider stub shared by purchase checkout tests; never real money."""
from types import SimpleNamespace
from uuid import uuid4


class SyntheticPurchaseProvider:
    def __init__(self):
        self.orders, self.payments, self.orderCalls, self.captureCalls = {}, {}, [], []
        self.captureFault = None
        self.onCapture = None
        self.order = SimpleNamespace(create=self.createOrder, fetch=lambda key,**options: dict(self.orders[key]),
            payments=self.orderPayments, all=lambda params,**options: {'items': [dict(o) for o in self.orders.values()
                if o['receipt'] == params['receipt']][params['skip']:params['skip']+params['count']]})
        self.payment = SimpleNamespace(fetch=lambda key,**options: dict(self.payments[key]), capture=self.capture)

    def createOrder(self, payload, **options):
        self.orderCalls.append(payload)
        key = 'order-synthetic-' + str(uuid4())
        self.orders[key] = {**payload, 'id': key, 'status': 'created'}
        return dict(self.orders[key])

    def orderPayments(self, key, params, **options):
        return {'items': [dict(p) for p in self.payments.values() if p['order_id'] == key]
            [params['skip']:params['skip']+params['count']]}

    def authorize(self, intent, key=None):
        key = key or 'pay-synthetic-' + str(uuid4())
        self.payments[key] = {'id': key, 'order_id': intent.razorpayOrderId, 'status': 'authorized',
            'amount': intent.amount, 'currency': intent.currency}
        return dict(self.payments[key])

    def capture(self, paymentId, amount, data, **options):
        self.captureCalls.append((paymentId, {'amount': amount, **data}))
        if self.onCapture:
            self.onCapture()
        if self.captureFault == 'before':
            raise TimeoutError('Synthetic response unknown before acceptance')
        self.payments[paymentId]['status'] = 'captured'
        if self.captureFault == 'after':
            raise TimeoutError('Synthetic response lost after acceptance')
        return dict(self.payments[paymentId])
