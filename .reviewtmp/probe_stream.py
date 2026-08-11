import asyncio
import tempfile
from pathlib import Path

import httpx2

from reaper.clients.base import IntegrationError
from reaper.clients.public import PublicClient


def handler(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(429, headers={"Retry-After": "17.5"}, content=b"slow down")


async def main() -> None:
    c = PublicClient("https://mirror.invalid")
    c._client = httpx2.AsyncClient(
        base_url="https://mirror.invalid",
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
    )
    with tempfile.TemporaryDirectory() as d:
        try:
            await c.stream_to("/data.tsv", Path(d) / "out")
        except IntegrationError as exc:
            print("raised:", exc)
            print("status:", exc.status, "retry_after:", exc.retry_after)
            print("__cause__:", repr(exc.__cause__))
        except Exception as exc:  # noqa: BLE001
            print("UNEXPECTED", type(exc).__name__, exc)
    await c._client.aclose()

    # transport failure chaining through stream_to
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("no route", request=request)

    c2 = PublicClient("https://mirror.invalid")
    c2._client = httpx2.AsyncClient(
        base_url="https://mirror.invalid",
        transport=httpx2.MockTransport(boom),
        follow_redirects=False,
    )
    with tempfile.TemporaryDirectory() as d:
        try:
            await c2.stream_to("/data.tsv", Path(d) / "out")
        except IntegrationError as exc:
            print("raised:", exc, "| cause:", type(exc.__cause__).__name__)
    await c2._client.aclose()


asyncio.run(main())
