import time
import uuid

from eventpark.core.ids import uuid7, uuid7_timestamp_ms


def test_is_a_version_7_rfc_uuid() -> None:
    value = uuid7()

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_embeds_the_current_time() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000

    assert before <= uuid7_timestamp_ms(value) <= after


def test_ids_created_later_sort_later() -> None:
    first = uuid7()
    time.sleep(0.002)  # different millisecond (within the same ms the order is random)
    second = uuid7()

    assert first < second
    assert str(first) < str(second)  # also as text, e.g. when sorted in logs


def test_ids_are_unique() -> None:
    ids = {uuid7() for _ in range(10_000)}

    assert len(ids) == 10_000
