from fastapi import APIRouter, Depends, Header, Query, Request, status

from api.adminModels import (
    AdminOverviewPeriod,
    AdminAuditEventView,
    AdminFreeTrialExtensionRequest,
    AdminFreeTrialExtensionResponse,
    AdminFreeTrialReductionRequest,
    AdminFreeTrialReductionResponse,
    AdminLoginRequest,
    AdminLoginResponse,
    AdminLogoutResponse,
    AdminSubscriptionPatch,
    AdminSubscriptionView,
    AdminTokenCostOverviewView,
    AdminTokenUsageOverviewView,
    AdminWebsiteVisitOverviewView,
    AdminUserAccessPatch,
    AdminUserAccessView,
    AdminUserErasureAcceptedView,
    AdminUserErasureRequest,
    AdminUserPatch,
    AdminUserSignupOverviewView,
    AdminUserView,
)
from api.services.adminAuditService import (
    ADMIN_AUDIT_MAX_PAGE_SIZE,
    AdminAuditService,
    getAdminAuditService,
)
from api.services.adminAuthService import (
    AdminAuthService,
    AdminContext,
    getAdminAuthService,
    resolveAdminClientIp,
    verifyAdmin,
    verifyAdminForLogout,
)
from api.services.adminManagementService import (
    AdminManagementService,
    getAdminManagementService,
)
from api.services.adminOverviewService import (
    AdminOverviewService,
    getAdminOverviewService,
)
from api.services.adminTrialExtensionService import (
    AdminTrialExtensionService,
    getAdminTrialExtensionService,
)
from api.services.adminTrialReductionService import (
    AdminTrialReductionService,
    getAdminTrialReductionService,
)
from api.services.userErasureService import (
    UserErasureService,
    getUserErasureService,
)


router = APIRouter()


@router.post("/auth/login", response_model=AdminLoginResponse)
async def login(
    payload: AdminLoginRequest,
    request: Request,
    service: AdminAuthService = Depends(getAdminAuthService),
):
    return service.login(
        str(payload.email), payload.password, resolveAdminClientIp(request)
    )


@router.post("/auth/logout", response_model=AdminLogoutResponse)
async def logout(
    admin: AdminContext = Depends(verifyAdminForLogout),
    service: AdminAuthService = Depends(getAdminAuthService),
):
    service.logout(admin)
    return {"success": True}


@router.get(
    "/overview/user-signups", response_model=AdminUserSignupOverviewView
)
def getUserSignupOverview(
    period: AdminOverviewPeriod = Query(default="30d"),
    _admin: AdminContext = Depends(verifyAdmin),
    service: AdminOverviewService = Depends(getAdminOverviewService),
):
    return service.getUserSignupOverview(period)


@router.get(
    "/overview/token-usage", response_model=AdminTokenUsageOverviewView
)
def getTokenUsageOverview(
    period: AdminOverviewPeriod = Query(default="30d"),
    _admin: AdminContext = Depends(verifyAdmin),
    service: AdminOverviewService = Depends(getAdminOverviewService),
):
    return service.getTokenUsageOverview(period)


@router.get(
    "/overview/website-visits", response_model=AdminWebsiteVisitOverviewView
)
def getWebsiteVisitOverview(
    period: AdminOverviewPeriod = Query(default="30d"),
    _admin: AdminContext = Depends(verifyAdmin),
    service: AdminOverviewService = Depends(getAdminOverviewService),
):
    return service.getWebsiteVisitOverview(period)


@router.get(
    "/overview/token-cost", response_model=AdminTokenCostOverviewView
)
def getTokenCostOverview(
    period: AdminOverviewPeriod = Query(default="30d"),
    _admin: AdminContext = Depends(verifyAdmin),
    service: AdminOverviewService = Depends(getAdminOverviewService),
):
    return service.getTokenCostOverview(period)


@router.get("/users", response_model=list[AdminUserView])
async def listUsers(
    _admin: AdminContext = Depends(verifyAdmin),
    service: AdminManagementService = Depends(getAdminManagementService),
):
    return service.listUsers()


@router.patch("/users/{userId}", response_model=AdminUserView)
async def updateUser(
    userId: str,
    payload: AdminUserPatch,
    admin: AdminContext = Depends(verifyAdmin),
    service: AdminManagementService = Depends(getAdminManagementService),
):
    return service.updateUser(userId, payload, admin)


@router.patch(
    "/users/{userId}/access",
    response_model=AdminUserAccessView,
)
async def setUserAccess(
    userId: str,
    payload: AdminUserAccessPatch,
    admin: AdminContext = Depends(verifyAdmin),
    service: AdminManagementService = Depends(getAdminManagementService),
):
    return service.setUserAccess(userId, payload, admin)


@router.post(
    "/users/{userId}/erasure",
    response_model=AdminUserErasureAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def startUserErasure(
    userId: str,
    payload: AdminUserErasureRequest,
    idempotencyKey: str = Header(alias="Idempotency-Key"),
    admin: AdminContext = Depends(verifyAdmin),
    service: UserErasureService = Depends(getUserErasureService),
):
    return service.start(userId, payload, idempotencyKey, admin)


@router.post(
    "/free-trial/extensions",
    response_model=AdminFreeTrialExtensionResponse,
)
def extendFreeTrials(
    payload: AdminFreeTrialExtensionRequest,
    idempotencyKey: str = Header(alias="Idempotency-Key"),
    admin: AdminContext = Depends(verifyAdmin),
    service: AdminTrialExtensionService = Depends(getAdminTrialExtensionService),
):
    return service.extend(payload, idempotencyKey, admin)


@router.post(
    "/free-trial/reductions",
    response_model=AdminFreeTrialReductionResponse,
)
def reduceFreeTrial(
    payload: AdminFreeTrialReductionRequest,
    idempotencyKey: str = Header(alias="Idempotency-Key"),
    admin: AdminContext = Depends(verifyAdmin),
    service: AdminTrialReductionService = Depends(
        getAdminTrialReductionService
    ),
):
    return service.reduce(payload, idempotencyKey, admin)


@router.get("/audit", response_model=list[AdminAuditEventView])
async def listAuditEvents(
    limit: int = Query(default=50, ge=1, le=ADMIN_AUDIT_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    targetType: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    _admin: AdminContext = Depends(verifyAdmin),
    service: AdminAuditService = Depends(getAdminAuditService),
):
    return service.listEvents(
        limit=limit, offset=offset, targetType=targetType, outcome=outcome
    )


@router.get("/subscriptions", response_model=list[AdminSubscriptionView])
async def listSubscriptions(
    _admin: AdminContext = Depends(verifyAdmin),
    service: AdminManagementService = Depends(getAdminManagementService),
):
    return service.listSubscriptions()


@router.patch(
    "/subscriptions/{subscriptionId}", response_model=AdminSubscriptionView
)
async def updateSubscription(
    subscriptionId: str,
    payload: AdminSubscriptionPatch,
    admin: AdminContext = Depends(verifyAdmin),
    service: AdminManagementService = Depends(getAdminManagementService),
):
    return service.updateSubscription(subscriptionId, payload, admin)
