import asyncio

import pytest

from app.resilience.admission import AdmissionController


@pytest.mark.asyncio
async def test_admission_rejects_when_the_wait_limit_expires() -> None:
    admission = AdmissionController(max_inflight=1, wait_ms=0)
    assert await admission.acquire() is True

    assert await admission.acquire() is False

    admission.release()


@pytest.mark.asyncio
async def test_admission_slot_is_released_when_a_task_is_cancelled() -> None:
    admission = AdmissionController(max_inflight=1, wait_ms=50)
    acquired = asyncio.Event()

    async def hold_slot() -> None:
        assert await admission.acquire() is True
        acquired.set()
        try:
            await asyncio.Event().wait()
        finally:
            admission.release()

    task = asyncio.create_task(hold_slot())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await admission.acquire() is True
    admission.release()
