"""
creditService.py

Core credit management for the single-bucket monthly token system.

Redis holds a per-user HASH at credits:v3:{userId} with fields trem, ttop,
tquota, pend, pnext. Two Lua scripts perform the atomic hot-path operations
(peek + deduct) and apply the monthly roll inline, so the first request after a
billing boundary already sees a full quota. Python then repairs the exact
calendar boundary and persists the new period. Supabase credit_balances is the
durable source of truth, rebuilt into Redis on cache miss.

There are two buckets. `trem` is the monthly allowance, restored by the roll.
`ttop` holds purchased top-up tokens and is never touched by the roll, so it
carries over indefinitely. Deductions draw down `trem` first and spill onto
`ttop` only once it is empty.

_ROLL_FALLBACK_SECONDS is the 31-day safety bound the Lua roll adds to pnext.
Python replaces it with the exact calendar value immediately after, but if that
write never lands the boundary is still monotonic and the loop terminates.
_ROLL_MAX_ITERATIONS caps the roll at 10 years of missed periods.

Tokens are the unit of record everywhere; credits are derived for display only.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = [
    "CreditService",
    "creditService",
    "calculateQuotaResizeTargets",
    "preserveDurableUsedTokens",
]


from api.services.credits.creditConfig import (
    TOKEN_TO_CREDIT_RATIO,
    getTokenQuotaForPlan,
)
from api.services.credits.creditPresentation import deriveActiveTopupWindow
from api.services.credits import creditMath
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from dateutil import parser as dateparser
from utils.logger import logger
import redis
import os


_ROLL_FALLBACK_SECONDS = 2678400

_ROLL_MAX_ITERATIONS = 120

# Subscription statuses that still entitle a user to their domain_count's
# credit allowance. Excludes 'none' (never subscribed), 'cancelled', and
# 'expired' — a lapsed subscription must not keep driving the quota upward.
_ACTIVE_LIKE_SUBSCRIPTION_STATUSES = (
    "trial", "active", "renewal_upcoming", "payment_pending", "past_due", "suspended",
)

_PEEK_LUA = """
local h = redis.call('HGETALL', KEYS[1])
if #h == 0 then return {-1, -1, -1, -1} end
local m = {}
for i = 1, #h, 2 do m[h[i]] = h[i+1] end
local trem = tonumber(m['trem'])
local ttop = tonumber(m['ttop']) or 0
local tquota = tonumber(m['tquota'])
local pend = tonumber(m['pend'])
local pnext = tonumber(m['pnext'])
local now = tonumber(ARGV[1])
local fallback = tonumber(ARGV[2])
local maxIter = tonumber(ARGV[3])
local rolled = 0
local guard = 0
while now >= pend and guard < maxIter do
  trem = tquota
  pend = pnext
  pnext = pnext + fallback
  rolled = 1
  guard = guard + 1
end
if trem < 0 then trem = 0 end
if ttop < 0 then ttop = 0 end
if rolled == 1 then
  redis.call('HSET', KEYS[1], 'trem', trem, 'pend', pend, 'pnext', pnext)
end
return {trem, ttop, rolled, pend}
"""

_DEDUCT_LUA = """
local h = redis.call('HGETALL', KEYS[1])
if #h == 0 then return {-1, -1, -1, -1} end
local m = {}
for i = 1, #h, 2 do m[h[i]] = h[i+1] end
local trem = tonumber(m['trem'])
local ttop = tonumber(m['ttop']) or 0
local tquota = tonumber(m['tquota'])
local pend = tonumber(m['pend'])
local pnext = tonumber(m['pnext'])
local n = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local fallback = tonumber(ARGV[3])
local maxIter = tonumber(ARGV[4])
local rolled = 0
local guard = 0
while now >= pend and guard < maxIter do
  trem = tquota
  pend = pnext
  pnext = pnext + fallback
  rolled = 1
  guard = guard + 1
end
local spill = 0
trem = trem - n
if trem < 0 then
  spill = -trem
  if spill > ttop then spill = ttop end
  ttop = ttop - spill
  trem = 0
end
if ttop < 0 then ttop = 0 end
redis.call('HSET', KEYS[1], 'trem', trem, 'ttop', ttop, 'pend', pend, 'pnext', pnext)
return {trem, ttop, spill, rolled}
"""

_RESIZE_LUA = """
local h = redis.call('HGETALL', KEYS[1])
if #h == 0 then return {-1, -1, -1, -1} end
local m = {}
for i = 1, #h, 2 do m[h[i]] = h[i+1] end
local trem = tonumber(m['trem'])
local tquota = tonumber(m['tquota'])
local pend = tonumber(m['pend'])
local pnext = tonumber(m['pnext'])
local newQuota = tonumber(ARGV[1])
local grantImmediately = tonumber(ARGV[2])
local usedFloor = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local fallback = tonumber(ARGV[5])
local maxIter = tonumber(ARGV[6])
local rolled = 0
local guard = 0
while now >= pend and guard < maxIter do
  trem = tquota
  pend = pnext
  pnext = pnext + fallback
  rolled = 1
  guard = guard + 1
end
if trem < 0 then trem = 0 end
if tquota < 0 then tquota = 0 end
if newQuota < 0 then newQuota = 0 end
local oldUsed = tquota - trem
if oldUsed < 0 then oldUsed = 0 end
if rolled == 0 and usedFloor > oldUsed then oldUsed = usedFloor end
local newRemaining
local used
if newQuota > tquota and grantImmediately == 0 then
  newRemaining = math.min(trem, newQuota)
  used = math.max(oldUsed, newQuota - newRemaining)
