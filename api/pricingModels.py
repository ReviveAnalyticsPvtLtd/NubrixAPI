"""Strict administrative price-publication input; actors are server-derived."""
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from api.services.billing.pricingCatalogContracts import MAX_PRICE_MINOR, validatePrices


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SkuPrices(_StrictModel):
    monthly: StrictInt = Field(ge=1, le=MAX_PRICE_MINOR)
    annual: StrictInt = Field(ge=1, le=MAX_PRICE_MINOR)
    small: StrictInt = Field(ge=1, le=MAX_PRICE_MINOR)
    medium: StrictInt = Field(ge=1, le=MAX_PRICE_MINOR)
    large: StrictInt = Field(ge=1, le=MAX_PRICE_MINOR)


class RegionalPrices(_StrictModel):
    INR: SkuPrices
    USD: SkuPrices

    @model_validator(mode='after')
    def validAmounts(self):
        validatePrices(self.model_dump())
        return self


class PublishPricingBody(_StrictModel):
    expectedRevision: StrictInt = Field(ge=1, le=9007199254740991)
    reason: str = Field(strict=True)
    prices: RegionalPrices

    @field_validator('reason')
    @classmethod
    def normalizeReason(cls, value):
        value = value.strip()
        if not 5 <= len(value) <= 500:
            raise ValueError('Reason must contain 5 to 500 characters')
        return value
