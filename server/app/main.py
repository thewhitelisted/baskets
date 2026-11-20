import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.bootstrap import build_service
from app.config import Settings
from app.ids import InvalidIdsError, parse_id_list
from app.providers.base import ProviderError
from app.service import PerIdResult, Service

VERSION = version("baskets-server")
ALLOWED_ORIGINS = ["https://baskets.christopherjlee.ca", "http://localhost:5173"]
CACHEABLE = "public, max-age=3600"
NOT_CACHEABLE = "no-store"
DATA_LIMIT = "120/minute"
SEARCH_LIMIT = "60/minute"

log = logging.getLogger(__name__)


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    db: Literal["ok", "not_configured", "error"]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(
    status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


def data_response(body: dict[str, Any], cacheable: bool) -> JSONResponse:
    return JSONResponse(body, headers={"Cache-Control": CACHEABLE if cacheable else NOT_CACHEABLE})


def _ids(raw: str) -> list[str]:
    try:
        return parse_id_list(raw)
    except InvalidIdsError as e:
        raise ApiError(400, "bad_request", str(e)) from e


def create_app(service_factory: Callable[[], Service] | None = None) -> FastAPI:
    """App with its service built at startup (production) or by `service_factory` (tests)."""
    factory = service_factory or (lambda: build_service(Settings.from_env()))
    holder: dict[str, Service] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        service = factory()
        try:
            service.store.init()
        except Exception:
            log.exception("database initialisation failed; /v1/health reports db=error")
        holder["service"] = service
        yield

    def svc() -> Service:
        return holder["service"]

    app = FastAPI(
        title="Baskets API",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_middleware(
        CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET"], allow_headers=[]
    )

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        message = "unknown route" if exc.status_code == 404 else str(exc.detail)
        return error_response(
            exc.status_code, codes.get(exc.status_code, "http_error"), message, exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "query")
        return error_response(400, "bad_request", f"{where}: {first.get('msg', 'invalid')}")

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        return error_response(429, "rate_limited", f"rate limit exceeded: {exc.detail}")

    @app.exception_handler(Exception)
    async def internal_error(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", exc_info=exc)
        return error_response(500, "internal", "internal server error")

    @app.get("/v1/health")
    def health() -> JSONResponse:
        db: Literal["ok", "error"] = "ok" if svc().store.check() else "error"
        body = Health(status="ok", version=VERSION, db=db).model_dump()
        return JSONResponse(body, headers={"Cache-Control": NOT_CACHEABLE})

    @app.get("/v1/series")
    @limiter.limit(DATA_LIMIT)
    def series(
        request: Request, ids: Annotated[str, Query()], base: Annotated[str, Query()] = "USD"
    ) -> JSONResponse:
        if base != "USD":
            raise ApiError(400, "bad_request", f"base must be USD, got {base!r}")
        result = svc().series(_ids(ids))
        return _per_id(result, "series", extra={"base": "USD"})

    @app.get("/v1/meta")
    @limiter.limit(DATA_LIMIT)
    def meta(request: Request, ids: Annotated[str, Query()]) -> JSONResponse:
        return _per_id(svc().meta(_ids(ids)), "meta")

    @app.get("/v1/shares")
    @limiter.limit(DATA_LIMIT)
    def shares(request: Request, ids: Annotated[str, Query()]) -> JSONResponse:
        return _per_id(svc().shares(_ids(ids)), "shares")

    @app.get("/v1/search")
    @limiter.limit(SEARCH_LIMIT)
    def search(
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=64)],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> JSONResponse:
        try:
            results = svc().search(q, limit)
        except ProviderError as e:
            raise ApiError(502, e.code, str(e)) from e
        return data_response({"results": results}, cacheable=True)

    @app.get("/v1/ended")
    @limiter.limit(DATA_LIMIT)
    def ended(request: Request) -> JSONResponse:
        return data_response({"ended": svc().ended()}, cacheable=True)

    return app


def _per_id(
    result: PerIdResult[Any], key: str, extra: dict[str, Any] | None = None
) -> JSONResponse:
    body = {**(extra or {}), key: result.values, "errors": result.errors}
    return data_response(body, result.cacheable)


app = create_app()
