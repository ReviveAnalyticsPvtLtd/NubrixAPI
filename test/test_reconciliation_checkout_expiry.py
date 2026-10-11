"""The 15-minute reconciliation closes lapsed checkouts after provider recovery."""
from unittest.mock import MagicMock, patch


def test_reconciliation_expires_lapsed_checkouts_after_provider_recovery():
    from nubrix.triggers.tasks.reconciliationTask import ReconciliationTask
    calls = []
    repository = MagicMock()
    repository.expireStaleCheckouts.side_effect = lambda: calls.append('expire') or {'attemptsExpired': 2, 'invoicesExpired': 1}
    recovery = MagicMock()
    recovery.return_value.execute.side_effect = lambda: calls.append('recover') or {'resolved': 0}
    task = ReconciliationTask.__new__(ReconciliationTask)
    task.razorpayClient = MagicMock()
    task._reconcileStaleAttempts = lambda: {'resolved': 0, 'errors': 0}
    task._generateAnomalyReport = lambda: {'totalAnomalies': 0}
    with patch('api.services.billing.manualBillingRepository.getManualBillingRepository', return_value=repository), \
            patch('api.services.billing.manualBillingRecoveryService.ManualBillingRecoveryService', recovery), \
            patch('api.services.credits.manualCreditRepository.ManualCreditRepository'):
        result = task.execute()
    assert calls == ['recover', 'expire']
    assert result['checkoutExpiry'] == {'attemptsExpired': 2, 'invoicesExpired': 1}
