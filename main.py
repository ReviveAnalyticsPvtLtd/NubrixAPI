"""
Main entry point for the NubrixAI FastAPI application.

This module initializes the FastAPI app, configures middleware, and includes all API routers for authentication, project management, data loading, blends, reporting, dashboard, and utilities.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"


import ssl
ssl._create_default_https_context = ssl._create_unverified_context

from api.routers import (
    admin,
    authentication,
    billingAdmin,
    blends,
    credits,
    dashboard,
    dataLoader,
    manager,
    reporting,
    subscriptions,
    tracking,
    transformations,
    utils,
    webhooks,
)
from api.adminErrors import AdminApiError, requestValidationErrors
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from utils.logger import logger
from utils.sandboxRunner import sweepOrphanedRuns
from fastapi.responses import ORJSONResponse
from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
import psutil
import threading
import os

app = FastAPI(
    title = "NubrixAI",
    summary = "API Endpoints for NubrixAI.",
    version = "1.0",
    root_path = "/api/latest",
    docs_url = "/documentation/docs",
    redoc_url = "/documentation/redoc"
)

def _isAdminRequest(request: Request) -> bool:
    path = request.scope.get("path", "")
    return (
        path == "/admin"
        or path.startswith("/admin/")
        or path == "/api/latest/admin"
        or path.startswith("/api/latest/admin/")
    )


@app.exception_handler(AdminApiError)
async def admin_exception_handler(_request: Request, exc: AdminApiError):
    content = {"message": exc.message}
    if exc.errors:
        content["errors"] = exc.errors
    return ORJSONResponse(status_code=exc.statusCode, content=content)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    if not _isAdminRequest(request):
        return await request_validation_exception_handler(request, exc)
    return ORJSONResponse(
        status_code=422,
        content={
            "message": "Validation failed",
            "errors": requestValidationErrors(exc.errors()),
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """
    Global exception handler for HTTPException.
    
    If the detail is a dict containing 'status' and 'message', it returns a 
    flat response. Otherwise, it returns the standard detail format.
    """
    if _isAdminRequest(request):
        if isinstance(exc.detail, dict):
            content = {"message": exc.detail.get("message", "Request failed")}
            if exc.detail.get("errors"):
                content["errors"] = exc.detail["errors"]
        else:
            content = {"message": str(exc.detail)}
        return ORJSONResponse(status_code=exc.status_code, content=content)
    if isinstance(exc.detail, dict) and "status" in exc.detail and "message" in exc.detail:
        return ORJSONResponse(
            status_code=exc.status_code,
            content=exc.detail
        )
    return ORJSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )


@app.middleware("http")
async def admin_unhandled_exception_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        if not _isAdminRequest(request):
            raise
        logger.error("Unhandled admin request failure: {}", type(exc).__name__)
        return ORJSONResponse(
            status_code=500,
            content={"message": "Internal server error"},
        )

app.add_middleware(
    CORSMiddleware,
    allow_origins = ["*"],
    allow_credentials = True,
    allow_methods = ["*"],
    allow_headers = ["*"],
)
app.add_middleware(
    GZipMiddleware, 
    minimum_size=1000, 
    compresslevel=5
)

@app.on_event("startup")
async def stats():
    """Log system stats at startup."""
    memory = psutil.virtual_memory()
    cpu_usage = psutil.cpu_percent(interval=1, percpu=True)
    totalUsage = psutil.cpu_percent(interval=1)
    logger.debug(f"RAM Usage Percentage: {memory.percent}%")
    logger.debug(f"Total CPU Utilization: {totalUsage}%")
    logger.debug(f"Total CPU Usage Per Core: {cpu_usage}")

@app.on_event("startup")
async def sweepSandboxRuns():
    """Clear what a killed worker's sandbox runs left. On a thread, because a uid that won't clear takes a while."""
    threading.Thread(target=sweepOrphanedRuns, daemon=True).start()

@app.on_event("shutdown")
async def shutdownShadowCharts():
    """Cancel the shadow chart runs still queued, so a recycled worker does not start them on its way out."""
    from api.services.reportingService import shutdownShadow
    shutdownShadow()

app.include_router(authentication.router, prefix = "/auth", tags = ["Authentication"])
app.include_router(manager.router, prefix = "/projects", tags = ["Project Management"])
app.include_router(dataLoader.router, prefix = "/loaders", tags = ["Data Loader"])
app.include_router(blends.router, prefix = "/blends", tags = ["Blends"])
app.include_router(reporting.router, prefix = "/reportingTool", tags = ["Reporting Tool"])
app.include_router(dashboard.router, prefix = "/dashboard", tags = ["Dashboard"])
app.include_router(utils.router, prefix = "/utils", tags = ["Utilities"])
app.include_router(subscriptions.router, prefix = "/subscriptions", tags = ["Subscriptions"])
app.include_router(webhooks.router, prefix = "/webhooks", tags = ["Webhooks"])
app.include_router(billingAdmin.router, prefix = "/billing-admin", tags = ["Billing Admin"])
app.include_router(transformations.router, prefix = "/transformations", tags = ["Transformations"])
app.include_router(credits.router, prefix = "/credits", tags = ["Credits"])
app.include_router(tracking.router, prefix = "/track", tags = ["Tracking"])
app.include_router(admin.router, prefix = "/admin", tags = ["Admin"])
