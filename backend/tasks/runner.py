"""Process-lifetime wakeups for the durable Todo execution queue.

APScheduler owns only the periodic wakeup. Task definitions, occurrence claims,
results and delivery receipts remain in the existing SQLite task store, so
restarting this scheduler cannot duplicate or erase user tasks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable


class ScheduledTaskRunner:
    def __init__(self, execute_next: Callable[[], Any], *,
                 write_log: Callable[..., None],
                 ready: Callable[[], bool] = lambda: True,
                 scheduler_factory: Callable[..., Any] | None = None) -> None:
        self.execute_next = execute_next
        self.write_log = write_log
        self.ready = ready
        self.scheduler_factory = scheduler_factory
        self._scheduler: Any = None
        self._execution_lock = Lock()

    def tick(self) -> Any:
        # Also covers an explicit test/manual tick racing with the scheduler.
        if not self.ready() or not self._execution_lock.acquire(blocking=False):
            return None
        try:
            return self.execute_next()
        except Exception as error:
            self.write_log('scheduled_task_dispatch_failed', errorType=type(error).__name__,
                           error=str(error)[:400])
            return None
        finally:
            self._execution_lock.release()

    def start(self) -> None:
        if self._scheduler is not None:
            return
        factory = self.scheduler_factory
        if factory is None:
            from apscheduler.schedulers.background import BackgroundScheduler
            factory = BackgroundScheduler
        scheduler = factory(timezone='UTC')
        scheduler.add_job(self.tick, 'interval', seconds=10,
                          id='amadeus-durable-task-dispatch', replace_existing=True,
                          max_instances=1, coalesce=True, misfire_grace_time=60,
                          next_run_time=datetime.now(timezone.utc))
        scheduler.start()
        self._scheduler = scheduler
        self.write_log('scheduled_task_runner_started', intervalSeconds=10)

    def stop(self) -> None:
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            # Interrupted tool executions retain their durable started receipt.
            scheduler.shutdown(wait=False)

