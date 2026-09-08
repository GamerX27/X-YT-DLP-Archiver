import os

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

router = APIRouter()

# Build the Jinja2 environment manually with cache_size=0 to work around a
# Python 3.14 incompatibility in Jinja2's LRU cache (unhashable dict in key tuple).
_templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
_jinja_env = Environment(
    loader=FileSystemLoader(_templates_dir),
    cache_size=0,
    autoescape=True,
)
templates = Jinja2Templates(env=_jinja_env)


@router.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers request /favicon.ico directly regardless of <link rel="icon">;
    # redirect to the real asset instead of returning a 404.
    return RedirectResponse(url="/static/favicon.ico")
