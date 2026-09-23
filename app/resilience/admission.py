import asyncio


class AdmissionController:
    def __init__(self, max_inflight: int, wait_ms: int) -> None:
        self._semaphore = asyncio.BoundedSemaphore(max_inflight)
        self._wait_seconds = wait_ms / 1000

    async def acquire(self) -> bool:
        if self._wait_seconds == 0:
            if self._semaphore.locked():
                return False
            await self._semaphore.acquire()
            return True
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._wait_seconds)
        except TimeoutError:
            return False
        return True

    def release(self) -> None:
        self._semaphore.release()
