"""
creditTrackingCallback.py

LangChain BaseCallbackHandler that extracts token usage from LLM responses
and deducts it from the user's monthly balance via creditService.  Composed
alongside the Langfuse callback in the ``callbacks`` list passed to chain
invocations.

Tokens are charged exactly as reported — there is no per-call rounding, so a
chain of six small calls costs the sum of its six token counts.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["CreditTrackingCallback"]


from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from utils.logger import logger
from typing import Any
import uuid


class CreditTrackingCallback(BaseCallbackHandler):
    """
    Post-LLM-call callback that reads ``usage_metadata`` from the
    model response and settles it against the ADMITTED credit period.

    The admission context (operation ID + credit period) is captured at work
    start; a delayed ``on_llm_end`` after an expiry/refund/activation
    boundary settles against that original period exactly once and never
    debits a newly refilled period.

    Usage::

        creditCb = CreditTrackingCallback(userId=user.userId, operationType="reporting_query")
        config = {"callbacks": [langfuseHandler, creditCb]}
        response = workflow.invoke(inputs, config=config)
    """

    def __init__(self, userId: str, operationType: str, operationId: str | None = None,
                 creditPeriodId: str | None = None, lifecycleId: str | None = None):
        super().__init__()
        self.userId = userId
        self.operationType = operationType
        self.operationId = operationId or f"llm:{uuid.uuid4()}"
        self.creditPeriodId = creditPeriodId
        self.lifecycleId = lifecycleId
        self._context = None
        self._admit()

    def _admit(self) -> None:
        """Persist the admission context before counted work starts.

        Admission failures are logged, not raised: the LLM call itself is
        already authorized by the request-time gate, and a missing context
        falls back to the legacy immediate-deduct path below.
        """
        try:
            from api.services.credits.creditOperationSettlement import (
                CreditOperationSettlement,
            )

            settlement = CreditOperationSettlement()
            self._context = settlement.admitCreditOperation(
                userId=self.userId,
                operationType=self.operationType,
                operationId=self.operationId,
                lifecycleId=self.lifecycleId or "unknown",
                creditPeriodId=self.creditPeriodId,
            )
        except Exception as admitError:
            logger.warning(
                f"Credit operation admission failed — userId={self.userId}, "
                f"op={self.operationType}: {admitError}"
            )
            self._context = None

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """
        Called after every LLM call. Extracts token counts and settles the
        real usage once against the originally admitted credit period.
        """
        try:
            totalTokens = 0

            for generations in response.generations:
                for gen in generations:
                    msg = gen.message if hasattr(gen, "message") else None
                    if msg is None:
                        continue

                    usage = getattr(msg, "usage_metadata", None)
                    if usage and isinstance(usage, dict):
                        totalTokens += usage.get("total_tokens", 0)
                    elif hasattr(msg, "response_metadata"):
                        meta = msg.response_metadata or {}
                        tokenUsage = meta.get("token_usage") or meta.get("usage", {})
                        if isinstance(tokenUsage, dict):
                            totalTokens += tokenUsage.get("total_tokens", 0)

            if totalTokens > 0 and self._context is not None:
                from api.services.credits.creditOperationSettlement import (
                    CreditOperationSettlement,
                )

                settlement = CreditOperationSettlement()
                settlement.settleCreditOperation(
                    context=self._context,
                    tokensUsed=totalTokens,
                )
            elif totalTokens > 0:
                # No admission context (legacy callers): immediate deduct.
                from api.services.credits.creditService import creditService
                creditService.deductTokens(
                    userId=self.userId,
                    tokensUsed=totalTokens,
                    operationType=self.operationType,
                )
        except Exception as e:
            logger.warning(
                f"CreditTrackingCallback.on_llm_end failed — "
                f"userId={self.userId}, op={self.operationType}: {e}"
            )
