import asyncio
import inspect
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal

from annotated_doc import Doc
from starlette.background import BackgroundTask as StarletteBackgroundTask
from starlette.background import BackgroundTasks as StarletteBackgroundTasks
from typing_extensions import ParamSpec

P = ParamSpec("P")

TaskStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class BackgroundTaskRecord:
    """
    Execution record for a single background task.

    One record is created per task when it is added to
    BackgroundTasks, and it is updated in place while the tasks run, so
    tests and monitoring code can observe exactly which tasks executed, which
    ones failed (and why), and which ones never ran.
    """

    def __init__(self, task: StarletteBackgroundTask) -> None:
        self.task = task
        self.status: TaskStatus = "pending"
        self.exception: BaseException | None = None

    @property
    def func(self) -> Callable[..., Any]:
        return self.task.func

    def __repr__(self) -> str:
        func_name = getattr(self.func, "__name__", repr(self.func))
        return f"<BackgroundTaskRecord {func_name} status={self.status}>"


class BackgroundTaskError(Exception):
    """
    Raised by BackgroundTasks after all tasks have been attempted, when one
    or more of them raised an exception.

    The response has already been sent to the client at this point, so this
    error can no longer be turned into an HTTP error response. It propagates
    to the server / monitoring layer instead, carrying the original
    exceptions in .exceptions and the per-task .records.
    """

    def __init__(self, records: Sequence[BackgroundTaskRecord]) -> None:
        self.records = list(records)
        self.exceptions = [record.exception for record in self.records]
        func_names = [
            getattr(record.func, "__name__", repr(record.func))
            for record in self.records
        ]
        super().__init__(
            f"{len(self.records)} background task(s) failed: {', '.join(func_names)}"
        )


class BackgroundTasks(StarletteBackgroundTasks):
    """
    A collection of background tasks that will be called after a response has been
    sent to the client.

    Tasks run sequentially, in the order they were added, and each task runs
    exactly once: calling a BackgroundTasks instance that already started
    running raises RuntimeError instead of re-running the tasks.

    If a task raises an exception, the failure is recorded (see
    BackgroundTasks.records), the remaining tasks still run, and once all
    tasks have been attempted a BackgroundTaskError is raised with all the
    failures, so the error reaches logging/monitoring instead of being
    silently swallowed. The response was already sent at that point, so it
    cannot be changed into an HTTP error.

    Cancellation (e.g. client disconnect or application shutdown) is not
    treated as a task failure: asyncio.CancelledError propagates
    immediately and the tasks that didn't run yet stay pending.

    Read more about it in the
    [FastAPI docs for Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/).

    ## Example

    ```python
    from fastapi import BackgroundTasks, FastAPI

    app = FastAPI()


    def write_notification(email: str, message=""):
        with open("log.txt", mode="w") as email_file:
            content = f"notification for {email}: {message}"
            email_file.write(content)


    @app.post("/send-notification/{email}")
    async def send_notification(email: str, background_tasks: BackgroundTasks):
        background_tasks.add_task(write_notification, email, message="some notification")
        return {"message": "Notification sent in the background"}
    ```
    """

    def __init__(
        self,
        tasks: Sequence[StarletteBackgroundTask] | None = None,
        *,
        on_error: Callable[[BackgroundTaskRecord], Any] | None = None,
    ) -> None:
        super().__init__(tasks)
        self.records: list[BackgroundTaskRecord] = [
            BackgroundTaskRecord(task) for task in self.tasks
        ]
        self.on_error = on_error
        self._started = False

    @property
    def started(self) -> bool:
        """
        True once the tasks started running (they cannot be run again).
        """
        return self._started

    def add_task(
        self,
        func: Annotated[
            Callable[P, Any],
            Doc(
                """
                The function to call after the response is sent.

                It can be a regular `def` function or an `async def` function.
                """
            ),
        ],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        """
        Add a function to be called in the background after the response is sent.

        Read more about it in the
        [FastAPI docs for Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/).
        """
        if self._started:
            raise RuntimeError(
                "Cannot add a background task: these background tasks already "
                "started running"
            )
        super().add_task(func, *args, **kwargs)
        self.records.append(BackgroundTaskRecord(self.tasks[-1]))

    def extend(self, tasks: Sequence[StarletteBackgroundTask]) -> None:
        """
        Append already created BackgroundTask objects, preserving order.

        Used internally by FastAPI to merge the background tasks collected
        through dependency injection with a background already attached to
        the Response returned by the endpoint, so that all tasks run once,
        in a single sequence.
        """
        if self._started:
            raise RuntimeError(
                "Cannot add a background task: these background tasks already "
                "started running"
            )
        for task in tasks:
            self.tasks.append(task)
            self.records.append(BackgroundTaskRecord(task))

    async def __call__(self) -> None:
        if self._started:
            raise RuntimeError(
                "BackgroundTasks can only be executed once, this instance was "
                "already called"
            )
        self._started = True
        failed_records: list[BackgroundTaskRecord] = []
        for record in self.records:
            record.status = "running"
            try:
                await record.task()
            except asyncio.CancelledError as exc:
                # Client disconnect or application shutdown: cancellation is
                # not a task failure, don't run the remaining tasks and don't
                # swallow the cancellation.
                record.status = "cancelled"
                record.exception = exc
                raise
            except Exception as exc:
                record.status = "failed"
                record.exception = exc
                failed_records.append(record)
                if self.on_error is not None:
                    callback_result = self.on_error(record)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            else:
                record.status = "succeeded"
        if failed_records:
            raise BackgroundTaskError(failed_records)
