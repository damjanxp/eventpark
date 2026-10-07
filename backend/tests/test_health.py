from httpx import ASGITransport, AsyncClient

from eventpark.app import create_app
from eventpark.core.config import Settings


async def test_healthz_returns_ok() -> None:
    app = create_app(Settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
