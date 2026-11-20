from importlib.metadata import version
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

VERSION = version("baskets-server")

app = FastAPI(
    title="Baskets API",
    version=VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    redirect_slashes=False,
)


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    db: Literal["ok", "not_configured", "error"]


@app.get("/v1/health")
def health() -> Health:
    return Health(status="ok", version=VERSION, db="not_configured")
