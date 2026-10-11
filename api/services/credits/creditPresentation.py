"""Pure helpers for presenting monthly and purchased credit balances."""

from api.services.credits.creditConfig import TOKEN_TO_CREDIT_RATIO
from api.services.credits.creditMath import tokensToCredits


__all__ = [
    "buildCreditBalanceView",
    "buildCreditView",
    "buildProfileCreditView",
    "deriveActiveTopupWindow",
]


def _nonNegativeInt(value) -> int:
    """Coerce token counts to non-negative integers."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def deriveActiveTopupWindow(
    availableTokens: int,
    lotsNewestFirst: list[int],
) -> dict[str, int]:
    """Return total and used tokens across the active FIFO purchase lots."""
    available = _nonNegativeInt(availableTokens)
    if available == 0:
        return {"total": 0, "used": 0}

    activeTotal = 0
    for lotTokens in lotsNewestFirst or []:
        lot = _nonNegativeInt(lotTokens)
        if lot == 0:
            continue
        activeTotal += lot
        if activeTotal >= available:
            return {"total": activeTotal, "used": activeTotal - available}

    return {"total": available, "used": 0}


def buildCreditView(snapshot: dict, includeTokens: bool = False) -> dict:
    """Build the nested public monthly/top-up balance contract."""
    monthlyTotal = _nonNegativeInt(snapshot.get("monthlyTokenQuota", 0))
    monthlyUsed = min(monthlyTotal, _nonNegativeInt(snapshot.get("usedTokens", 0)))
    topupTotal = _nonNegativeInt(snapshot.get("topupTotalTokens", 0))
    topupUsed = min(topupTotal, _nonNegativeInt(snapshot.get("topupUsedTokens", 0)))

    view = {
        "monthlyCredits": {
            "total": tokensToCredits(monthlyTotal, TOKEN_TO_CREDIT_RATIO),
            "used": tokensToCredits(monthlyUsed, TOKEN_TO_CREDIT_RATIO),
            "percentageUsed": (
                round((monthlyUsed / monthlyTotal) * 100, 2)
                if monthlyTotal > 0
                else 0.0
            ),
        },
        "topupCredits": {
            "total": tokensToCredits(topupTotal, TOKEN_TO_CREDIT_RATIO),
            "used": tokensToCredits(topupUsed, TOKEN_TO_CREDIT_RATIO),
        },
    }

    if includeTokens:
        view.update({
            "monthlyTokens": {"total": monthlyTotal, "used": monthlyUsed},
            "topupTokens": {"total": topupTotal, "used": topupUsed},
        })

    return view


def buildProfileCreditView(snapshot: dict) -> dict:
    """Build the trimmed credits-only view embedded in the user profile."""
    return {
        **buildCreditView(snapshot),
        "periodEnd": snapshot.get("periodEnd"),
        "initialized": bool(snapshot.get("initialized", False)),
        **{key:snapshot[key] for key in ('nextRefillAt','storedTopupTokens','spendableTopupTokens','spendableTokens','accessAllowed') if key in snapshot},
    }


def buildCreditBalanceView(snapshot: dict) -> dict:
    """Build the full public balance shared by balance and usage endpoints."""
    return {
        "planTier": snapshot.get("planTier", "none"),
        **buildCreditView(snapshot, includeTokens=True),
        "periodStart": snapshot.get("periodStart"),
        "periodEnd": snapshot.get("periodEnd"),
        "lastResetAt": snapshot.get("lastResetAt"),
        "initialized": bool(snapshot.get("initialized", False)),
        **{key:snapshot[key] for key in ('nextRefillAt','storedTopupTokens','spendableTopupTokens','spendableTokens','accessAllowed') if key in snapshot},
    }
