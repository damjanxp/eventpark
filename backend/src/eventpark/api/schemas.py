"""Schemas shared by several routers."""

from pydantic import BaseModel


class Page[T](BaseModel):
    """One page of a cursor-paginated list. Pass `next_cursor` as `?cursor=` to get the next page (None = last)."""

    items: list[T]
    next_cursor: str | None
