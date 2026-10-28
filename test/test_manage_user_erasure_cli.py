from scripts import manage_user_erasure


REQUEST_ID = "8cfdb150-417d-47ab-acd1-fef39d2bc14e"


class FakeRepository:
    def __init__(self, request=None, retry=True):
        self.request = request
        self.retry = retry
        self.retried = []

    def getRequest(self, requestId):
        return self.request

    def retryRequest(self, requestId):
        self.retried.append(requestId)
        return self.retry


def test_retry_requeues_a_partially_failed_request(capsys):
    repository = FakeRepository({"status": "PARTIALLY_FAILED"})
    queued = []

    code = manage_user_erasure.main(
        ["retry", "--request-id", REQUEST_ID],
        repository=repository,
        enqueue=queued.append,
    )

    assert code == 0
    assert repository.retried == [REQUEST_ID]
    assert queued == [REQUEST_ID]
    assert "Queued erasure request" in capsys.readouterr().out


def test_retry_rejects_missing_or_completed_request_without_queueing(capsys):
    queued = []
    assert manage_user_erasure.main(
        ["retry", "--request-id", REQUEST_ID],
        repository=FakeRepository(None),
        enqueue=queued.append,
    ) == 1
    assert manage_user_erasure.main(
        ["retry", "--request-id", REQUEST_ID],
        repository=FakeRepository({"status": "COMPLETED"}),
        enqueue=queued.append,
    ) == 1
    assert queued == []
    assert "cannot be retried" in capsys.readouterr().err


def test_status_explicitly_reports_original_retention_without_resource_identifiers(capsys):
    import json
    repository = FakeRepository({'status':'COMPLETED','target_user_id':'private-user',
        'resource_manifest':{'projectIds':['private-project']},
        'retention_manifest':{'policyCode':'ORIGINAL_AUDITS_RETAINED',
            'originalIdentifiersRetained':True,'originalMetadataRetained':True,
            'retainedRecordCounts':{'billing_events':8},'retentionExpiresAt':None,'purgeScheduled':False}})
    assert manage_user_erasure.main(['status','--request-id',REQUEST_ID],repository=repository)==0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result['retention']['originalIdentifiersRetained'] is True
    assert result['retention']['policyCode']=='ORIGINAL_AUDITS_RETAINED'
    assert 'private-' not in output and repository.retried==[]
