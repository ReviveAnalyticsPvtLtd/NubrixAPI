"""Bounded per-request options for purchase recovery; never mutate a shared SDK."""
import math
import os


def purchaseProviderOptions():
    seconds=float(os.environ.get('PURCHASE_PROVIDER_TIMEOUT_SECONDS','10'))
    if not math.isfinite(seconds) or not 1<=seconds<=30:
        raise ValueError('PURCHASE_PROVIDER_TIMEOUT_INVALID')
    return {'timeout':seconds}
