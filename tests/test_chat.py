import pytest
from fastapi.testclient import TestClient

from laxcode.main import app


def test_chat_parses_path_and_json_body():
    query = "  请解释这段代码\n保留原始输入。  "
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/sessions/session-123/chat", json={"query": query}
        )

    assert response.status_code == 200
    assert response.json() == {"session_id": "session-123", "query": query}


@pytest.mark.parametrize("body", [{}, {"query": None}, {"query": 123}, {"query": ""}])
def test_chat_rejects_invalid_query(body):
    with TestClient(app) as client:
        response = client.post("/api/v1/sessions/session-123/chat", json=body)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "query"]


def test_chat_requires_json_body():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/sessions/session-123/chat", params={"query": "hello"}
        )

    assert response.status_code == 422
