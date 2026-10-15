"""Price-catalog domain contracts. Amounts are always integer minor units."""
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from collections.abc import Mapping

CURRENCIES = ('INR', 'USD')
SKUS = ('monthly', 'annual', 'small', 'medium', 'large')
MAX_PRICE_MINOR = 9007199254740991 // 48


def validatePrices(prices: dict) -> dict:
    if not isinstance(prices, Mapping) or set(prices) != set(CURRENCIES):
        raise ValueError('Exactly INR and USD prices are required')
    result = {}
    for currency in CURRENCIES:
        values = prices[currency]
        if not isinstance(values, Mapping) or set(values) != set(SKUS):
            raise ValueError(f'{currency}: all five prices are required')
        result[currency] = {}
        for sku in SKUS:
            amount = values[sku]
            minimum = 100 if currency == 'INR' else 1
            if type(amount) is not int or not minimum <= amount <= MAX_PRICE_MINOR:
                raise ValueError(f'{currency}.{sku}: invalid amount')
            result[currency][sku] = amount
    return result


@dataclass(frozen=True)
class CatalogSnapshot:
    revision: int
    prices: Mapping
    publishedAt: datetime
    adminId: str | None
    adminEmail: str
    reason: str

    def __post_init__(self):
        prices = validatePrices(self.prices)
        object.__setattr__(self, 'prices', MappingProxyType({
            currency: MappingProxyType(values) for currency, values in prices.items()
        }))


@dataclass(frozen=True)
class PublishResult:
    catalog: CatalogSnapshot
    noOp: bool


class CatalogConflict(ValueError):
    def __init__(self, code: str, currentRevision: int | None = None):
        super().__init__(code)
        self.code = code
        self.currentRevision = currentRevision


class CatalogUnavailable(RuntimeError):
    pass
