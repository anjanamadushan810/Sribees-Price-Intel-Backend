import json
import logging

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.adapters import botauth
from app.api import admin, auth, market, pricing
from app.core.config import get_settings
from app.core.db import get_engine
from app.services.errors import IntelError

log = logging.getLogger("intel")

# SQLSTATEs raised by the constraints and triggers in alembic/versions/0001.
_DB_RULE_ERRORS = {
    "23514": 422,  # check_violation    - below floor, bad status, price <= 0
    "23001": 409,  # restrict_violation - audit log / price history touched
    "23505": 409,  # unique_violation   - second live match, second pending suggestion
}


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Sribees Price Intel",
        version="0.1.0",
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(IntelError)
    async def intel_error(_: Request, exc: IntelError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(DBAPIError)
    async def database_rule(_: Request, exc: DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        status = _DB_RULE_ERRORS.get(sqlstate or "")
        if status is None:
            log.exception("database error")
            return JSONResponse(status_code=500, content={"detail": "internal error"})
        message = str(exc.orig).split("\n")[0]
        if ":" in message and "ERROR" in message[:40]:
            message = message.split(":", 1)[1].strip()
        return JSONResponse(status_code=status, content={"detail": message})

    @app.get("/health", include_in_schema=False)
    async def health():
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get(botauth.DIRECTORY_PATH, include_in_schema=False)
    async def key_directory(request: Request):
        """Our Web Bot Auth public key, for shops that check our signatures."""
        signed = botauth.directory(request.headers.get("host", ""))
        if signed is None:
            return JSONResponse(status_code=404, content={"detail": "signing is off"})
        body, headers = signed
        return Response(content=json.dumps(body), media_type=botauth.DIRECTORY_TYPE,
                        headers=headers)

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(market.router)
    app.include_router(pricing.router)
    return app


app = create_app()
