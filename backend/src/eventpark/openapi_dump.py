"""`python -m eventpark.openapi_dump [path]`: write the API's OpenAPI document as JSON (default: stdout).

The frontend's typed client is generated from it (`make openapi`). No server, database or Redis is needed: the
document is built from the route definitions alone. CI regenerates it and fails if the committed copy is stale, so
an API change that would break the frontend breaks the build instead of production.
"""

import json
import sys

from eventpark.app import create_app
from eventpark.core.config import Settings


def main() -> None:
    settings = Settings(  # dummy values: only the routes matter
        database_url="postgresql+asyncpg://x:x@localhost:5432/x",
        redis_url="redis://localhost:6379/0",
        jwt_secret="openapi-dump-not-a-secret-0123456789abcdef",  # noqa: S106
        fakepay_webhook_secret="openapi-dump-not-a-secret",  # noqa: S106
    )
    document = json.dumps(create_app(settings).openapi(), indent=2, sort_keys=True) + "\n"
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8") as f:
            f.write(document)
    else:
        sys.stdout.write(document)


if __name__ == "__main__":
    main()
