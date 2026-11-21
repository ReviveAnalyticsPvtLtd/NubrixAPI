"""
exceptionHandler.py

This module defines application-specific exceptions with detailed context
for debugging and logging.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = [
    "ACCOUNT_ACCESS_REVOKED_ERROR_CODE",
    "ACCOUNT_ACCESS_REVOKED_MESSAGE",
    "CustomException",
    "accountAccessRevokedException",
    "raiseAccountAccessRevokedHttpException",
    "raiseHttpException",
    "raiseFeatureGateHttpException",
]


from fastapi import HTTPException
import traceback

from utils.logger import logger


ACCOUNT_ACCESS_REVOKED_ERROR_CODE = "ACCOUNT_ACCESS_REVOKED"
ACCOUNT_ACCESS_REVOKED_MESSAGE = (
    "Access to this account has been revoked. "
    "Please contact NubrixAI Support."
)

class CustomException(Exception):
    """
    CustomException provides detailed error information including
    the line number, file name, and original error message where
    the exception was raised.
    """
    def __init__(
        self,
        exception: Exception,
        statusCode: int = 500,
        uiMessage: str = "Something went wrong. Please try again later.",
        errorCode: str | None = None,
    ) -> None:
        """
        Initialize the CustomException with detailed traceback information.

        Args:
            exception (Exception): The original exception to wrap.
        """
        self.statusCode = statusCode
        self.uiMessage = uiMessage
        self.errorCode = errorCode
        tbList = traceback.extract_tb(exception.__traceback__) if exception.__traceback__ else []
        if tbList:
            tb = tbList[-1]
            lineNumber = tb.lineno
            fileName = tb.filename
            customErrorMessage = "Error encountered in line no: [{lineNumber}], filename: [{fileName}], saying: [{errorMessage}]"
            self.customErrorMessage = customErrorMessage.format(
                lineNumber = lineNumber,
                fileName = fileName,
                errorMessage = str(exception)
            )
        else:
            self.customErrorMessage = self.uiMessage
        super().__init__(self.customErrorMessage)

def raiseHttpException(e: CustomException):
    # The internal detail (file, line, exception text) goes to the logs, never to the browser.
    logger.log("ERROR" if e.statusCode >= 500 else "WARNING", e.customErrorMessage)
    detail = {
        "status": e.statusCode,
        "message": e.uiMessage,
    }
    if e.errorCode is not None:
        detail["errorCode"] = e.errorCode
    raise HTTPException(
        status_code=e.statusCode,
        detail=detail,
    )


def accountAccessRevokedException(userId: str) -> CustomException:
    return CustomException(
        PermissionError(f"Account access revoked for userId={userId}"),
        statusCode=403,
        uiMessage=ACCOUNT_ACCESS_REVOKED_MESSAGE,
        errorCode=ACCOUNT_ACCESS_REVOKED_ERROR_CODE,
    )


def raiseAccountAccessRevokedHttpException(userId: str) -> None:
    exception = accountAccessRevokedException(userId)
    raiseHttpException(exception)


def raiseFeatureGateHttpException(
    statusCode: int,
    uiMessage: str,
    backendLogMessage: str,
    errorCode: str,
) -> None:
    """
    Raise a feature-gate HTTPException using the same response shape as
    raiseHttpException, plus errorCode for frontend discrimination.
    backendLogMessage is logged, not returned.
    """
    logger.warning(backendLogMessage)
    raise HTTPException(
        status_code=statusCode,
        detail={
            "status": statusCode,
            "message": uiMessage,
            "errorCode": errorCode,
        },
    )
