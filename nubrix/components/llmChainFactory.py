"""
llmChainFactory.py

Factory for building LangChain text-generation chains from config.ini section
+ prompts.yaml key. Replaces the per-component boilerplate classes
(CodeGenerator, CodeDebugger, DashboardNameGenerator, DomainKpiMapper,
InsightGenerator), which were 80-line copies of the same pattern differing only
by config section name and prompt key.

The parsed prompts.yaml (2000+ lines) and config.ini are cached at module level
so repeated chain builds (3 per reporting workflow = dozens per second under
load) never re-parse from disk.
"""

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough, RunnableLambda
from utils.llm import getGenaiLlm, cleanThinkTokens
from utils.exceptionHandler import CustomException
from nubrix.utils import readYaml, getConfig
from utils.logger import logger
import os

_PROMPTS_PATH = os.path.join(os.getcwd(), "prompts.yaml")
_CONFIG_PATH = os.path.join(os.getcwd(), "config.ini")
_PROMPTS: dict | None = None
_CONFIG = None


def _prompts() -> dict:
    global _PROMPTS
    if _PROMPTS is None:
        _PROMPTS = readYaml(_PROMPTS_PATH)
    return _PROMPTS


def _config():
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = getConfig(_CONFIG_PATH)
    return _CONFIG


def buildLlmChain(section: str, promptKey: str, fallbackTokens: int = 8192, jsonOutput: bool = False):
    """Build a prompt -> LLM -> cleanThinkTokens -> StrOutput chain.

    Args:
        section: config.ini section (e.g. "CODEGENERATOR").
        promptKey: key in prompts.yaml (e.g. "codeGeneratorAgentPrompt").
        fallbackTokens: max_tokens fallback when config omits it.
        jsonOutput: ask Gemini for a JSON reply (`response_mime_type` set to
            "application/json" on this chain's calls only; the cached model
            instance is shared and left unchanged). The chain still returns
            text, which the caller validates.

    Returns:
        Runnable chain.
    """
    try:
        logger.info(f"Constructing LLM chain: section={section}, prompt={promptKey}.")
        config = _config()
        promptTemplate = _prompts().get(promptKey)
        prompt = PromptTemplate.from_template(promptTemplate)
        llm = getGenaiLlm(
            model=config.get(section, "model"),
            max_tokens=config.getint(section, "maxTokens", fallback=fallbackTokens),
        )
        if jsonOutput:
            llm = llm.bind(response_mime_type="application/json")
        chain = RunnablePassthrough() | prompt | llm | RunnableLambda(cleanThinkTokens) | StrOutputParser()
        logger.info(f"LLM chain constructed: section={section}.")
        return chain
    except Exception as e:
        exception = CustomException(e)
        logger.error(exception)
        raise exception