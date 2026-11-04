"""
transformationAgent.py

This module defines the TransformationAgent for generating executable pandas
transformation code, Mermaid flowcharts, and user-facing summaries.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["TransformationAgent"]


from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from utils.llm import getGenaiLlm
from utils.exceptionHandler import CustomException
from utils.sandboxSlots import isBusy
from api.models import TransformationAgentResponse
from nubrix.utils import getConfig, readYaml
from collections import OrderedDict
from utils.logger import logger
from dataclasses import dataclass
import json
import os


def getStringContent(content) -> str:
    """Helper to convert LangChain message content (possibly list of dicts/strings) to a string."""
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                parts.append(part["text"])
        return "".join(parts)
    return ""


@dataclass
class TransformationAgentConfig:
    """Configuration for the TransformationAgent."""
    yamlPath: str = os.path.join(os.getcwd(), "prompts.yaml")
    configPath: str = os.path.join(os.getcwd(), "config.ini")


class TransformationAgent:
    """
    Agent wrapper for transformation generation.

    Maintains per-transformation chat context persisted in PostgreSQL/Supabase
    and summarizes history older than the last 2 exchanges (4 messages) to optimize context window usage.
    """

    _MAX_SUMMARY_CACHE_SIZE = 256

    def __init__(self):
        """Initialize model configuration."""
        logger.info("Initializing TransformationAgent.")
        self.transformationAgentConfig = TransformationAgentConfig()
        self.config = getConfig(self.transformationAgentConfig.configPath)
        self.systemPrompt = readYaml(self.transformationAgentConfig.yamlPath).get("transformationAgentPrompt")
        self.llm = getGenaiLlm(
            model=self.config.get("TRANSFORMATIONAGENT", "model"),
            max_tokens=self.config.getint("TRANSFORMATIONAGENT", "maxTokens", fallback=8192),
        )
        # LRU cache for raw LLM summaries (keyed by message-id chain)
        self._historySummaryCache: OrderedDict[str, str] = OrderedDict()
        # Per-thread summary state (keyed by transformationId)
        self._threadSummaryCache: dict[str, tuple[str, int]] = {}

    async def _summarizeHistory(self, oldMessages: list[BaseMessage], cacheKey: str, userId: str | None = None) -> str:
        """Summarize old chat history messages using the LLM, with LRU-bounded caching."""
        if not oldMessages:
            return ""
        if cacheKey in self._historySummaryCache:
            logger.info("Using cached history summary.")
            self._historySummaryCache.move_to_end(cacheKey)
            return self._historySummaryCache[cacheKey]
        formatted = []
        for msg in oldMessages:
            role = "User" if isinstance(msg, HumanMessage) else "Assistant"
            content = getStringContent(msg.content)
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and "userFacingResponse" in parsed:
                    content = parsed["userFacingResponse"]
                    if parsed.get("pythonCode"):
                        content += " (Transformation code was generated)"
            except Exception:
                pass
            formatted.append(f"{role}: {content}")
        textToSummarize = "\n".join(formatted)

        prompt = (
            "Summarize the following data transformation conversation history as compact bullet points. "
            "The summary will be used as context for FUTURE steps that must build cumulatively on prior work, "
            "so preserve concrete, actionable details and DROP pleasantries.\n\n"
            "MUST PRESERVE (be specific, use exact identifiers):\n"
            "- Source table names loaded (e.g. `orders`, `customers`) and any aliases assigned.\n"
            "- Every transformation applied in order: filters (with conditions), joins (with keys), "
            "column renames (old -> new), column additions/deletions, aggregations (group-by keys, agg funcs), "
            "sorts, type conversions, and the resulting intermediate/final variable names.\n"
            "- Any user constraints or decisions: required columns in output, naming conventions, "
            "business rules the user stated.\n"
            "- Failed attempts the user redirected away from (e.g. 'tried join on user_id, switched to customer_id'), "
            "so we don't repeat the same mistake.\n"
            "- The current output table name (if any has been applied).\n\n"
            "DROP: greetings, apologies, generic phrases like 'I will help you', restatements of the user's request.\n\n"
            f"CONVERSATION:\n{textToSummarize}"
        )
        try:
            from utils.llm import getLangfuseConfig
            from api.services.credits.creditTrackingCallback import CreditTrackingCallback
            summaryConfig = getLangfuseConfig(trace_name="TransformationAgent-HistorySummary", userId=userId)
            if userId:
                summaryConfig.setdefault("callbacks", []).append(
                    CreditTrackingCallback(userId=userId, operationType="transformation_history_summary")
                )
            summaryResponse = await self.llm.ainvoke(
                prompt,
                config=summaryConfig or None,
            )
            summary = summaryResponse.content
            # LRU eviction: remove oldest entry if cache exceeds max size
            self._historySummaryCache[cacheKey] = summary
            if len(self._historySummaryCache) > self._MAX_SUMMARY_CACHE_SIZE:
                self._historySummaryCache.popitem(last=False)
            return summary
        except Exception as e:
            logger.warning(f"Failed to summarize history: {e}")
            return "Earlier conversation history summary could not be generated."

    def invalidateThreadCache(self, transformationId: str) -> None:
        """Invalidate the cached summary for a transformation thread (e.g. after rollback)."""
        self._threadSummaryCache.pop(transformationId, None)

    async def _getMessages(self, chatHistory: list[dict], formattedInput: str, transformationId: str, userId: str | None = None) -> list[BaseMessage]:
        """Convert database chat history to LangChain messages, summarizing older history efficiently."""
        history = []
        for msg in chatHistory:
            role = msg.get("role")
            if role == "user":
                history.append(HumanMessage(content=msg.get("content") or ""))
            elif role == "assistant":
                # Reconstruct full assistant model output representation as JSON (including nulls)
                assistantContent = json.dumps({
                    "userFacingResponse": msg.get("content") or "",
                    "pythonCode": msg.get("python_code"),
                    "mermaidCode": msg.get("artifact", {}).get("code") if msg.get("artifact") else None
                }, ensure_ascii=False)
                history.append(AIMessage(content=assistantContent))

        # Find the last active python and mermaid code in the entire history to preserve active code state
        last_python_code = None
        last_mermaid_code = None
        for i in range(len(chatHistory) - 1, -1, -1):
            msg = chatHistory[i]
            if msg.get("role") == "assistant" and msg.get("python_code"):
                last_python_code = msg.get("python_code")
                last_mermaid_code = msg.get("artifact", {}).get("code") if msg.get("artifact") else None
                break

        # Keep past 4 messages (last 2 exchanges) in full; summarize anything older
        summaryMessage = None
        unsummarized_msgs = []
        recentHistory = history

        if len(history) > 4:
            oldHistory = history[:-4]
            recentHistory = history[-4:]
            oldChatHistory = chatHistory[:-4]

            # Get cached summary for this transformation thread
            cached_summary, cached_count = self._threadSummaryCache.get(transformationId, ("", 0))

            # Detect stale cache (e.g. after rollback truncated messages)
            if cached_count > len(oldHistory):
                cached_summary = ""
                cached_count = 0

            # Check how many new messages need to be summarized since the last summary
            new_unsummarized_count = len(oldHistory) - cached_count

            if new_unsummarized_count >= 4 or (not cached_summary and len(oldHistory) >= 4):
                # Regenerate summary and cache it (only if there's enough old history to justify an LLM call)
                logger.info(f"Regenerating conversation history summary for thread {transformationId}.")
                cacheKey = ":".join(m.get("message_id", "") for m in oldChatHistory)
                summaryText = await self._summarizeHistory(oldHistory, cacheKey, userId=userId)
                self._threadSummaryCache[transformationId] = (summaryText, len(oldHistory))
                cached_summary = summaryText
                unsummarized_msgs = []
            else:
                # Use cached summary and pass any unsummarized old messages in full
                # (also covers the case where oldHistory < 4 and no summary exists yet)
                unsummarized_msgs = oldHistory[cached_count:]

            if cached_summary:
                summaryMessage = SystemMessage(
                    content=f"Summary of earlier conversation history:\n{cached_summary}"
                )

        messages_to_send = []
        if summaryMessage:
            messages_to_send.append(summaryMessage)

        # Inject the active code state if it exists, so the model always knows the current active code
        if last_python_code:
            activeCodeMessage = SystemMessage(
                content=(
                    "Current Active Code State:\n"
                    f"### Python Code:\n```python\n{last_python_code}\n```\n\n"
                    f"### Mermaid Flowchart:\n```mermaid\n{last_mermaid_code or ''}\n```\n"
                    "CRITICAL DIRECTIVE: You must build upon, modify, or extend this active code state. "
                    "Your output Python code must represent the cumulative pipeline (including the previous steps and the new step). "
                    "Do not restart from scratch unless the user explicitly requests a reset."
                )
            )
            messages_to_send.append(activeCodeMessage)

        messages_to_send.extend(unsummarized_msgs)
        messages_to_send.extend(recentHistory)
        messages_to_send.append(HumanMessage(content=formattedInput))
        return messages_to_send

    async def invoke(
        self,
        projectId: str,
        transformationId: str,
        userMessage: str,
        metadata: dict,
        chatHistory: list[dict],
        callbacks: list | None = None,
        userId: str | None = None,
    ) -> TransformationAgentResponse:
        """
        Generate a structured transformation response using a clean tool-calling loop and self-healing retries.
        """
        from nubrix.components.transformationExecutor import TransformationExecutor

        executor = TransformationExecutor()

        # Define the inspection tool
        @tool
        def inspect_dataset(pythonCode: str) -> str:
            """
            Run read-only inspection code in a safe Python sandbox on the project's data.
            Use this tool to inspect table shapes, column names, duplicate counts, unique values, and sample data.
            Your code MUST fetch data using fetch_data_pl(projectId, '<table_name>') (Polars, preferred) or fetch_data(projectId, '<table_name>') (pandas), perform inspection operations, and print the results.
            For Polars: use .schema, .columns, .height, .head(), .unique(). For pandas: .shape, .columns, .dtypes, .head().
            The printed output will be returned to you. Limit pythonCode to read-only inspection operations.
            """
            code_to_exec = pythonCode.strip()
            # If the LLM wraps the argument as a JSON/dict object, defensively unpack it
            if (code_to_exec.startswith("{") and code_to_exec.endswith("}")) or "pythonCode" in code_to_exec:
                try:
                    parsed = json.loads(code_to_exec)
                    if isinstance(parsed, dict) and "pythonCode" in parsed:
                        code_to_exec = parsed["pythonCode"]
                except Exception:
                    # Attempt simple regex extraction for escaped/nested quotes
                    import re
                    match = re.search(r'"pythonCode"\s*:\s*"(.*?)"', code_to_exec, re.DOTALL)
                    if match:
                        try:
                            code_to_exec = match.group(1).encode().decode('unicode-escape')
                        except Exception:
                            code_to_exec = match.group(1)

            return executor.executeInspection(projectId, code_to_exec)

        # Bind both inspect_dataset and the final schema (TransformationAgentResponse) to the model
        bound_llm = self.llm.bind_tools([inspect_dataset, TransformationAgentResponse])

        try:
            # Build system prompt with metadata injected (passed to the agent, not the user turn)
            metadataJson = json.dumps(metadata, ensure_ascii=False, default=str)
            system_prompt = (
                self.systemPrompt
                .replace("{metadata}", metadataJson)
                .replace("{user_request}", userMessage)
            )

            base_input = f"Please perform the transformation: {userMessage}"
            max_healing_attempts = 3
            error_feedback = ""
            response: TransformationAgentResponse | None = None

            # Setup Langfuse config
            from utils.llm import getLangfuseConfig
            agentConfig = getLangfuseConfig(
                trace_name="TransformationAgent",
                projectId=projectId,
                userId=userId,
            )
            if callbacks:
                agentConfig.setdefault("callbacks", []).extend(callbacks)

            # We loop over self-healing attempts
            for attempt in range(max_healing_attempts):
                current_input = base_input
                if error_feedback:
                    current_input = (
                        f"{base_input}\n\n"
                        f"[RETRY FEEDBACK]: Your previous response failed with the following error:\n"
                        f"{error_feedback}\n"
                        "Please correct the formatting and code structure."
                    )

                # Fetch fresh message history starting from chatHistory + current input
                messages_to_send = await self._getMessages(
                    chatHistory=chatHistory,
                    formattedInput=current_input,
                    transformationId=transformationId,
                    userId=userId,
                )

                # Prepend the system prompt at the beginning of the messages list
                messages_list = [SystemMessage(content=system_prompt)] + messages_to_send

                # Run the internal tool-calling loop (up to 5 steps of tool call/response)
                max_iterations = 5
                response = None
                
                for iteration in range(max_iterations):
                    res = await bound_llm.ainvoke(messages_list, config=agentConfig)
                    
                    if res.tool_calls:
                        # Model called a tool or schema function
                        tool_call = res.tool_calls[0]
                        name = tool_call["name"]
                        args = tool_call["args"]
                        call_id = tool_call["id"]
                        
                        if name == "TransformationAgentResponse":
                            # Model produced final answer
                            try:
                                response = TransformationAgentResponse(
                                    pythonCode=args.get("pythonCode"),
                                    mermaidCode=args.get("mermaidCode"),
                                    userFacingResponse=args.get("userFacingResponse") or ""
                                )
                            except Exception as pe:
                                logger.warning(f"Failed parsing response schema parameters: {pe}")
                                response = None
                            break
                        elif name == "inspect_dataset":
                            logger.info(f"Iteration {iteration + 1}: inspect_dataset called with args={args}")
                            messages_list.append(res)
                            try:
                                tool_result = inspect_dataset.invoke(args)
                            except Exception as tool_e:
                                if isBusy(tool_e):  # Not a code failure: stop and tell the caller.
                                    raise
                                tool_result = f"Error executing inspection: {tool_e}"
                            messages_list.append(ToolMessage(content=tool_result, tool_call_id=call_id))
                        else:
                            # Unknown tool
                            messages_list.append(res)
                            messages_list.append(ToolMessage(content="Unknown tool name.", tool_call_id=call_id))
                    else:
                        # Model returned pure text response (fallback parsing)
                        text = getStringContent(res.content).strip()
                        if text.startswith("```json"):
                            text = text[7:]
                        if text.endswith("```"):
                            text = text[:-3]
                        text = text.strip()
                        try:
                            parsed = json.loads(text)
                            response = TransformationAgentResponse(
                                pythonCode=parsed.get("pythonCode"),
                                mermaidCode=parsed.get("mermaidCode"),
                                userFacingResponse=parsed.get("userFacingResponse") or ""
                            )
                        except Exception:
                            # Conversation fallback
                            response = TransformationAgentResponse(
                                pythonCode=None,
                                mermaidCode=None,
                                userFacingResponse=getStringContent(res.content)
                            )
                        break

                if response is None:
                    error_feedback = "Failed to output structured response matching the TransformationAgentResponse schema."
                    continue

                # If no Python code is returned, return directly
                if not response.pythonCode:
                    return response

                # Test-execute the generated Python code; retry on failure
                try:
                    executor._execute_code(projectId=projectId, pythonCode=response.pythonCode)
                    logger.info(f"Generated python code executed successfully on attempt {attempt + 1}.")
                    return response
                except Exception as e:
                    if isBusy(e):  # Not a code failure: no rewrite, no new attempt.
                        raise
                    error_msg = str(e)
                    logger.warning(f"Transformation code execution failed on attempt {attempt + 1}: {error_msg}")
                    code_snippet = response.pythonCode[:1500] if response.pythonCode else ""
                    error_feedback = (
                        f"The Python code you generated failed to execute with the following error:\n"
                        f"{error_msg}\n\n"
                        f"Failing code (first 1500 chars):\n```python\n{code_snippet}\n```\n"
                        "Analyze the error, rewrite the Python code and Mermaid diagram to resolve it, "
                        "and ensure the output is correct and executable. Do NOT repeat the same mistake."
                    )

            if response is None:
                logger.error("TransformationAgent exhausted all retries with no parseable response.")
                return TransformationAgentResponse(
                    pythonCode=None,
                    mermaidCode=None,
                    userFacingResponse=(
                        "I was unable to generate a valid transformation after multiple attempts. "
                        "Please try rephrasing your request with more specific table and column details."
                    ),
                )
            # Exhausted retries on execution failures; return the last response with a caveat.
            return TransformationAgentResponse(
                pythonCode=response.pythonCode,
                mermaidCode=response.mermaidCode,
                userFacingResponse=(
                    f"{response.userFacingResponse}\n\n"
                    "Note: the generated code could not be validated in the sandbox after multiple attempts. "
                    "Review the pipeline before applying."
                ) if response.pythonCode else response.userFacingResponse,
            )

        except Exception as e:
            if isBusy(e):
                raise
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    async def astream(
        self,
        projectId: str,
        transformationId: str,
        userMessage: str,
        metadata: dict,
        chatHistory: list[dict],
        callbacks: list | None = None,
        userId: str | None = None,
    ):
        """
        Stream transformation output events, yielding progress status updates
        and a word-by-word replay of the user-facing summary.
        """
        yield {"type": "status", "message": "Analyzing your request against the project schema..."}
        yield {"type": "status", "message": "Reasoning and constructing the transformation pipeline..."}
        response = await self.invoke(
            projectId=projectId,
            transformationId=transformationId,
            userMessage=userMessage,
            metadata=metadata,
            chatHistory=chatHistory,
            callbacks=callbacks,
            userId=userId,
        )
        if response.pythonCode:
            yield {"type": "status", "message": "Validating the generated code in a sandbox..."}
        summary = response.userFacingResponse or ""
        # Preserve original whitespace; emit word tokens with their trailing spaces.
        tokens = summary.split(" ")
        for i, token in enumerate(tokens):
            delta = token + (" " if i < len(tokens) - 1 else "")
            if delta:
                yield {"type": "token", "delta": delta}
        yield {"type": "done", "structured": response}
