"""Database sessions.

A session is the "shopping cart" of a unit of work: it tracks the objects you load and change, and
`async with session.begin():` turns everything inside into ONE transaction (all or nothing).

Rule (CLAUDE.md §8): the *service* owns the transaction boundary, so routers only pass the session along.
"""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState, Session


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit (we often return them as the API response);
    # with the default True, touching an attribute after commit would trigger a new query (or fail in async code).
    return async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(Session, "do_orm_execute")
def _locked_reads_are_fresh(state: ORMExecuteState) -> None:
    """Every SELECT ... FOR UPDATE refreshes the objects it returns (populate_existing).

    Because of expire_on_commit=False, a session can still hold an object from an EARLIER transaction. A locking
    select then waits for the row lock correctly, but by default returns that cached object, with the values from
    before the other transaction committed. Code that checks "already succeeded?" under the lock would see the old
    answer. The Phase 9 chaos run caught exactly this (an exit payment counted twice), so the rule is global:
    a row you just locked is always read from the database.
    """
    if state.is_select and getattr(state.statement, "_for_update_arg", None) is not None:
        state.update_execution_options(populate_existing=True)
