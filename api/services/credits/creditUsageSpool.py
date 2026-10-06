"""Recover measured usage during SQL outages; configure a persistent private volume."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile


def spoolDirectory():
    return Path(os.environ.get('CREDIT_USAGE_SPOOL_DIR','.runtime/credit-usage'))


def retainUsage(context,tokensUsed,runId):
    data={**asdict(context),'admittedAt':context.admittedAt.isoformat(),
        'runId':str(runId),'tokensUsed':int(tokensUsed)}
    key=sha256(json.dumps([context.userId,context.operationId,str(runId)]).encode()).hexdigest()
    directory=spoolDirectory()
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/(key+'.json')
    if target.exists():
        if json.loads(target.read_text(encoding='utf-8'))!=data:
            raise ValueError('CREDIT_USAGE_IDENTITY_CONFLICT')
        return target
    # Atomic replace + fsync retains complete measurements across worker crashes.
    descriptor,temporary=tempfile.mkstemp(prefix=key+'.',suffix='.tmp',dir=directory)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            json.dump(data,stream,sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def recoverSpooledUsage(repository,limit=100):
    summary={'settled':0,'errors':0}
    for path in sorted(spoolDirectory().glob('*.json'))[:limit]:
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            context=repository._context(data)
            repository.reportUsage(context,data['tokensUsed'],data['runId'])
            repository.settle(context,data['tokensUsed'],data['runId'])
            path.unlink()
            summary['settled']+=1
        except Exception:
            summary['errors']+=1
    return summary
