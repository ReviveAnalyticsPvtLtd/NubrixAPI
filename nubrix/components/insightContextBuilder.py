"""
insightContextBuilder.py

Assembles a rich context payload for the hybrid image-to-insights pipeline.
Loads project metadata, dashboard configuration, domain expert profiles,
and user-provided objectives into a single dict consumed by the LLM prompt.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["InsightContextBuilder"]


from utils.exceptionHandler import CustomException
from utils.projectStorage import readJson
from urllib.request import urlopen
from utils.logger import logger
from api.commons import client
from dataclasses import dataclass
import json
import time
import os


@dataclass
class InsightContextBuilderConfig:
    """
    Configuration dataclass for InsightContextBuilder.

    Attributes:
        storageBucket (str): Supabase storage bucket name for project artifacts.
        domainBucket (str): Supabase storage bucket name for domain-specific KPI profiles.
    """
    storageBucket: str = "AnalyticsHub"
    domainBucket: str = "DomainSpecificKpis"


class InsightContextBuilder:
    """
    Builds a consolidated context dict from project artifacts and user-supplied
    parameters for use in the hybrid insight generation pipeline.
    """
    def __init__(self):
        """Initializes the InsightContextBuilder and sets up the Supabase client."""
        logger.info("Initializing InsightContextBuilder.")
        self.config = InsightContextBuilderConfig()
        self.client = client

    def _loadJsonFromStorage(self, projectId: str, fileName: str) -> dict | list | None:
        """
        Loads a JSON file from Supabase storage with cache-busting.

        Returns None if the file does not exist in the project folder.
        """
        try:
            files = self.client.storage.from_(self.config.storageBucket).list(path=projectId)
            if fileName not in [x.get("name") for x in files]:
                return None
            return readJson(projectId, fileName)
        except Exception:
            return None

    def _loadDomainContext(self, projectId: str) -> dict:
        """
        Loads the domain expert profile for a project from the DomainSpecificKpis bucket.

        Includes extended playbook keys when available: northStar, driverTree,
        decisionLevers, alertThresholds, recommendedActions.
        """
        try:
            row = self.client.table("Projects").select("domainExpert").eq("projectId", projectId).execute().data
            if not row:
                return {}
            domainFile = row[0].get("domainExpert", "") + ".json"
            available = [x.get("name") for x in self.client.storage.from_(self.config.domainBucket).list()]
            if domainFile not in available:
                return {}
            domainFileUrl = os.environ["DOMAIN_FILE_URL"].format(fileName=domainFile) + f"?cb={int(time.time())}"
            domainData = json.loads(urlopen(domainFileUrl).read())
            playbookKeys = ["northStar", "driverTree", "decisionLevers", "alertThresholds", "recommendedActions"]
            playbook = {k: domainData[k] for k in playbookKeys if k in domainData}
            return {
                "profile": domainData,
                "playbook": playbook
            }
        except Exception:
            return {}

    def _extractChartData(self, dashboardConfig: dict, pageId: str) -> list[dict]:
        """
        Extracts widget data from a single dashboard page.

        Carries `generatedCode` through so downstream provenance extraction can
        recover what each widget measures, and assigns each widget a short
        sequential `ref` the LLM can cite in its findings.

        Args:
            dashboardConfig (dict): Parsed dashboardConfig.json.
            pageId (str): The dashboard page to extract widgets from.

        Returns:
            list[dict]: Widget dicts in render order.
        """
        chartData = []
        if not dashboardConfig:
            return chartData

        for pid in [pageId]:
            page = dashboardConfig.get(pid)
            if not page:
                continue
            for widget in page.get("widgets", []):
                chartData.append({
                    "ref": f"W{len(chartData) + 1}",
                    "id": widget.get("id"),
                    "pageId": pid,
                    "title": widget.get("title"),
                    "chartType": widget.get("chartType"),
                    "data": widget.get("data"),
                    "xLabels": widget.get("xLabels"),
                    "yLabels": widget.get("yLabels"),
                    "label": widget.get("label"),
                    "map": widget.get("map"),
                    "generatedCode": widget.get("generatedCode"),
                })
        return chartData

    def buildContext(
        self,
        projectId: str,
        pageId: str,
    ) -> dict:
        """
        Assembles the full context payload for insight generation.

        Args:
            projectId (str): The project identifier.
            pageId (str): The dashboard page to generate insights for.

        Returns:
            dict: Context payload with keys: metadata, dashboard, chartData, domainContext, filters.

        Raises:
            CustomException: If a critical error prevents context assembly.
        """
        try:
            logger.info(f"Building insight context for project {projectId}.")

            metadata = self._loadJsonFromStorage(projectId, "metadata.json") or {}
            metadata = {k: v for k, v in metadata.items() if not k.startswith("_") and v.get("isActive", True) is not False}
            dashboardConfig = self._loadJsonFromStorage(projectId, "dashboardConfig.json") or {}
            existingInsights = self._loadJsonFromStorage(projectId, "insights.json") or {}
            domainContext = self._loadDomainContext(projectId)
            chartData = self._extractChartData(dashboardConfig, pageId)

            pages = [
                {"pageId": pid, "pageName": (page or {}).get("name")}
                for pid, page in dashboardConfig.items()
                if isinstance(page, dict)
            ]
            pageName = next(
                (page["pageName"] for page in pages if page["pageId"] == pageId),
                None,
            )

            context = {
                "metadata": metadata,
                "dashboard": {
                    "pageId": pageId,
                    "pageName": pageName,
                    "pages": pages,
                    "widgetCount": len(chartData),
                    "existingInsights": existingInsights,
                },
                "chartData": chartData,
                "domainContext": domainContext,
            }

            logger.info(f"Insight context assembled: {len(chartData)} widgets, domain={'present' if domainContext else 'absent'}.")
            return context
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception
