import pytest
from api.adminErrors import AdminApiError
from api.services.userErasureRepository import UserErasureRepository
from test.test_user_erasure_repository import FakeConnection


def test_erasure_keeps_unresolved_approved_refund_recoverable():
    connection=FakeConnection(tables={'billing_events'},pendingRefund={'id':'refund-intent'})
    repository=UserErasureRepository(connectionFactory=lambda:connection)
    with pytest.raises(AdminApiError,match='refund must be reconciled'):
        repository.deleteDatabaseData('request','user-1',[])
    assert connection.commits==0 and connection.rollbacks==1
    assert not any('update public.billing_events' in query for query,_ in connection.state['executed'])
