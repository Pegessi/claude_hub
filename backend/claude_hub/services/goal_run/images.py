"""Request-scoped Goal images: originals never enter the Goal JSON snapshot.

The controller may retain a bounded review-time batch until delayed dispatch.
An interrupted dispatch
retains a small marker and fails closed until the user reattaches the images.
Accepted image previews use the ordinary shared Chat attachment LRU.
"""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ...models.chat_image import ChatImageAttachment
from ...models.goal_run import GoalRun

_inputs: ContextVar[tuple[str, str, list[ChatImageAttachment]] | None] = ContextVar(
    "goal_images", default=None
)


def image_fingerprint(images: list[ChatImageAttachment]) -> str:
    if not images:
        return ""
    return hashlib.sha256(
        json.dumps([image.model_dump() for image in images], sort_keys=True).encode()
    ).hexdigest()


@contextmanager
def goal_image_input(tab_id: str, images: list[ChatImageAttachment]) -> Iterator[None]:
    scoped = list(images)
    token = _inputs.set((tab_id, image_fingerprint(images), scoped))
    try:
        yield
    finally:
        # Native providers start long-lived reader tasks inside this context.
        # They inherit the same holder; release its payload in every context.
        scoped.clear()
        _inputs.reset(token)


def pending_goal_images(goal: GoalRun) -> list[ChatImageAttachment]:
    if not goal.pending_image_count:
        return []
    supplied = _inputs.get()
    if (
        supplied is None
        or supplied[:2] != (goal.tab_id, goal.pending_image_fingerprint)
        or len(supplied[2]) != goal.pending_image_count
    ):
        raise ValueError(
            "Image delivery interrupted. Reattach images and steer the Goal to continue."
        )
    return supplied[2]


def combine_review_images(
    retained: list[ChatImageAttachment], incoming: list[ChatImageAttachment]
) -> list[ChatImageAttachment]:
    """Bound the combined batch; each request already passed attachment validation."""
    images = retained + incoming
    if len(images) > 10:
        raise ValueError("Queued Goal images are limited to 10 attachments")
    # Match the transport's 40 MiB original+preview limit without decoding the
    # already-validated payload again while holding the Goal mutation lock.
    size = 0
    for image in images:
        for url in (image.data_url, image.preview_data_url):
            if url:
                payload = url.split(",", 1)[1]
                size += len(payload) * 3 // 4 - (len(payload) - len(payload.rstrip("=")))
    if size > 40 * 1024 * 1024:
        raise ValueError("Queued Goal images are limited to 40 MiB including previews")
    return [image.model_copy(deep=True) for image in images]
