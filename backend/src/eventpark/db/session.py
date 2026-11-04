"""Database sessions.

A session is the "shopping cart" of a unit of work: it tracks the objects you load and change, and
`async with session.begin():` turns everything inside into ONE transaction (all or nothing).

Rule (CLAUDE.md §8): the *service* owns the transaction boundary, so routers only pass the session along.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit (we often return them as the API response);
    # with the default True, touching an attribute after commit would trigger a new query (or fail in async code).
    return async_sessionmaker(engine, expire_on_commit=False)
