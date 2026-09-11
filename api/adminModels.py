from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


AdminSubscriptionStatus = Literal[
    "none", "trial", "active", "renewal_upcoming", "payment_pending",
    "past_due", "suspended", "paused", "cancelled", "expired",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminLoginRequest(_StrictModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class AdminPublic(_StrictModel):
    id: str
    email: str
    name: str


class AdminLoginResponse(_StrictModel):
    token: str
    admin: AdminPublic


class AdminLogoutResponse(_StrictModel):
    success: bool


class AdminUserPatch(_StrictModel):
    email: EmailStr | None = None
    fullName: str | None = None
    phoneNumber: str | None = None
    onboarded: bool | None = None
    companyName: str | None = None
    role: str | None = None
    country: str | None = None
    goals: str | None = None

    @model_validator(mode="after")
    def validatePatch(self):
        if not self.model_fields_set:
            raise ValueError("At least one editable field is required")
        if "email" in self.model_fields_set and self.email is None:
            raise ValueError("email cannot be null")
        if "onboarded" in self.model_fields_set and self.onboarded is None:
            raise ValueError("onboarded cannot be null")
        return self


class AdminUserAccessPatch(_StrictModel):
    banned: bool
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def normalizeReason(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value


class AdminUserErasureStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PARTIALLY_FAILED = "PARTIALLY_FAILED"
    COMPLETED = "COMPLETED"


class AdminUserErasureRequest(_StrictModel):
    confirmation: Literal["ERASE"]
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def normalizeReason(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value


class AdminUserErasureAcceptedView(_StrictModel):
    requestId: str
    userId: str
    status: AdminUserErasureStatus
    createdAt: str


class AdminFreeTrialExtensionRequest(_StrictModel):
    userId: str = Field(min_length=1, max_length=128)
    days: int = Field(ge=1, le=30, strict=True)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("userId")
    @classmethod
    def normalizeUserId(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("userId cannot be blank")
        return normalized

    @field_validator("reason", mode="before")
    @classmethod
    def normalizeReason(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value


class AdminFreeTrialExtensionResponse(_StrictModel):
    extensionId: str
    userId: str
    outcome: Literal["EXTENDED", "FAILED"]
    daysAdded: int | None = Field(default=None, ge=1, le=30)
    previousExpiry: str | None = None
    newExpiry: str | None = None
    creditsRefreshed: bool
    creditSyncStatus: Literal[
        "SYNCED", "PENDING", "SUPERSEDED", "CANCELLED", "NOT_APPLICABLE"
    ]
    accessStillBanned: bool
    errorCode: str | None = None


class AdminFreeTrialReductionRequest(_StrictModel):
    userId: str = Field(min_length=1, max_length=128)
    days: int = Field(ge=1, le=30, strict=True)
    reason: str = Field(min_length=1, max_length=1000)
    confirmation: Literal["REDUCE"]

    @field_validator("userId", "reason", mode="before")
    @classmethod
    def normalizeRequiredText(cls, value):
        return value.strip() if isinstance(value, str) else value


class AdminFreeTrialReductionResponse(_StrictModel):
    reductionId: str
    userId: str
    outcome: Literal["REDUCED", "FAILED"]
    daysRemoved: int | None = Field(default=None, ge=1, le=30)
    previousExpiry: str | None = None
    newExpiry: str | None = None
    accessStillBanned: bool
    errorCode: str | None = None


class AdminSubscriptionPatch(_StrictModel):
    status: AdminSubscriptionStatus | None = None
    subscribed_experts: str | None = None
    domain_count: int | None = Field(default=None, ge=1, le=4)

    @model_validator(mode="after")
    def validatePatch(self):
        if not self.model_fields_set:
            raise ValueError("At least one editable field is required")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class AdminUserView(_StrictModel):
    userId: str
    email: str
    fullName: str | None = None
    phoneNumber: str | None = None
    profileImage: str | None = None
    onboarded: bool
    currentWorkspaceId: str | None = None
    companyName: str | None = None
    role: str | None = None
    profileBio: str | None = None
    usage: str | None = None
    industryType: str | None = None
    companySize: str | None = None
    country: str | None = None
    goals: str | None = None
    source: str | None = None
    isBanned: bool
    bannedAt: str | None = None
    bannedBy: str | None = None
    banReason: str | None = None


class AdminUserAccessView(_StrictModel):
    userId: str
    isBanned: bool
    bannedAt: str | None = None
    bannedBy: str | None = None
    banReason: str | None = None
    sessionsRevoked: int = Field(ge=0)
    supabaseAuthSynced: bool
    warnings: list[str]


class AdminAuditEventView(_StrictModel):
    id: str
    admin_id: str | None = None
    admin_email: str
    session_id: str | None = None
    actor_type: str
    action: str
    target_type: str
    target_id: str | None = None
    changed_fields: str
    details: str
    outcome: str
    created_at: str


class AdminSubscriptionView(_StrictModel):
    id: str
    user_id: str
    billing_mode: str
    current_period_start: str | None = None
    current_period_end: str | None = None
    renewal_due_at: str | None = None
    auto_renew_enabled: bool
    payment_collection_mode: str
    status: str
    default_currency: str
    subscribed_experts: str
    domain_count: int
    pending_removals: str
    pending_additions: str
    billing_state: str
    razorpay_customer_id: str | None = None
    razorpay_token_id: str | None = None
    subscription_anchor_day: int | None = None
    recurring_failures: int
    cancellation_reason: str | None = None
    version: int
    plan_type: str
    created_at: str
    updated_at: str


AdminOverviewPeriod = Literal["7d", "14d", "30d", "90d", "6m", "1y"]
AdminOverviewGranularity = Literal["day", "week", "month"]


class AdminSignupDataset(_StrictModel):
    """One Chart.js-shaped series; the frontend maps this straight to ECharts."""

    label: str
    data: list[int]


class AdminSignupChart(_StrictModel):
    labels: list[str]
    datasets: list[AdminSignupDataset]


class AdminUserSignupOverviewView(_StrictModel):
    period: AdminOverviewPeriod
    granularity: AdminOverviewGranularity
    timezone: str
    rangeStart: str
    rangeEnd: str
    lastUpdatedAt: str
    totalSignups: int
    chart: AdminSignupChart


class AdminTokenUsageOverviewView(_StrictModel):
    period: AdminOverviewPeriod
    granularity: AdminOverviewGranularity
    timezone: str
    rangeStart: str
    rangeEnd: str
    lastUpdatedAt: str
    totalTokens: int = Field(ge=0)
    chart: AdminSignupChart


class AdminWebsiteVisitOverviewView(_StrictModel):
    period: AdminOverviewPeriod
    granularity: AdminOverviewGranularity
    timezone: str
    rangeStart: str
    rangeEnd: str
    lastUpdatedAt: str
    totalVisits: int = Field(ge=0)
    chart: AdminSignupChart


class AdminTokenCostDataset(_StrictModel):
    label: str
    data: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]]


class AdminTokenCostChart(_StrictModel):
    labels: list[str]
    datasets: list[AdminTokenCostDataset]


class AdminTokenCostOverviewView(_StrictModel):
    period: AdminOverviewPeriod
    granularity: AdminOverviewGranularity
    timezone: str
    rangeStart: str
    rangeEnd: str
    lastUpdatedAt: str
    totalCost: float = Field(ge=0, allow_inf_nan=False)
    currency: Literal["USD"]
    chart: AdminTokenCostChart
