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
        """Every eligible mode needs durable admission before counted work."""
        from api.services.credits.creditService import creditService
        self._context = creditService.admitCreditOperation(self.userId,self.operationType,self.operationId)
        if ((self.creditPeriodId and self.creditPeriodId!=self._context.creditPeriodId)
                or (self.lifecycleId and self.lifecycleId!=self._context.lifecycleId)):
            raise ValueError('CREDIT_CONTEXT_MISMATCH')
        self.raise_error = True

    def on_llm_start(self, serialized, prompts, **kwargs):
        if kwargs.get('run_id') is None:
            raise ValueError('LLM_RUN_ID_REQUIRED_FOR_DURABLE_USAGE')
        self._admit()

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.on_llm_start(serialized,messages,**kwargs)

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
                runId = kwargs.get("run_id")
                if runId is None:
                    raise ValueError("LLM_RUN_ID_REQUIRED_FOR_DURABLE_USAGE")
                from api.services.credits.creditService import creditService
                creditService.settleCreditOperation(self._context,totalTokens,str(runId))
        except Exception as e:
            logger.warning(
                f"CreditTrackingCallback.on_llm_end failed — "
                f"userId={self.userId}, op={self.operationType}: {e}"
            )
            if self._context is not None:
                raise
