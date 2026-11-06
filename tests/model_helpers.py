import asyncio
import json

import httpx2
from openai import AsyncOpenAI


def completed(text="hello"):
    return {"type": "response.completed", "response": {
        "id": "resp_test", "model": "model", "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text},
        ]}], "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
    }}


class ModelStream(httpx2.AsyncByteStream):
    def __init__(self, events, wait=None):
        self.events = events
        self.wait = wait
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        if self.wait is not None:
            self.wait.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def client_for(stream, captured, status=200):
    def handle(request):
        captured.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer placeholder"
        if status != 200:
            return httpx2.Response(status, json={"error": {"message": "secret-key"}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    return AsyncOpenAI(
        api_key="placeholder", base_url="http://router.test/v1", max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle), trust_env=False),
    )