else
  newRemaining = math.max(0, newQuota - oldUsed)
  used = oldUsed
end
redis.call(
  'HSET', KEYS[1],
  'tquota', newQuota,
  'trem', newRemaining,
  'pend', pend,
  'pnext', pnext
)
return {newRemaining, used, rolled, tquota}
"""

_TRIAL_REFRESH_LUA = """
local existingTopup = redis.call('HGET', KEYS[1], 'ttop')
local existingGeneration = tonumber(redis.call('HGET', KEYS[1], 'tgen')) or -1
local incomingGeneration = tonumber(ARGV[5])
if incomingGeneration < existingGeneration then return 0 end
local topup = existingTopup
if not topup then topup = ARGV[2] end
redis.call(
  'HSET', KEYS[1],
  'trem', ARGV[1],
  'ttop', topup,
  'tquota', ARGV[1],
  'pend', ARGV[3],
  'pnext', ARGV[4],
  'tgen', incomingGeneration
)
return 1
"""

_redis_pool: redis.ConnectionPool | None = None


def calculateQuotaResizeTargets(oldQuota: int, currentRemaining: int,
                                newQuota: int, grantImmediately: bool,
                                usedFloor: int = 0) -> dict:
    """Calculate resize targets while preserving already-consumed usage."""
    oldQuota = max(0, int(oldQuota or 0))
    currentRemaining = max(0, int(currentRemaining or 0))
    newQuota = max(0, int(newQuota or 0))
    oldUsed = max(0, oldQuota - currentRemaining, int(usedFloor or 0))

    if newQuota > oldQuota and not grantImmediately:
        remaining = min(currentRemaining, newQuota)
        used = max(oldUsed, newQuota - remaining)
    else:
        remaining = max(0, newQuota - oldUsed)
        used = oldUsed

    return {"remaining": remaining, "used": used}


def preserveDurableUsedTokens(storedUsed: int, quota: int,
                              remaining: int, periodRolled: bool = False) -> int:
    """Never let a Redis write-back forgive monthly usage stored durably."""
    return max(
        0,
        0 if periodRolled else int(storedUsed or 0),
        int(quota or 0) - int(remaining or 0),
    )


class CreditService:
    """Per-user monthly token balances backed by Redis + Supabase."""

    def __init__(self):
        self._supabase = None

    @property
    def supabase(self):
        """Lazily acquire the shared Supabase client (avoids import-time coupling)."""
        if self._supabase is None:
            from api.commons import client
            self._supabase = client
        return self._supabase

    @supabase.setter
    def supabase(self, value):
        self._supabase = value

    def _redis(self) -> redis.Redis:
        global _redis_pool
        if _redis_pool is None:
            try:
                socketTimeout = max(
                    0.1,
                    float(os.environ.get("REDIS_SOCKET_TIMEOUT_SECONDS", "3")),
                )
            except (TypeError, ValueError):
                socketTimeout = 3.0
            _redis_pool = redis.ConnectionPool(
                host=os.environ["REDIS_HOST"],
                port=int(os.environ["REDIS_PORT"]),
                password=os.environ["REDIS_PASSWORD"],
                socket_connect_timeout=socketTimeout,
                socket_timeout=socketTimeout,
            )
        return redis.Redis(connection_pool=_redis_pool)

    @staticmethod
    def _redisKey(userId: str) -> str:
        return f"credits:v3:{userId}"

    def invalidateCreditProjection(self, userId: str) -> bool:
        """Delete one user's Redis projection after a committed SQL change.

        Only eviction: a delayed call can drop a newer projection, which then
        rebuilds from SQL, but it never publishes a historical snapshot.
        Returns False on failure without touching the committed SQL result.
        """
        try:
            self._redis().delete(self._redisKey(userId))
            return True
        except Exception as e:
            logger.warning(f"Credit projection invalidation failed: {type(e).__name__}")
            return False

    # ---- durable read helpers -------------------------------------------------

    def _dbRow(self, userId: str) -> dict | None:
        row = (
            self.supabase.table("credit_balances")
            .select(
                "plan_tier, monthly_token_quota, used_tokens, remaining_tokens, "
                "topup_tokens, domain_count, period_start, period_end, last_reset_at, "
                "subscription_id, lifecycle_id, credit_period_id, balance_version"
            )
            .eq("user_id", userId)
            .limit(1)
            .execute()
        )
        return row.data[0] if row.data else None

    def _getPaidTopupLots(self, userId: str) -> list[int]:
        """Return paid top-up lot sizes in newest-first purchase order."""
        try:
            result = (
                self.supabase.table("Invoices")
                .select("status, pricing_reference_snapshot_json, paidAt")
                .eq("userId", userId)
                .eq("billing_reason", "add_on")
                .order("paidAt", desc=True)
                .execute()
            )
            lots = []
            for invoice in result.data or []:
                if str(invoice.get("status", "")).lower() != "paid":
                    continue
                snapshot = invoice.get("pricing_reference_snapshot_json")
                if not isinstance(snapshot, dict):
                    continue
                try:
                    tokens = int(snapshot.get("tokens", 0))
                except (TypeError, ValueError):
                    continue
                if tokens > 0:
                    lots.append(tokens)
            return lots
        except Exception as e:
            logger.warning(f"Top-up invoice history read failed for userId={userId}: {e}")
            return []

    # ---- Redis hash lifecycle -------------------------------------------------

    def _manualBalance(self,userId):
        # One PostgreSQL authority for all modes; Redis never owns shared top-ups.
        from api.services.credits.manualCreditRepository import ManualCreditRepository
        return ManualCreditRepository().balanceSnapshot(userId)

    def _ensureHash(self, userId: str) -> dict | None:
        """
        Guarantee the Redis hash exists for the user.

        Rebuilds from the DB on cache miss, applying the monthly roll from the
        stored period_end so an evicted key never resurrects a stale balance.
        Boundary crossings on a live hash are handled by the Lua roll, not here.
        Returns the DB row used, or None when the hash is already present or no
        row exists.
        """
        now = datetime.now(timezone.utc)
        try:
            r = self._redis()
            if r.hget(self._redisKey(userId), "pend") is not None:
                return None
        except Exception as e:
            logger.warning(f"Redis unavailable in _ensureHash for {userId}: {e}")
            r = None

        dbRow = self._dbRow(userId)
        if not dbRow:
            return None

        tquota = dbRow.get("monthly_token_quota", 0)
        trem = dbRow.get("remaining_tokens", 0)
        ttop = dbRow.get("topup_tokens", 0) or 0
        periodEnd = dbRow.get("period_end")

        pe = dateparser.parse(periodEnd) if periodEnd else now
        if pe <= now:
            ps2, pe2 = creditMath.rollMonthly(pe, now)
            trem = tquota
            self._writePeriod(userId, ps2, pe2, tquota, now)
            pe = pe2

        if r is not None:
            try:
                r.hset(self._redisKey(userId), mapping={
                    "trem": trem,
                    "ttop": ttop,
                    "tquota": tquota,
                    "pend": int(pe.timestamp()),
                    "pnext": int(creditMath.nextPeriodEnd(pe).timestamp()),
                })
            except Exception as e:
                logger.warning(f"Redis hash rebuild failed for {userId}: {e}")
        return dbRow

    def _repairPeriod(self, userId: str, now: datetime) -> None:
        """
        Follow-up to a Lua roll: replace the 31-day safety bound on `pnext` with
        the exact calendar boundary and persist the new period to Supabase.

        Best-effort. `pend` has already advanced inside Redis, so a failure here
        cannot cause a double roll — the next request repairs it instead. A
        stored period_end at or past the new one means another request already
        persisted this roll, and nothing is written.
        """
        try:
            r = self._redis()
            key = self._redisKey(userId)
            pendRaw, quotaRaw = r.hmget(key, "pend", "tquota")
            if pendRaw is None:
                return
            periodEnd = datetime.fromtimestamp(float(pendRaw), tz=timezone.utc)
            r.hset(key, "pnext", int(creditMath.nextPeriodEnd(periodEnd).timestamp()))
            tquota = int(quotaRaw) if quotaRaw is not None else 0
        except Exception as e:
            logger.warning(f"Redis period repair failed for {userId}: {e}")
            return

        try:
            row = self._dbRow(userId)
            storedEnd = dateparser.parse(row["period_end"]) if row and row.get("period_end") else None
            if storedEnd is not None and storedEnd >= periodEnd:
                return
            periodStart = storedEnd if storedEnd is not None else periodEnd - relativedelta(months=1)
            self._writePeriod(userId, periodStart, periodEnd, tquota, now)
            logger.info(
                f"Monthly token quota rolled — userId={userId}, quota={tquota}, "
                f"periodEnd={periodEnd.isoformat()}"
            )
        except Exception as e:
            logger.warning(f"DB period repair failed for {userId}: {e}")

    def _writePeriod(self, userId, periodStart, periodEnd, tquota, now) -> None:
        try:
            self.supabase.table("credit_balances").update({
                "used_tokens": 0,
                "remaining_tokens": tquota,
                "period_start": periodStart.isoformat(),
                "period_end": periodEnd.isoformat(),
                "last_reset_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }).eq("user_id", userId).execute()
        except Exception as e:
            logger.warning(f"DB period write failed for {userId}: {e}")

    # ---- Lua seams (patched in unit tests) ------------------------------------

    def _peek(self, userId: str) -> dict | None:
        """
        Atomic monthly roll + both-bucket read via _PEEK_LUA.

        The script rolls the billing period inline for every boundary `now` has
        passed, then reports both buckets. Its arguments are positional and
        untyped, so the order below is the only contract it has:
        KEYS[1]=hash, ARGV[1]=nowEpoch, ARGV[2]=fallbackSeconds,
        ARGV[3]=maxIterations. It returns the flat array {trem, ttop, rolled},
        or {-1,-1,-1} when the hash is absent.

        Args:
            userId (str): The user whose hash to read.

        Returns:
            dict | None: {trem, ttop, rolled}, or None when the hash is absent
                or Redis is unreachable.
        """
        try:
            r = self._redis()
            now = int(datetime.now(timezone.utc).timestamp())
            res = r.eval(
                _PEEK_LUA, 1, self._redisKey(userId),
                now, _ROLL_FALLBACK_SECONDS, _ROLL_MAX_ITERATIONS,
            )
            if not res or int(res[0]) == -1:
                return None
            return {
                "trem": int(res[0]),
                "ttop": int(res[1]),
                "rolled": int(res[2]),
                "periodEndEpoch": int(res[3]),
            }
        except Exception as e:
            logger.warning(f"Redis peek failed for {userId}: {e}")
            return None

    def _deduct(self, userId: str, n: int) -> dict | None:
        """
        Atomic monthly roll + token decrement across both buckets via _DEDUCT_LUA.

        The script applies the same roll as _PEEK_LUA, then subtracts n tokens:
        the monthly bucket first, spilling the remainder onto the purchased
        bucket. Both clamp at 0. Its arguments are positional and untyped, so
        the order below is the only contract it has — swapping n and nowEpoch
        raises nothing and would charge an epoch timestamp's worth of tokens:
        KEYS[1]=hash, ARGV[1]=n, ARGV[2]=nowEpoch, ARGV[3]=fallbackSeconds,
        ARGV[4]=maxIterations. It returns the flat array
        {trem, ttop, spill, rolled}, or {-1,-1,-1,-1} when the hash is absent.

        Args:
            userId (str): The user whose balance to charge.
            n (int): Tokens to subtract.

        Returns:
            dict | None: {trem, ttop, spill, rolled}, where `spill` is the
                number of tokens taken from the purchased bucket on this call.
                None when the hash is absent or Redis is unreachable.
        """
        try:
            r = self._redis()
            now = int(datetime.now(timezone.utc).timestamp())
            res = r.eval(
                _DEDUCT_LUA, 1, self._redisKey(userId),
                n, now, _ROLL_FALLBACK_SECONDS, _ROLL_MAX_ITERATIONS,
            )
            if not res or int(res[0]) == -1:
                return None
            return {
                "trem": int(res[0]), "ttop": int(res[1]),
                "spill": int(res[2]), "rolled": int(res[3]),
            }
        except Exception as e:
            logger.warning(f"Redis deduct failed for {userId}: {e}")
            return None

    def _resizeQuota(self, userId: str, newQuota: int,
                     grantImmediately: bool, usedFloor: int = 0) -> dict | None:
        """Atomically roll the period and resize the monthly Redis bucket."""
        try:
            r = self._redis()
            now = int(datetime.now(timezone.utc).timestamp())
            res = r.eval(
                _RESIZE_LUA, 1, self._redisKey(userId),
                newQuota, int(grantImmediately), usedFloor, now,
                _ROLL_FALLBACK_SECONDS, _ROLL_MAX_ITERATIONS,
            )
            if not res or int(res[0]) == -1:
                return None
            return {
                "trem": int(res[0]),
                "used": int(res[1]),
                "rolled": int(res[2]),
                "oldQuota": int(res[3]),
            }
        except Exception as e:
            logger.warning(f"Redis quota resize failed for {userId}: {e}")
            return None

    # ---- public API -----------------------------------------------------------

    def admitCreditOperation(self,userId,operationType,operationId):
        from api.services.credits.manualCreditRepository import ManualCreditRepository
        return ManualCreditRepository().admit(userId,operationType,operationId)

    def settleCreditOperation(self,context,tokensUsed,runId):
        from api.services.credits.manualCreditRepository import ManualCreditRepository
        repository=ManualCreditRepository()
        try:
            repository.reportUsage(context,tokensUsed,str(runId))
        except ValueError:
            raise
        except Exception:
            from api.services.credits.creditUsageSpool import retainUsage
            retainUsage(context,tokensUsed,str(runId))
            raise
        return repository.settle(context,tokensUsed,str(runId))

    def refreshTrialCreditsCache(
        self, userId: str, quota: int, topupTokens: int, periodEnd,
        generation: int,
    ) -> str:
        """Atomically publish a durable admin trial-credit reset to Redis."""
        try:
            parsedEnd = (
                periodEnd
                if isinstance(periodEnd, datetime)
                else dateparser.parse(str(periodEnd))
            )
            applied = self._redis().eval(
                _TRIAL_REFRESH_LUA,
                1,
                self._redisKey(userId),
                max(0, int(quota)),
                max(0, int(topupTokens or 0)),
                int(parsedEnd.timestamp()),
                int(creditMath.nextPeriodEnd(parsedEnd).timestamp()),
                max(0, int(generation)),
            )
            return "APPLIED" if int(applied or 0) == 1 else "STALE"
        except Exception as e:
            logger.warning(f"Admin trial credit cache refresh failed for {userId}: {e}")
            return "FAILED"

    def initializeCreditBalance(self,userId,planTier,subscriptionId=None,domainCount=1):
        from api.services.credits.manualCreditRepository import ManualCreditRepository
        balance=ManualCreditRepository().balanceSnapshot(userId)
        if balance.get('plan_tier')!=planTier or (subscriptionId and str(balance.get('subscription_id'))!=str(subscriptionId)):
            raise ValueError('CREDIT_INITIALIZER_MODE_OR_OWNER_MISMATCH')
        # Reading a committed allocation never resets usage or purchased credits.
        return balance

    def applyDomainCountChange(self, userId: str, domainCount: int,
                               grantImmediately: bool) -> dict:
        """
        Resize the monthly allowance after a domain is added or removed.

        The allowance is 1,000 credits per subscribed domain, held as one
        pooled balance. `grantImmediately` distinguishes the two callers:
        an activation makes the new domain's allowance spendable at once,
        while a removal at the period boundary only lowers the ceiling and
        clamps what is left underneath it.

        Resize and monthly roll are one Redis Lua operation. This prevents a
        concurrent deduction from being overwritten by a later absolute HSET.
        A reduction preserves the amount already consumed, even when usage is
        above the new ceiling; in that case remaining becomes zero and
        used_tokens legitimately remains greater than monthly_token_quota.

        The purchased bucket (`ttop`) is never touched — it was paid for
        separately and does not belong to any domain.

        `domainCount` is clamped to a minimum of 1 before it is used for the
        quota computation or persisted, so a caller passing 0 (e.g. a user
        removing their last domain) cannot desync the stored count from the
        floored quota `getTokenQuotaForPlan` would have returned anyway.

        Redis is the atomic hot-path authority. If it cannot be resized, the
        durable row is left unchanged. A later reconciliation retries any
        durable write that fails after the atomic Redis resize.

        Args:
            userId (str): The user whose allowance to resize.
            domainCount (int): The new number of subscribed domains. Clamped
                to a minimum of 1.
            grantImmediately (bool): Whether an increase should also raise the
                spendable remainder, rather than only the ceiling.

        Returns:
            dict: {"applied": bool, "quota": int, "delta": int, "remaining": int}.
        """
        if self._manualBalance(userId) is not None:
            from api.services.credits.manualCreditRepository import ManualCreditRepository
            return ManualCreditRepository().resizeQuota(userId,domainCount,grantImmediately)
        try:
            domainCount = max(1, int(domainCount))
        except (TypeError, ValueError):
            domainCount = 1

        row = self._dbRow(userId)
        if not row:
            logger.warning(
                f"No credit_balances row for userId={userId}, "
                f"skipping domain count change to {domainCount}"
            )
            return {"applied": False, "quota": 0, "delta": 0, "remaining": 0}

        planTier = row.get("plan_tier", "none")
        oldQuota = row.get("monthly_token_quota", 0) or 0
        newQuota = getTokenQuotaForPlan(planTier, domainCount)
        delta = newQuota - oldQuota

        self._ensureHash(userId)
        usedFloor = row.get("used_tokens", 0) or 0
        state = self._resizeQuota(
            userId, newQuota, grantImmediately, usedFloor=usedFloor
        )
        if state is None:
            self._ensureHash(userId)
            state = self._resizeQuota(
                userId, newQuota, grantImmediately, usedFloor=usedFloor
            )
        if state is None:
            currentRemaining = row.get("remaining_tokens", 0) or 0
            return {"applied": False, "quota": oldQuota, "delta": 0,
                    "remaining": currentRemaining}

        newRemaining = state["trem"]
        usedTokens = state["used"]

        now = datetime.now(timezone.utc)
        if state["rolled"]:
            self._repairPeriod(userId, now)
        try:
            self.supabase.table("credit_balances").update({
                "monthly_token_quota": newQuota,
                "domain_count": domainCount,
                "remaining_tokens": newRemaining,
                "used_tokens": usedTokens,
                "updated_at": now.isoformat(),
            }).eq("user_id", userId).execute()
        except Exception as e:
            logger.error(
                f"Domain count quota write failed for userId={userId}: {e}"
            )
            return {"applied": False, "quota": oldQuota, "delta": 0,
                    "remaining": newRemaining}

        logger.info(
            f"Domain count applied — userId={userId}, domains={domainCount}, "
            f"quota={oldQuota}->{newQuota}, remaining={newRemaining}, "
            f"granted={grantImmediately}"
        )
        return {"applied": True, "quota": newQuota, "delta": delta,
                "remaining": newRemaining}

    def deductTokens(self, userId, tokensUsed, operationType, context=None, runId=None) -> int:
        """
        Subtract the exact token count from the user's balance.

        No rounding: the tokens reported by the LLM response are the tokens
        charged. The monthly bucket is drawn down first, spilling onto the
        purchased bucket once it is empty. The spill is mirrored to Supabase as
        a relative decrement, because an absolute write would race with a
        concurrent grant and lose the purchase.

        On a Redis miss the hash is rebuilt and the deduction retried once,
        after which the balance is written back durably to Supabase.

        Returns the total remaining tokens across both buckets, or -1 when the
        balance is unreachable (the call is then not charged).
        """
        if tokensUsed <= 0:
            return self.getRemainingTokens(userId)

        if self._manualBalance(userId) is not None:
            if context is None or runId is None or context.userId!=userId or context.operationType!=operationType:
                raise ValueError('CREDIT_PREWORK_ADMISSION_REQUIRED')
            self.settleCreditOperation(context,tokensUsed,runId)
            return self.getRemainingTokens(userId)

        self._ensureHash(userId)
        state = self._deduct(userId, tokensUsed)
        if state is None:
            self._ensureHash(userId)
            state = self._deduct(userId, tokensUsed)
        if state is None:
            logger.warning(f"Token deduction unavailable for {userId}")
            return -1

        now = datetime.now(timezone.utc)
        if state["rolled"]:
            self._repairPeriod(userId, now)

        if state.get("spill", 0) > 0:
            try:
                self.supabase.rpc("decrement_topup_tokens", {
                    "p_user_id": userId,
                    "p_tokens": state["spill"],
                }).execute()
            except Exception as e:
                logger.warning(f"Top-up decrement write-back failed for {userId}: {e}")

        logger.info(
            f"Tokens deducted — userId={userId}, tokens={tokensUsed}, "
            f"remaining={state['trem']}, topup={state['ttop']}, "
            f"spill={state.get('spill', 0)}, op={operationType}"
        )

        try:
            row = self._dbRow(userId)
            tquota = row.get("monthly_token_quota", 0) if row else 0
            storedUsed = row.get("used_tokens", 0) if row else 0
            self.supabase.table("credit_balances").update({
                "remaining_tokens": state["trem"],
                "used_tokens": preserveDurableUsedTokens(
                    storedUsed, tquota, state["trem"],
                    periodRolled=bool(state["rolled"]),
                ),
                "updated_at": now.isoformat(),
            }).eq("user_id", userId).execute()
        except Exception as e:
            logger.warning(f"DB token write-back failed for {userId}: {e}")

        return state["trem"] + state["ttop"]

    def getRemainingParts(self, userId: str) -> dict:
        """
        Remaining tokens split by bucket: {"monthly": int, "topup": int}.

        Falls back to Supabase when Redis is down or cold, applying the lazy
        roll in Python. That fallback includes the purchased balance, so a Redis
        outage cannot lock out the users who paid not to be locked out.

        `monthly` is -1 when the balance is unreadable. Protected work treats
        that state as a retryable failure and cannot use the cache to authorize it.
        """
        manual = self._manualBalance(userId)
        if manual is not None:
            return {"monthly": int(manual.get("remaining_tokens") or 0),
                    "topup": int(manual.get("topup_tokens") or 0), "rolled": False}
        self._ensureHash(userId)
        state = self._peek(userId)
        if state is not None:
            if state["rolled"]:
                self._repairPeriod(userId, datetime.now(timezone.utc))
            return {
                "monthly": state["trem"],
                "topup": state["ttop"],
                "rolled": bool(state["rolled"]),
            }

        try:
            row = self._dbRow(userId)
            if not row:
                return {"monthly": -1, "topup": 0}
            now = datetime.now(timezone.utc)
            topup = row.get("topup_tokens", 0) or 0
            periodEnd = row.get("period_end")
            if periodEnd and dateparser.parse(periodEnd) <= now:
                return {
                    "monthly": row.get("monthly_token_quota", 0),
                    "topup": topup,
                    "rolled": True,
                }
            return {
                "monthly": row.get("remaining_tokens", 0),
                "topup": topup,
                "rolled": False,
            }
        except Exception as e:
            logger.warning(f"DB token read failed for {userId}: {e}")
            return {"monthly": -1, "topup": 0}

    def getRemainingTokens(self, userId: str) -> int:
        """
        Total spendable tokens: monthly remainder plus purchased balance.

        Returns -1 when the authoritative balance is unreadable, which
        requireCredits translates to a retryable HTTP 503.
        """
        try:
            parts = self.getRemainingParts(userId)
        except Exception:
            logger.warning('Authoritative credit balance unavailable')
            return -1
        if parts["monthly"] == -1:
            return -1
        return parts["monthly"] + parts["topup"]

    def grantTopupTokens(self, userId: str, orderId: str, paymentId: str) -> dict:
        """Recovery alias; the owned durable attempt is the only grant authority."""
        from api.services.billing.manualBillingRepository import getManualBillingRepository
        from api.services.subscriptions.subscriptionService import subscriptionService
        repository = getManualBillingRepository()
        attempt = repository.attemptForOrder(orderId)
        frozen = repository._json(attempt['metadata_json'])['manualBilling']
        if attempt['user_id'] != userId or frozen['purpose'] != 'topup':
            raise ValueError('TOPUP_OWNER_OR_PURPOSE_MISMATCH')
        payment = subscriptionService.razorpayClient.payment.fetch(paymentId)
        result = subscriptionService._finalizeManualCheckout(str(attempt['invoice_id']), orderId, paymentId, payment)
        return {'granted': result['finalized'] and not result['alreadyFinalized'],
            'tokens': int(frozen['tokens']) if result['finalized'] else 0,
            'disposition': result['state'], 'anomalyId': result['anomalyId']}

    def clawbackTopupTokens(self,userId,refundId,paymentId,refundAmount):
        from api.services.credits.manualCreditRepository import ManualCreditRepository
        return ManualCreditRepository().clawbackTopup(userId,refundId,paymentId,int(refundAmount))

    def reconcile(self, userId: str) -> None:
        """
        Safety-net sync of the Redis hash back to Supabase (guards eviction/drift).

        Covers the monthly bucket only. topup_tokens is maintained by relative
        writes on both sides, so an absolute sync here would race with an
        in-flight grant and lose a purchase.
        """
        if self._manualBalance(userId) is not None:
            return
        try:
            self.syncQuotaFromConfig(userId)

            state = self._peek(userId)
            row = self._dbRow(userId)
            if not row:
                return
            if state is None:
                self._ensureHash(userId)
                return
            tquota = row.get("monthly_token_quota", 0)
            now = datetime.now(timezone.utc)
            periodStart = None
            periodEnd = None
            if state["rolled"] and state.get("periodEndEpoch") is not None:
                periodEnd = datetime.fromtimestamp(
                    state["periodEndEpoch"], tz=timezone.utc
                )
                storedEnd = (
                    dateparser.parse(row["period_end"])
                    if row.get("period_end")
                    else None
                )
                periodStart = storedEnd or periodEnd - relativedelta(months=1)
                try:
                    self._redis().hset(
                        self._redisKey(userId),
                        "pnext",
                        int(creditMath.nextPeriodEnd(periodEnd).timestamp()),
                    )
                except Exception as e:
                    logger.warning(
                        f"Redis next-period repair failed for {userId}: {e}"
                    )
            usedTokens = preserveDurableUsedTokens(
                row.get("used_tokens", 0), tquota, state["trem"],
                periodRolled=bool(state["rolled"]),
            )
            self.supabase.rpc(
                "reconcile_credit_balance_if_no_admin_refresh",
                {
                    "p_user_id": userId,
                    "p_remaining_tokens": state["trem"],
                    "p_used_tokens": usedTokens,
                    "p_last_reconciled_at": now.isoformat(),
                    "p_period_start": (
                        periodStart.isoformat() if periodStart else None
                    ),
                    "p_period_end": periodEnd.isoformat() if periodEnd else None,
                },
            ).execute()
        except Exception as e:
            logger.warning(f"Credit reconciliation failed for userId={userId}: {e}")

    def getBalanceSnapshot(self, userId: str) -> dict:
        """
        Login/balance snapshot in tokens, with credits derived. Never raises.

        remainingTokens is the total across both buckets; usagePercentage covers
        the monthly bucket only, so a purchased balance cannot make the meter
        read under 100% when the plan's included allowance is spent.
        """
        defaults = {
            "planTier": "none", "monthlyTokenQuota": 0, "usedTokens": 0,
            "monthlyRemainingTokens": 0, "topupTokens": 0,
            "topupTotalTokens": 0, "topupUsedTokens": 0,
            "remainingTokens": 0, "monthlyCredits": 0.0, "usedCredits": 0.0,
            "topupCredits": 0.0, "remainingCredits": 0.0, "usagePercentage": 0.0,
            "periodStart": None, "periodEnd": None, "lastResetAt": None,
            "initialized": False,
            "nextRefillAt":None,"storedTopupTokens":None,"spendableTopupTokens":None,
            "spendableTokens":None,"accessAllowed":None,
        }
        try:
            authoritative = self._manualBalance(userId)
            row = authoritative if authoritative is not None else self._dbRow(userId)
            if not row:
                return defaults

            tquota = row.get("monthly_token_quota", 0)
            parts = self.getRemainingParts(userId)
            if parts["monthly"] == -1:
                monthlyRemaining = row.get("remaining_tokens", 0)
                topup = row.get("topup_tokens", 0) or 0
            else:
                monthlyRemaining = parts["monthly"]
                topup = parts["topup"]
                refreshedRow = self._dbRow(userId)
                if refreshedRow:
                    row = refreshedRow
                    tquota = row.get("monthly_token_quota", 0)
            topupWindow = (
                deriveActiveTopupWindow(topup, self._getPaidTopupLots(userId))
                if topup > 0
                else {"total": 0, "used": 0}
            )
            used = preserveDurableUsedTokens(
                row.get("used_tokens", 0), tquota, monthlyRemaining,
                periodRolled=bool(parts.get("rolled", False)),
            )
            remaining = monthlyRemaining + topup
            usagePercent = round((used / tquota) * 100, 2) if tquota > 0 else 0.0

            return {
                "planTier": row.get("plan_tier", "none"),
                "monthlyTokenQuota": tquota,
                "usedTokens": used,
                "monthlyRemainingTokens": monthlyRemaining,
                "topupTokens": topup,
                "topupTotalTokens": topupWindow["total"],
                "topupUsedTokens": topupWindow["used"],
                "remainingTokens": remaining,
                "monthlyCredits": creditMath.tokensToCredits(tquota, TOKEN_TO_CREDIT_RATIO),
                "usedCredits": creditMath.tokensToCredits(used, TOKEN_TO_CREDIT_RATIO),
                "topupCredits": creditMath.tokensToCredits(topup, TOKEN_TO_CREDIT_RATIO),
                "remainingCredits": creditMath.tokensToCredits(remaining, TOKEN_TO_CREDIT_RATIO),
                "usagePercentage": usagePercent,
                "periodStart": row.get("period_start"),
                "periodEnd": row.get("period_end"),
                "lastResetAt": row.get("last_reset_at"),
                "initialized": True,
                "nextRefillAt":authoritative.get('next_refill_at') if authoritative else None,
                "storedTopupTokens":topup,
                "spendableTopupTokens":authoritative.get('spendable_topup_tokens') if authoritative else None,
                "spendableTokens":monthlyRemaining + int(authoritative.get('spendable_topup_tokens') or 0) if authoritative else None,
                "accessAllowed":authoritative.get('access_allowed') if authoritative else None,
            }
        except Exception as e:
            logger.warning(f"getBalanceSnapshot failed for userId={userId}: {e}")
            return defaults

    def forceResetAllQuotas(self, resetUsage: bool = False) -> dict:
        """
        Admin-triggered bulk reset.

        Always: recompute monthly_token_quota from config and the stored
        domain_count for every credit_balances row, then flush all Redis
        credit hashes.

        When resetUsage=True: also zero used_tokens and restore remaining_tokens
        to the full quota — equivalent to giving every user a fresh monthly
        bucket mid-cycle. The billing period itself is left untouched, and so is
        topup_tokens: this runs over every user, so including it would destroy
        every purchase ever made in a single call.

        Returns:
            dict with updatedCount, redisDeletedCount, and mode.
        """
        try:
            rows = (
                self.supabase.table("credit_balances")
                .select("user_id, plan_tier, domain_count")
                .execute()
            )
            if not rows.data:
                return {"updatedCount": 0, "redisDeletedCount": 0, "mode": "none"}

            now = datetime.now(timezone.utc)
            updatedCount = 0
            for row in rows.data:
                userId = row["user_id"]
                if self._manualBalance(userId) is not None:
                    from api.services.credits.manualCreditRepository import ManualCreditRepository
                    result=ManualCreditRepository().resizeQuota(userId,row.get('domain_count') or 1,False,resetUsage)
                    updatedCount += int(result['applied'])
                    continue
                planTier = row.get("plan_tier", "none")
                domainCount = row.get("domain_count", 1) or 1
                newQuota = getTokenQuotaForPlan(planTier, domainCount)

                payload = {
                    "monthly_token_quota": newQuota,
                    "updated_at": now.isoformat(),
                }

                if resetUsage:
                    payload.update({
                        "used_tokens": 0,
                        "remaining_tokens": newQuota,
                        "last_reset_at": now.isoformat(),
                    })

                try:
                    self.supabase.table("credit_balances").update(
                        payload
                    ).eq("user_id", userId).execute()
                    updatedCount += 1
                except Exception as e:
                    logger.warning(f"Force quota update failed for userId={userId}: {e}")

            redisDeletedCount = 0
            try:
                r = self._redis()
                keys = list(r.scan_iter("credits:v3:*"))
                if keys:
                    redisDeletedCount = r.delete(*keys)
            except Exception as e:
                logger.warning(f"Redis bulk delete failed during force reset: {e}")

            mode = "quotaSync+usageReset" if resetUsage else "quotaSync"
            logger.info(
                f"Force quota reset complete — mode={mode}, "
                f"dbUpdated={updatedCount}, redisDeleted={redisDeletedCount}"
            )
            return {
                "updatedCount": updatedCount,
                "redisDeletedCount": int(redisDeletedCount),
                "mode": mode,
            }
        except Exception as e:
            logger.error(f"forceResetAllQuotas failed: {e}")
            raise

    def syncQuotaFromConfig(self, userId: str) -> None:
        """
        Ensure a single user's monthly_token_quota matches the current config.

        Also repairs domain_count drift against `subscriptions`, which is the
        canonical entitlement record — the credit row is a denormalised copy
        kept so quota reads and the monthly roll never need a join. Only rows
        in an active-like status (see _ACTIVE_LIKE_SUBSCRIPTION_STATUSES) are
        eligible to drive that repair: a cancelled or expired subscription
        must not keep inflating the quota. When no such row exists for the
        user, domain_count is left untouched rather than forced down to 1 —
        note that nothing currently sets plan_tier back to 'none' on
        cancellation either, which is a separate, already-escalated gap this
        function does not attempt to close.

        The same atomic resize used by domain lifecycle hooks repairs the drift.
        Lowering a quota preserves consumed usage; raising it does not grant new
        headroom because a missed grant is safer to investigate than an
        accidental double grant.

        Called during reconcile to prevent long-term drift.
        """
        if self._manualBalance(userId) is not None:
            from api.services.credits.manualCreditRepository import ManualCreditRepository
            ManualCreditRepository().resizeQuota(userId,None,False)
            return
        try:
            row = self._dbRow(userId)
            if not row:
                return
            planTier = row.get("plan_tier", "none")
            storedDomains = row.get("domain_count", 1) or 1

            canonicalDomains = storedDomains
            try:
                subRow = (
                    self.supabase.table("subscriptions")
                    .select("domain_count")
                    .eq("user_id", userId)
                    .eq("is_canonical", True)
                    .in_("status", _ACTIVE_LIKE_SUBSCRIPTION_STATUSES)
                    .limit(1)
                    .execute()
                )
                if subRow.data and subRow.data[0].get("domain_count") is not None:
                    canonicalDomains = max(1, int(subRow.data[0]["domain_count"]))
                else:
                    logger.debug(
                        f"No active-like subscription found for userId={userId}; "
                        f"leaving domain_count at {storedDomains}"
                    )
            except Exception as e:
                logger.warning(f"Domain count lookup failed for {userId}: {e}")

            expectedQuota = getTokenQuotaForPlan(planTier, canonicalDomains)
            currentQuota = row.get("monthly_token_quota", 0) or 0
            if currentQuota == expectedQuota and canonicalDomains == storedDomains:
                return

            if expectedQuota > currentQuota:
                logger.warning(
                    f"syncQuotaFromConfig is raising the ceiling for "
                    f"userId={userId}: {currentQuota} -> {expectedQuota}. The "
                    f"difference is NOT auto-granted; verify whether a grant "
                    f"was missed for this user."
                )

            result = self.applyDomainCountChange(
                userId,
                canonicalDomains,
                grantImmediately=False,
            )
            if not result["applied"]:
                logger.warning(
                    f"Quota drift repair deferred for userId={userId}: "
                    f"Redis resize was unavailable"
                )
                return
            logger.info(
                f"Quota drift corrected for userId={userId}: "
                f"{currentQuota} -> {expectedQuota} "
                f"(domains {storedDomains} -> {canonicalDomains})"
            )
        except Exception as e:
            logger.warning(f"syncQuotaFromConfig failed for userId={userId}: {e}")


creditService = CreditService()
