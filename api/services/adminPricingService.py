"""Authenticated pricing management. The browser never writes catalog tables."""
from api.adminErrors import AdminApiError
from api.services.billing.pricingCatalogContracts import CatalogConflict, CatalogUnavailable
from api.services.billing.pricingCatalogRepository import getPricingCatalogRepository


def serializeCatalog(catalog):
    return {'revision':catalog.revision,
            'prices':{currency:dict(values) for currency,values in catalog.prices.items()},
            'publishedAt':catalog.publishedAt.isoformat(),
            'publishedBy':{'id':catalog.adminId,'email':catalog.adminEmail},
            'reason':catalog.reason}


class AdminPricingService:
    def __init__(self, repository=None):
        self.repository = repository or getPricingCatalogRepository()

    def read(self, *, beforeRevision=None):
        try:
            history, cursor = self.repository.listHistory(beforeRevision=beforeRevision)
            current = self.repository.loadCurrent()
            return {'current':serializeCatalog(current), 'history':[serializeCatalog(c) for c in history], 'nextCursor':cursor}
        except CatalogUnavailable as exc:
            raise AdminApiError(503, 'Prices are temporarily unavailable', {'code':'PRICE_UNAVAILABLE'}) from exc

    def publish(self, admin, body, idempotencyKey):
        if not 1 <= len(idempotencyKey) <= 128 or any(c.isspace() for c in idempotencyKey):
            raise AdminApiError(422, 'Invalid request key', {'code':'INVALID_PRICE','Idempotency-Key':'Use a valid request key'})
        try:
            result = self.repository.publish(admin=admin, expectedRevision=body.expectedRevision,
                reason=body.reason, prices=body.prices.model_dump(), idempotencyKey=idempotencyKey)
            return {'catalog':serializeCatalog(result.catalog),'noOp':result.noOp}
        except CatalogConflict as exc:
            raise AdminApiError(409, 'Prices changed. Reload the latest prices.', {'code':exc.code}) from exc
        except CatalogUnavailable as exc:
            raise AdminApiError(503, 'Prices could not be saved. Try again.', {'code':'PRICE_UNAVAILABLE'}) from exc
        except ValueError as exc:
            raise AdminApiError(422, 'Invalid prices', {'code':'INVALID_PRICE'}) from exc


def getAdminPricingService():
    return AdminPricingService()
