from unittest.mock import Mock
from uuid import uuid4
import os

import pytest

from laxagent import layout
from laxagent.skills import MAX_SKILL_BYTES, initialize_skills, user_skill_list
from test_web_api import web_api, login


def make_skill(root, name, description="Test", extra=""):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n{extra}---\nBody\n")
    return directory


def test_list_only_reads_valid_entry_metadata(tmp_path):
    owner = str(uuid4())
    assert user_skill_list(owner) == []
    assert not layout.get_user_data_dir(owner).exists()
    initialize_skills(owner, None)
    root = layout.get_skills_dir(owner)
    manual = make_skill(root, "manual", "Manual purpose", "allow_auto_use: false\n")
    # Resources are deliberately not opened during list requests.
    (manual / "resource").symlink_to(tmp_path / "missing")
    make_skill(root, "automatic")
    make_skill(root, "mismatch").joinpath("SKILL.md").write_text("---\nname: other\ndescription: Bad\n---")
    make_skill(root, "large").joinpath("SKILL.md").write_bytes(b"a" * (MAX_SKILL_BYTES + 1))
    (root / "incomplete").mkdir()
    outside = make_skill(tmp_path / "outside", "linked")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    for kind in ("symlink", "hardlink", "fifo"):
        directory = root / kind
        directory.mkdir()
        path = directory / "SKILL.md"
        if kind == "symlink": path.symlink_to(manual / "SKILL.md")
        elif kind == "hardlink": os.link(manual / "SKILL.md", path)
        else: os.mkfifo(path)
    # The hardlink makes manual's entry invalid too, until it is removed.
    assert user_skill_list(owner) == [{"name": "automatic", "desc": "Test"}]
    (root / "hardlink" / "SKILL.md").unlink()
    assert user_skill_list(owner) == [
        {"name": "automatic", "desc": "Test"}, {"name": "manual", "desc": "Manual purpose"},
    ]


@pytest.mark.anyio
async def test_list_auth_isolation_and_read_only_missing_directory(web_api):
    assert (await web_api.get("/api/v1/skills")).status_code == 401
    first, second = str(uuid4()), str(uuid4())
    await login(web_api, first)
    make_skill(layout.get_skills_dir(first), "private-test", "Private", "allow_auto_use: false\n")
    response = await web_api.get("/api/v1/skills")
    assert response.status_code == 200
    skills = response.json()["skills"]
    assert {"name": "private-test", "desc": "Private"} in skills
    assert skills == sorted(skills, key=lambda skill: skill["name"])
    assert all(set(skill) == {"name", "desc"} for skill in skills)
    await login(web_api, second)
    assert "private-test" not in [skill["name"] for skill in (await web_api.get("/api/v1/skills")).json()["skills"]]
    root = layout.get_skills_dir(second)
    root.rename(root.with_name("saved-skills"))
    assert (await web_api.get("/api/v1/skills")).json() == {"skills": []}
    assert not root.exists()


@pytest.mark.anyio
async def test_list_root_failure_is_safe(web_api, monkeypatch):
    await login(web_api, str(uuid4()))
    monkeypatch.setattr("laxagent.api.skills.user_skill_list", Mock(side_effect=PermissionError("private path")))
    response = await web_api.get("/api/v1/skills")
    assert response.status_code == 503
    assert "private path" not in response.text


@pytest.mark.anyio
async def test_login_seeds_incrementally_and_retries_failure(web_api, repository, tmp_path, monkeypatch):
    from laxagent.config import AppConfig
    builtins = tmp_path / "builtins"
    make_skill(builtins, "first")
    monkeypatch.setattr(AppConfig, "builtin_skills_dir", property(lambda self: builtins))
    owner = str(uuid4())
    await login(web_api, owner)
    root = layout.get_skills_dir(owner)
    assert (root / "first" / "SKILL.md").is_file()
    (root / "first" / "SKILL.md").write_text("user edit")
    make_skill(builtins, "second")
    await login(web_api, owner)
    assert (root / "first" / "SKILL.md").read_text() == "user edit"
    assert (root / "second" / "SKILL.md").is_file()
    assert not (layout.get_user_data_dir(owner) / "project").exists()
    assert not (layout.get_user_data_dir(owner) / "session").exists()
    failed_owner = str(uuid4())
    with monkeypatch.context() as patch:
        patch.setattr("laxagent.runtime.profile.initialize_skills", Mock(side_effect=OSError("private path")))
        encode = Mock(side_effect=AssertionError("must not sign"))
        patch.setattr("laxagent.api.auth.jwt.encode", encode)
        response = await web_api.post("/api/v1/login", json={"user_id": failed_owner})
        assert response.status_code == 503 and "private path" not in response.text
        encode.assert_not_called()
    assert await repository.user_exists(failed_owner)
    await login(web_api, failed_owner)
    assert (layout.get_skills_dir(failed_owner) / "second" / "SKILL.md").exists()
