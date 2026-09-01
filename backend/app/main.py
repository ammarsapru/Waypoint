from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from backend.app.api.routes import router
from backend.app.agent.provider import model_provider_router
from backend.app.config import settings
from backend.app.observability import (
    configure_logging,
    install_max_call_tracer,
    log_event,
    process_metadata,
    trace_context,
)

configure_logging()
install_max_call_tracer()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log_event(
        logger,
        logging.INFO,
        "process.startup",
        "Application process starting",
        app=settings.app_name,
        process=process_metadata(),
        allowed_root=settings.allowed_root,
        max_repository_files=settings.max_repository_files,
        max_python_file_bytes=settings.max_python_file_bytes,
        max_unresolved_call_details=settings.max_unresolved_call_details,
    )
    try:
        provider = await run_in_threadpool(model_provider_router.initialize)
    except Exception as exc:
        provider = "unavailable"
        log_event(
            logger,
            logging.ERROR,
            "model.provider_initialization_failed",
            "No configured model provider passed its startup check",
            exception_type=type(exc).__name__,
            exception_message=str(exc),
        )
    log_event(
        logger,
        logging.INFO,
        "process.ready",
        "Application startup complete and ready to receive requests",
        app=settings.app_name,
        model_provider=provider,
    )
    try:
        yield
    finally:
        log_event(
            logger,
            logging.INFO,
            "process.shutdown",
            "Application process shutting down",
            app=settings.app_name,
        )


app = FastAPI(
    title="Adaptive Codebase Onboarding API",
    version="0.1.0",
    lifespan=lifespan,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Server-Timing"],
    )


@app.middleware("http")
async def diagnostic_request_middleware(
    request: Request, call_next: object
) -> Response:
    supplied_request_id = request.headers.get("x-request-id", "")
    request_id = (
        supplied_request_id[:128]
        if supplied_request_id and supplied_request_id.isascii()
        else uuid.uuid4().hex
    )
    started = time.perf_counter()
    with trace_context(trace_id=request_id):
        log_event(
            logger,
            logging.INFO,
            "http.request_received",
            "HTTP request received",
            method=request.method,
            path=request.url.path,
            query_keys=sorted(request.query_params.keys()),
            client_host=request.client.host if request.client else None,
            content_length=request.headers.get("content-length"),
            content_type=request.headers.get("content-type"),
            user_agent=request.headers.get("user-agent"),
        )
        try:
            response = await call_next(request)  # type: ignore[operator]
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "http.request_failed",
                "Unhandled exception while processing HTTP request",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                exception_type=type(exc).__name__,
                exception_message=str(exc),
            )
            logger.exception("Full HTTP request failure stack")
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        response.headers["x-request-id"] = request_id
        response.headers["server-timing"] = f"app;dur={duration_ms:.3f}"
        log_event(
            logger,
            logging.INFO,
            "http.response_sent",
            "HTTP response completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            response_content_type=response.headers.get("content-type"),
            response_content_length=response.headers.get("content-length"),
        )
        return response


app.include_router(router)

frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    log_event(
        logger,
        logging.INFO,
        "process.frontend_mounted",
        "Production frontend bundle mounted by FastAPI",
        directory=frontend_dist,
    )
else:
    log_event(
        logger,
        logging.INFO,
        "process.frontend_not_mounted",
        "Frontend distribution directory does not exist; use the Vite dev server",
        expected_directory=frontend_dist,
    )
