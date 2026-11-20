"""
queryRephraser.py

This module contains the QueryRephaser agent for rephrasing user queries.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["QueryRephaser", "ParallelQueryRephaser"]        


from langchain_core.output_parsers import JsonOutputParser
from utils.llm import getGenaiLlm
from langchain_core.runnables import RunnableLambda
from utils.exceptionHandler import CustomException
from nubrix.utils import readYaml, getConfig
from langchain_core.prompts import PromptTemplate
from langchain_core.messages import AIMessage
from pydantic import Field, BaseModel
from dataclasses import dataclass
from utils.logger import logger
import os

@dataclass
class QueryRephraserConfig:
    """Configuration for the QueryRephaser."""
    yamlPath: str = os.path.join(os.getcwd(), "prompts.yaml")
    configPath: str = os.path.join(os.getcwd(), "config.ini")

class QueryRephraseOutput(BaseModel):
    """Pydantic model for the structured output of the query rephraser agent.

    Both fields are MANDATORY in the LLM's JSON output. Each is given an
    explicit default of None so that JsonOutputParser fills the field with
    None when the LLM omits it, instead of silently dropping the key. This
    guarantees downstream code (reportingToolWorkflow._router,
    _formatJsonResponse) can always access rephrasedOutput['doubt'] without
    a KeyError, and the LLM is told in the prompt that both fields must be
    returned even when one of them is null.
    """
    rephrasedOutput: str | None = Field(
        default=None,
        description=(
            "A clear and concise rephrased version of the user's query with "
            "step-by-step transformation instructions. Set to null when the "
            "query is invalid or unclear, but the field MUST still be present "
            "in the output. The output schema is enforced; an absent "
            "rephrasedOutput field will be rejected."
        ),
    )
    doubt: str | None = Field(
        default=None,
        description=(
            "A short, non-technical message indicating any doubt, required "
            "clarification, or reason why the input query cannot be executed. "
            "Set to null when the query is successfully rephrased, but the "
            "field MUST still be present in the output. The output schema is "
            "enforced; an absent doubt field will be rejected."
        ),
    )

class QueryRephaser:
    """An agent that rephrases a user's query to be clearer and more specific."""
    def __init__(self, promptKey: str = "queryRephraserAgentPrompt"):
        """Initializes the QueryRephaser and its configuration.

        Args:
            promptKey (str): The prompts.yaml key of the rephraser prompt. The
                default is the Python chart engine's; the SQL chart engine
                passes "sqlQueryRephraserAgentPrompt".
        """
        logger.info("Initializing QueryRephaser.")
        self.queryRephraserConfig = QueryRephraserConfig()
        self.promptKey = promptKey

    def _removeThinkTokens(self, inputStr: AIMessage) -> AIMessage:
        """
        Removes '<think>' and '</think>' tokens (and the text inside them) from the AIMessage content,
        and extracts the raw JSON string.
        
        Args:
            inputStr (AIMessage): The input AIMessage from the language model.
            
        Returns:
            AIMessage: A new AIMessage containing only the clean JSON string.
        """
        import re
        content = inputStr.content
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    text_parts.append(part["text"])
                elif isinstance(part, str):
                    text_parts.append(part)
            content = "".join(text_parts)
        elif not isinstance(content, str):
            content = str(content)
            
        # Remove <think>...</think> completely (non-greedy)
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
        
        # Extract JSON block
        json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        if json_match:
            content = json_match.group(1).strip()
        else:
            json_match_any = re.search(r'```\s*(.*?)\s*```', content, re.DOTALL)
            if json_match_any:
                content = json_match_any.group(1).strip()
            else:
                start = content.find('{')
                end = content.rfind('}')
                if start != -1 and end != -1 and end > start:
                    content = content[start:end+1].strip()
        
        return AIMessage(content.strip())

    def _ensureFields(self, parsed_dict: dict) -> dict:
        """
        Validates the parsed dictionary against QueryRephraseOutput using Pydantic,
        which fills in default values (None) for any missing keys to prevent KeyErrors.
        """
        try:
            return QueryRephraseOutput.model_validate(parsed_dict).model_dump()
        except Exception as e:
            logger.warning(f"Failed to validate rephraser output: {e}. Falling back to default keys.")
            return {
                "rephrasedOutput": parsed_dict.get("rephrasedOutput") if isinstance(parsed_dict, dict) else None,
                "doubt": parsed_dict.get("doubt") if isinstance(parsed_dict, dict) else None
            }

    def getQueryRephraserChain(self):
        """
        Constructs and returns a LangChain chain for query rephrasing.

        The chain consists of a prompt, a Cerebras LLM, a token remover, 
        and a JSON output parser.

        Raises:
            CustomException: If there is an error during chain construction.

        Returns:
            A LangChain runnable chain.
        """
        try:
            logger.info("Constructing query rephraser chain.")
            self.config = getConfig(self.queryRephraserConfig.configPath)
            queryRephraseParser = JsonOutputParser(pydantic_object = QueryRephraseOutput)
            queryRephrasePrompt = PromptTemplate(
                template = readYaml(self.queryRephraserConfig.yamlPath).get(self.promptKey),
                input_variables = ["metadata", "query"],
                partial_variables = {"format_instructions": queryRephraseParser.get_format_instructions()}
            )
            llm = getGenaiLlm(
                model=self.config.get("QUERYREPHRASER", "model"),
                max_tokens=self.config.getint("QUERYREPHRASER", "maxTokens", fallback=8192)
            )
            queryRephraseChain = (
                queryRephrasePrompt 
                | llm 
                | RunnableLambda(self._removeThinkTokens) 
                | queryRephraseParser 
                | RunnableLambda(self._ensureFields)
            )
            logger.info("Query rephraser chain constructed successfully.")
            return queryRephraseChain
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception


