import os
from langchain_google_genai import ChatGoogleGenerativeAI

# Singleton cache: (model, max_tokens) -> ChatGoogleGenerativeAI.
# A reporting-workflow build constructs 3 LLM clients (rephraser/codegen/debugger);
# caching them avoids repeated HTTP-client + credential setup per request.
# ChatGoogleGenerativeAI is stateless and thread-safe for invocation.
#
# NOTE: temperature is intentionally NOT passed to the model. Per Google's latest
# documentation, sampling parameters (temperature, top_p, top_k) should not be
# used with the latest Gemini models. The models use deterministic defaults.
_LLM_CACHE: dict = {}


def getGenaiLlm(model: str, temperature: float = None, max_tokens: int = None) -> ChatGoogleGenerativeAI:
    """
    Factory that returns a cached ChatGoogleGenerativeAI instance, enforcing the
    use of API keys and disabling GCE metadata check.

    Args:
        model: The Gemini model name (e.g. "gemini-3.5-flash-lite").
        temperature: **Deprecated/ignored.** Retained only for backward-compat
            with existing call sites. Per Google's latest documentation, sampling
            parameters must not be set for the latest Gemini models. Passing a
            value has no effect — it is never forwarded to the model.
        max_tokens: Optional maximum output tokens.

    Returns:
        A cached (or newly created) ChatGoogleGenerativeAI instance.
    """
    import warnings
    warnings.filterwarnings("ignore", message=".*non-text parts.*")

    os.environ["NO_GCE_CHECK"] = "true"
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"

    # Cache key excludes temperature (it is never forwarded to the model).
    cache_key = (model, max_tokens)
    cached = _LLM_CACHE.get(cache_key)
    if cached is not None:
        return cached

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError(
            "Google GenAI API Key is missing. Please set GEMINI_API_KEY or GOOGLE_API_KEY in your environment."
        )

    kwargs = {
        "model": model,
        "api_key": api_key,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    llm = ChatGoogleGenerativeAI(**kwargs)
    _LLM_CACHE[cache_key] = llm
    return llm

from langchain_core.messages import AIMessage

def cleanThinkTokens(message: AIMessage) -> AIMessage:
    """
    Robustly removes <think> and </think> tokens from AIMessage content,
    supporting both raw string content and multi-part list content structures.
    """
    content = message.content
    if isinstance(content, str):
        clean_content = content.replace("<think>", "").replace("</think>", "")
    elif isinstance(content, list):
        clean_list = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                part_copy = dict(part)
                part_copy["text"] = part_copy["text"].replace("<think>", "").replace("</think>", "")
                clean_list.append(part_copy)
            elif isinstance(part, str):
                clean_list.append(part.replace("<think>", "").replace("</think>", ""))
            else:
                clean_list.append(part)
        clean_content = clean_list
    else:
        clean_content = content
    return AIMessage(
        content=clean_content,
        additional_kwargs=message.additional_kwargs,
        response_metadata=message.response_metadata,
        id=message.id
    )

from langfuse.langchain import CallbackHandler

_langfuse_handler = None

def _langfuseConfigured() -> bool:
    """True when the Langfuse keys are set. Maps LANGFUSE_BASE_URL to the standard LANGFUSE_HOST if needed."""
    if "LANGFUSE_BASE_URL" in os.environ and "LANGFUSE_HOST" not in os.environ:
        os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))

def getLangfuseHandler() -> CallbackHandler | None:
    """
    Returns a cached instance of the Langfuse CallbackHandler for LangChain tracing.
    """
    global _langfuse_handler
    if _langfuse_handler is None:
        if _langfuseConfigured():
            _langfuse_handler = CallbackHandler()
    return _langfuse_handler

def newLangfuseHandler() -> CallbackHandler | None:
    """
    Returns a new CallbackHandler (the Langfuse client behind it stays shared), or None without keys.

    The cached handler keeps one `last_trace_id` for the whole process, which concurrent runs overwrite.
    A run that logs its own trace id needs a handler of its own.
    """
    return CallbackHandler() if _langfuseConfigured() else None

def getLangfuseConfig(
    trace_name: str,
    projectId: str = None,
    userId: str = None,
    session_id: str = None,
    tags: list[str] = None,
    perRequestHandler: bool = False
) -> dict:
    """
    Constructs the config dictionary to pass to LangChain chain executions
    (e.g., config=getLangfuseConfig(...)) containing callbacks for Langfuse.
    """
    handler = newLangfuseHandler() if perRequestHandler else getLangfuseHandler()
    if not handler:
        return {}
        
    metadata = {
        "trace_name": trace_name,
    }
    
    if projectId:
        metadata["projectId"] = projectId
        metadata["langfuse_session_id"] = projectId
    if userId:
        metadata["userId"] = userId
        metadata["langfuse_user_id"] = userId
    if session_id:
        metadata["langfuse_session_id"] = session_id
        
    if tags:
        metadata["langfuse_tags"] = tags
    else:
        metadata["langfuse_tags"] = [trace_name]
        
    return {
        "callbacks": [handler],
        "metadata": metadata
    }
