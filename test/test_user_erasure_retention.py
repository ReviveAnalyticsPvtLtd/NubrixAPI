from api.services.userErasureRetention import operationalMetadata


def test_operational_financial_metadata_rejects_nested_private_fields():
    metadata = {'private':'account@example.test','manualBilling':{
        'purpose':'renewal','invoiceId':'invoice-1','currency':'INR','frozenAmount':100,
        'domains':['banking'],'periodStart':{'email':'account@example.test'},
        'providerObservation':{'complete':True,'outcome':'unpaid','private':'account@example.test'}},
        'refundIntentId':'refund-1','items':[{'paymentId':'payment-1','amount':50,'email':'private'}],
        'submissions':{'payment-1':{'state':'claimed','claimedAt':'time','email':'private'}},
        'providerRefunds':{}}
    cleaned = operationalMetadata(metadata,'request-1')
    assert 'private' not in str(cleaned) and 'account@example.test' not in str(cleaned)
    assert cleaned['manualBilling']['frozenAmount']==100
    assert cleaned['submissions']['payment-1']['state']=='claimed'


def test_legacy_refund_without_submission_history_remains_lookup_only():
    for original in ({}, {'submissions':None}, {'submissions':[]}):
        cleaned = operationalMetadata({'refundIntentId':'refund-1','items':[],**original},'request-1')
        assert 'submissions' not in cleaned


def test_existing_refund_submission_key_is_never_lost_even_with_invalid_state():
    cleaned = operationalMetadata({'refundIntentId':'refund-1','items':[],
        'submissions':{'payment-1':None}},'request-1')
    assert cleaned['submissions']['payment-1']=='unknown'


def test_purchase_recovery_facts_survive_erasure_without_contact():
    cleaned = operationalMetadata({'manualBilling': {'purpose': 'initial_purchase', 'purchaseDraftRevision': 2,
        'capturePolicy': 'automatic', 'purchaseCancellationAcceptedAt': '2026-10-20T12:00:00+00:00',
        'authorizedPaymentId': 'payment-1', 'contact': '+919999999999'}}, 'request-1')
    assert cleaned['manualBilling']['purchaseDraftRevision'] == 2
    assert cleaned['manualBilling']['capturePolicy'] == 'automatic'
    assert 'authorizedPaymentId' not in cleaned['manualBilling']
    assert 'purchaseCancellationAcceptedAt' not in cleaned['manualBilling']  # No user cancellation exists.
    assert 'contact' not in cleaned['manualBilling']
