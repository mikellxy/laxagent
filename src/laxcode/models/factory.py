from openai import AsyncOpenAI, DefaultAsyncHttpx2Client


def create_client(router_port: int) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=f"http://127.0.0.1:{router_port}/v1",
        api_key="placeholder",
        max_retries=0,
        timeout=300,
        http_client=DefaultAsyncHttpx2Client(trust_env=False, follow_redirects=False),
    )
