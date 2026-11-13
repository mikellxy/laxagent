import json
from types import SimpleNamespace

import httpx2
import pytest

from laxagent.api.app import create_app
from laxagent.config import AppConfig, load_config


def test_frontend_path_is_relative_to_config(config_data, tmp_path):
    config_data["frontend"] = {"dist_dir": "../web-dist"}
    folder = tmp_path / "settings"
    folder.mkdir()
    path = folder / "config.json"
    path.write_text(json.dumps(config_data))
    config = load_config(path)
    assert config.frontend.dist_dir == str(tmp_path / "web-dist")
    with pytest.raises(ValueError, match="index.html is missing"):
        create_app(config)


@pytest.mark.anyio
async def test_static_mount_preserves_api_and_cache_headers(config_data, tmp_path):
    (tmp_path / "index.html").write_text("<html>frontend</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app-abc.js").write_text("export const app = 1")
    config_data["frontend"] = {"dist_dir": str(tmp_path)}
    app = create_app(AppConfig.model_validate(config_data))
    app.state.agent_context = SimpleNamespace(repository=None)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test") as api:
        for path in ("/", "/index.html"):
            response = await api.get(path)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
            cached = await api.get(path, headers={"If-None-Match": response.headers["etag"]})
            assert cached.status_code == 304 and cached.headers["cache-control"] == "no-cache"
        asset = await api.get("/assets/app-abc.js")
        assert asset.status_code == 200 and "etag" in asset.headers
        assert (await api.get("/assets/app-abc.js", headers={"If-None-Match": asset.headers["etag"]})).status_code == 304
        assert (await api.post("/api/v1/login", json={"user_id": "bad"})).status_code == 422
        assert (await api.get("/api/v1/projects")).status_code == 401
        assert (await api.post("/api/v1/users", json={})).status_code == 404
        assert (await api.get("/api/v1/missing")).status_code == 404
        assert (await api.get("/config/config.json")).status_code == 404
        assert (await api.get("/%2e%2e/config/config.json")).status_code == 404
        assert (await api.get("/openapi.json")).status_code == 200
