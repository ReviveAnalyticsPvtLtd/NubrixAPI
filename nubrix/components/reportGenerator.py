"""
reportGenerator.py

This module provides the ReportGenerator class for generating profiling reports and retrieving table metadata
from remote project data sources. It uses ydata_profiling for report generation and BeautifulSoup for HTML post-processing.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["ReportGenerator"]        


from ydata_profiling import ProfileReport
from utils.projectStorage import readJson, readTableBytes
from bs4 import BeautifulSoup
import pandas as pd
import io

class ReportGenerator:
    """
    ReportGenerator handles the retrieval of table metadata and the generation of profiling reports for project tables.
    """
    def __init__(self):
        """
        Initializes the ReportGenerator instance.
        """
        pass

    def getAllTables(self, projectId: str) -> list:
        """
        Retrieves all table names for a given project by reading the project's metadata.json file from a remote location.

        Args:
            projectId (str): The unique identifier for the project.

        Returns:
            list: A list of table names available in the project.
        """
        metadata = readJson(projectId, "metadata.json")
        tables = [k for k, v in metadata.items() if not k.startswith("_") and v.get("isActive", True) is not False]
        return tables

    def getProfilingReport(self, projectId: str, tableName: str) -> str:
        """
        Generates a profiling HTML report for a specified table in a project, and post-processes the HTML to remove unnecessary UI elements.

        Args:
            projectId (str): The unique identifier for the project.
            tableName (str): The name of the table to generate the report for.

        Returns:
            str: The cleaned HTML profiling report as a string.
        """
        df = pd.read_parquet(io.BytesIO(readTableBytes(projectId, tableName)))
        htmlText = ProfileReport(df, title = f"Report for: {tableName}").to_html()
        soup = BeautifulSoup(htmlText, 'html.parser')
        soup.find("button", id = "tab-overview-reproduction").decompose()
        soup.find("div", id = "tab-pane-overview-reproduction").decompose()
        soup.find("p", class_ = "text-body-secondary text-end").decompose()
        soup.find("footer").decompose()
        return str(soup)