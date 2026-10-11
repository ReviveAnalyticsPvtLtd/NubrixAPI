from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
from api.services.billing.manualBillingContracts import CreditOperationContext
from api.services.credits import creditUsageSpool as spool
from test.test_manual_billing_runtime import NOW


def test_concurrent_conflicting_measurements_cannot_overwrite_each_other(tmp_path,monkeypatch):
    monkeypatch.setenv('CREDIT_USAGE_SPOOL_DIR',str(tmp_path))
    original=spool.tempfile.mkstemp
    barrier=Barrier(2)
    def coordinated(*args,**kwargs):
        result=original(*args,**kwargs)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(spool.tempfile,'mkstemp',coordinated)
    context=CreditOperationContext('user','life','period','operation','speech','operation',NOW,'sub','monthly_prepaid',1000)
    def retain(tokens):
        try:
            spool.retainUsage(context,tokens,'run')
            return 'ok'
        except ValueError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(retain,[100,200]))
    assert sorted(results)==['conflict','ok']
    files=list(tmp_path.glob('*.json'))
    assert len(files)==1 and json.loads(files[0].read_text())['tokensUsed'] in (100,200)
