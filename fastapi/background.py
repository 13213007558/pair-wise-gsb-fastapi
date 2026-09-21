import asyncio
from collections.abc import Callable, Sequence
from typing import Annotated, Any

from annotated_doc import Doc
from starlette._utils import is_async_callable
from starlette.background import BackgroundTask as StarletteBackgroundTask
from starlette.background import BackgroundTasks as StarletteBackgroundTasks
from starlette.concurrency import run_in_threadpool
from typing_extensions import ParamSpec

P = ParamSpec("P")
ErrorCallback = Callable[[StarletteBackgroundTask, BaseException], Any]


class BackgroundTasksError(Exception):
    """Raised when one or more background tasks fail after the response was sent."""

    def __init__(self, errors: Sequence[BaseException]) -> None:
        self.errors = list(errors)
        details = ", ".join(f"{type(error).__name__}: {error}" for error in self.errors)
        super().__init__(f"Background task failures: {details}")


class _RunOnceBackgroundTask(StarletteBackgroundTask):
    def __init__(
        self,
        task: StarletteBackgroundTask,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self.task = task
        self.is_async = True
        self.on_error = on_error
        self._has_run = False

    async def __call__(self) -> None:
        if self._has_run:
            return
        self._has_run = True
        await self.task()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.task, name)


async def _call_error_callback(
    callback: ErrorCallback,
    task: StarletteBackgroundTask,
    error: BaseException,
) -> None:
    if is_async_callable(callback):
        await callback(task, error)
    else:
        await run_in_threadpool(callback, task, error)


async def _dispatch_task_error(
    task: StarletteBackgroundTask,
    error: BaseException,
) -> None:
    callback = getattr(task, "on_error", None)
    if callback is not None:
        await _call_error_callback(callback, task, error)


def _as_run_once_tasks(
    background: StarletteBackgroundTask | Sequence[StarletteBackgroundTask],
    *,
    seen: set[int],
    on_error: ErrorCallback | None = None,
) -> list[_RunOnceBackgroundTask]:
    backgrounds = (
        [*background.tasks]
        if type(background) is BackgroundTasks
        or type(background) is StarletteBackgroundTasks
        else background
        if isinstance(background, Sequence)
        else [background]
    )

    run_once_tasks: list[_RunOnceBackgroundTask] = []
    for task in backgrounds:
        task_id = id(task)
        if task_id in seen:
            continue
        seen.add(task_id)
        if isinstance(task, _RunOnceBackgroundTask):
            if task.on_error is None and on_error is not None:
                task.on_error = on_error
            run_once_tasks.append(task)
        else:
            run_once_tasks.append(_RunOnceBackgroundTask(task, on_error=on_error))
    return run_once_tasks


def merge_background_tasks(
    *backgrounds: StarletteBackgroundTask | None,
) -> StarletteBackgroundTask | None:
    """Merge HTTP response backgrounds while preserving declaration order."""
    merged = BackgroundTasks(on_error=_dispatch_task_error)
    seen: set[int] = set()
    found = False

    for background in backgrounds:
        if background is None:
            continue
        found = True
        if not isinstance(background, StarletteBackgroundTask):
            raise TypeError("Response background must be a Starlette BackgroundTask")
        background_on_error = (
            background.on_error if type(background) is BackgroundTasks else None
        )
        merged.tasks.extend(
            _as_run_once_tasks(
                background,
                seen=seen,
                on_error=background_on_error,
            )
        )

    return merged if found else None


class BackgroundTasks(StarletteBackgroundTasks):
    """
    A collection of background tasks that will be called after a response has been
    sent to the client.

    Tasks are awaited serially in declaration order. A task that raises an
    exception invokes the optional ``on_error`` callback, after which the
    remaining tasks are still awaited. The recorded exception is reraised after
    the response has been sent, so it cannot replace the completed HTTP
    response. Cancellation is reraised immediately after cleanup.

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
        on_error: ErrorCallback | None = None,
    ) -> None:
        super().__init__()
        self.on_error = on_error
        if tasks:
            seen: set[int] = set()
            self.tasks.extend(_as_run_once_tasks(tasks, seen=seen))

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
        super().add_task(func, *args, **kwargs)
        self.tasks[-1] = _RunOnceBackgroundTask(self.tasks[-1])

    async def __call__(self) -> None:
        if getattr(self, "_has_run", False):
            return
        self._has_run = True

        errors: list[BaseException] = []
        for task in self.tasks:
            try:
                await task()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self.on_error is not None:
                    try:
                        await _call_error_callback(self.on_error, task, error)
                    except Exception as callback_error:
                        errors.extend((error, callback_error))
                    else:
                        errors.append(error)
                else:
                    errors.append(error)

        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise BackgroundTasksError(errors)
