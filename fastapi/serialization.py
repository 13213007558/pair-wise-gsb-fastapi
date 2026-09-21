import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeAlias, cast

from fastapi.concurrency import run_in_threadpool
from fastapi.dependencies.models import _is_coroutine_callable
from fastapi.requests import Request

EncodersByType: TypeAlias = Mapping[type[Any], Callable[[Any], Any]]


@dataclasses.dataclass(frozen=True)
class SerializerContext:
    """
    An explicit, immutable serializer context for encoding a single response.

    A SerializerContext carries per-type encoders (e.g. tenant-specific
    Decimal/UUID rules) that are applied on top of FastAPI's default JSON
    encoders while serializing the response of one *path operation*.

    The context is deliberately immutable and is passed explicitly through
    the response serialization path: it is never stored in global or
    context-local state, so concurrent requests, async endpoints and
    background tasks never share temporary encoder state.

    The context only affects how the response of the current request is
    encoded. It does not affect:

    * OpenAPI schema generation (the schema is derived from response_model
      only).
    * response_model_include / response_model_exclude /
      response_model_by_alias and friends, which keep working independently.
    * Responses returned directly by the endpoint (Response,
      StreamingResponse, custom Response subclasses) and streamed (JSONL/SSE)
      items, which bypass response serialization entirely.
    """

    encoders: EncodersByType = dataclasses.field(default_factory=dict)

    def with_encoders(self, encoders: EncodersByType) -> "SerializerContext":
        """
        Return a new context with additional (or overridden) encoders.

        The original context is left untouched, so derived contexts can be
        built per tenant without mutating shared state.
        """
        return dataclasses.replace(self, encoders={**self.encoders, **encoders})


# A response serializer can be:
# * a SerializerContext applied to every response of the path operation, or
# * a (sync or async) callable receiving the current Request and returning
#   the SerializerContext to use for that response only (or None to fall
#   back to the default encoders). This is the recommended way to select an
#   encoder per tenant without touching any global state.
ResponseSerializer: TypeAlias = (
    SerializerContext
    | Callable[[Request], "SerializerContext | None"]
    | Callable[[Request], Awaitable["SerializerContext | None"]]
)


async def resolve_serializer_context(
    serializer: ResponseSerializer | None, request: Request
) -> SerializerContext | None:
    """
    Resolve the SerializerContext for the current request.

    Returns None when no serializer was configured or when the serializer
    callable opts out for this request, in which case FastAPI's default
    encoding path is used unchanged.
    """
    if serializer is None:
        return None
    if isinstance(serializer, SerializerContext):
        return serializer
    if _is_coroutine_callable(serializer):
        async_serializer = cast(
            Callable[[Request], Awaitable["SerializerContext | None"]], serializer
        )
        return await async_serializer(request)
    sync_serializer = cast(Callable[[Request], "SerializerContext | None"], serializer)
    return await run_in_threadpool(sync_serializer, request)
