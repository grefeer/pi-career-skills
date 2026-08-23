"""Bounded-concurrency batch runner with i/n progress lines (C5).

docs/findjobs-optimization-plan.zh-CN.md §7 C5.  Long batches (public page
fetch) become observable: a monotonically increasing ``i/n done`` line per
completion, bounded concurrency (default 4), and deterministic results —
always returned in input-index order regardless of completion order.  A
failing item is isolated into ``BatchResult.error`` and never aborts the
batch (the caller decides how to surface it), so the batch tool semantics
stay identical to the sequential loop it replaces.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

Item = TypeVar("Item")
Value = TypeVar("Value")


@dataclass
class BatchResult(Generic[Item, Value]):
    """One item's outcome; ``error`` is set when ``work`` raised."""

    index: int
    item: Item
    value: Value | None = None
    error: BaseException | None = None


def run_parallel_with_progress(
    items: Sequence[Item],
    work: Callable[[Item], Value],
    *,
    workers: int = 4,
    label: str = "item",
    key: Callable[[Item], str] = str,
    progress: Callable[[str], None] = logger.info,
) -> list[BatchResult[Item, Value]]:
    """Run ``work`` over ``items`` with bounded concurrency + progress lines.

    - concurrency is capped at ``workers`` (no unbounded fan-out);
    - each completion emits ``<done>/<total> done <label>=<key>`` (monotone);
    - results are returned sorted by input index (deterministic);
    - a raising item becomes ``BatchResult.error``; the batch continues.
    """
    results: dict[int, BatchResult[Item, Value]] = {}
    total = len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_to_index = {
            executor.submit(work, item): index for index, item in enumerate(items)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            item = items[index]
            try:
                results[index] = BatchResult(index=index, item=item, value=future.result())
            except BaseException as exc:  # noqa: BLE001 - item isolation is the point
                results[index] = BatchResult(index=index, item=item, error=exc)
            done += 1
            progress(f"{done}/{total} done {label}={key(item)}")
    return [results[index] for index in sorted(results)]
