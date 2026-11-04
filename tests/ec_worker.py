"""Spawned worker for the multi-process coordination test (separate OS processes)."""
from __future__ import annotations

import asyncio
import os
import time

from tests.ec_helpers import Upstream, make_service, register
from app.episode_cache.service import FetchResult


def run_worker(args: tuple) -> list:
    db_path, series_id, episode_id, counter_file, start_at, n_requests, delay = args
    up = Upstream(delay=delay)

    orig = up.fetch

    async def counted(sid: str, eid: str) -> FetchResult:
        fd = os.open(counter_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(fd, f"{os.getpid()}:{sid}:{eid}\n".encode())  # atomic small append
        finally:
            os.close(fd)
        return await orig(sid, eid)

    up.fetch = counted  # type: ignore[assignment]
    register(up)

    async def main() -> list:
        svc = make_service(db_path, lock_wait_s=15.0)
        await svc.ensure_ready()
        while time.time() < start_at:
            await asyncio.sleep(0.005)
        res = await asyncio.gather(*[svc.get_episode("tp", series_id, episode_id)
                                     for _ in range(n_requests)])
        svc.client.close()
        return [(r.hit, r.source) for r in res]

    return asyncio.run(main())