class ParallelQueryRephraseOutput(BaseModel):
    """Pydantic model for the structured output of the parallel query rephraser agent. No doubt field."""
    rephrasedOutput: str = Field(
        description="A clear and concise rephrased version of the user's query with step-by-step transformation instructions. This must ALWAYS be provided."
    )
class ParallelQueryRephaser(QueryRephaser):
    """An agent that rephrases queries for parallel generation without issuing doubts."""

    def __init__(self, promptKey: str = "parallelQueryRephraserAgentPrompt"):
        """Initializes the parallel rephraser; the SQL chart engine passes "parallelSqlQueryRephraserAgentPrompt"."""
        super().__init__(promptKey=promptKey)

    def _ensureParallelFields(self, parsed_dict: dict) -> dict:
        """
        Validates the parsed dictionary against ParallelQueryRephraseOutput using Pydantic,
        filling in None/default values if missing.
        """
        try:
            return ParallelQueryRephraseOutput.model_validate(parsed_dict).model_dump()
        except Exception as e:
            logger.warning(f"Failed to validate parallel rephraser output: {e}. Falling back.")
            return {
                "rephrasedOutput": parsed_dict.get("rephrasedOutput") if isinstance(parsed_dict, dict) else None
            }

    def getQueryRephraserChain(self):
        try:
            logger.info("Constructing parallel query rephraser chain.")
            self.config = getConfig(self.queryRephraserConfig.configPath)
            queryRephraseParser = JsonOutputParser(pydantic_object = ParallelQueryRephraseOutput)
            queryRephrasePrompt = PromptTemplate(
                template = readYaml(self.queryRephraserConfig.yamlPath).get(self.promptKey),
                input_variables = ["metadata", "query"],
                partial_variables = {"format_instructions": queryRephraseParser.get_format_instructions()}
            )
            llm = getGenaiLlm(
                model=self.config.get("QUERYREPHRASER", "model"),
                max_tokens=self.config.getint("QUERYREPHRASER", "maxTokens", fallback=8192)
            )
            queryRephraseChain = (
                queryRephrasePrompt 
                | llm 
                | RunnableLambda(self._removeThinkTokens) 
                | queryRephraseParser
                | RunnableLambda(self._ensureParallelFields)
            )
            logger.info("Parallel query rephraser chain constructed successfully.")
            return queryRephraseChain
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception