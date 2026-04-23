"""Shared template environment + jinja context."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import formatting

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
formatting.register(templates.env)
