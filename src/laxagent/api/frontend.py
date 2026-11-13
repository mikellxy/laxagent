from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        # Unknown API paths must never be interpreted as static assets.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404)
        response = await super().get_response(path, scope)
        if path in (".", "", "index.html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def frontend_files(directory: Path) -> FrontendFiles:
    if not (directory / "index.html").is_file():
        raise ValueError("Frontend index.html is missing; build the frontend before starting")
    return FrontendFiles(directory=directory, html=True)
