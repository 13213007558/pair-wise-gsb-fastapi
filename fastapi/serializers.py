"""Request-scoped response serialization helpers."""

import dataclasses
from collections.abc import Callable, Mapping
from collections import deque
from enum import Enum
from pathlib import PurePath
from types import GeneratorType
from typing import Any

from fastapi.encoders import ENCODERS_BY_TYPE, jsonable_encoder
from pydantic import BaseModel
from pydantic_core import PydanticUndefinedType

SERIALIZER_CONTEXT_SCOPE_KEY = "fastapi.serializer_context"

SerializerEncoder = Callable[[Any], Any]
SerializerEncoderMap = dict[type[Any], SerializerEncoder]


class _ExcludeNoneMatcher(type):
    target: type[Any]

    def __instancecheck__(cls, instance: Any) -> bool:
        return instance is not None and isinstance(instance, cls.target)


def _exclude_none_matcher(encoder_type: type[Any]) -> type[Any]:
    class MatcherKey(metaclass=_ExcludeNoneMatcher):
        target = encoder_type

    return MatcherKey


def _scoped_custom_encoders(
    custom_encoders: Mapping[type[Any], SerializerEncoder],
) -> SerializerEncoderMap:
    scoped_encoders: SerializerEncoderMap = {}
    for encoder_type, encoder in custom_encoders.items():
        if encoder_type is type(None):
            continue
        if isinstance(None, encoder_type):
            scoped_encoders[_exclude_none_matcher(encoder_type)] = encoder
        else:
            scoped_encoders[encoder_type] = encoder
    return scoped_encoders


def encode_with_scoped_encoders(
    obj: Any, custom_encoders: Mapping[type[Any], SerializerEncoder]
) -> Any:
    """Recursively encode *obj* without encoding containers wholesale."""
    if obj is None:
        return None
    if isinstance(obj, BaseModel):
        obj_dict = obj.model_dump(mode="python")
        return encode_with_scoped_encoders(obj_dict, custom_encoders)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj_dict = dataclasses.asdict(obj)
        return encode_with_scoped_encoders(obj_dict, custom_encoders)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, PydanticUndefinedType):
        return None

    if isinstance(obj, dict):
        encoded = {}
        for key, value in obj.items():
            encoded_key = encode_with_scoped_encoders(key, custom_encoders)
            if value is None:
                encoded[encoded_key] = None
            else:
                encoded[encoded_key] = encode_with_scoped_encoders(
                    value, custom_encoders
                )
        return encoded
    if isinstance(obj, (list, set, frozenset, GeneratorType, tuple, deque)):
        encoded = []
        for item in obj:
            if item is None:
                encoded.append(None)
            else:
                encoded.append(
                    encode_with_scoped_encoders(item, custom_encoders)
                )
        return encoded

    scoped_encoders = _scoped_custom_encoders(custom_encoders)
    if type(obj) in scoped_encoders:
        return scoped_encoders[type(obj)](obj)
    for encoder_type, encoder in scoped_encoders.items():
        if isinstance(obj, encoder_type):
            return encoder(obj)

    if type(obj) in ENCODERS_BY_TYPE:
        return ENCODERS_BY_TYPE[type(obj)](obj)
    for encoder_type, encoder in ENCODERS_BY_TYPE.items():
        if isinstance(obj, encoder_type):
            return encoder(obj)
    return jsonable_encoder(obj)


class SerializerContext:
    """Per-response Pydantic serialization context and encoder selection.

    FastAPI creates one instance for each request when an endpoint or one of
    its dependencies asks for this type. The instance is stored only in the
    current ASGI scope, so concurrent requests and background tasks never share
    temporary encoder configuration.

    The context is passed to Pydantic as ``context`` during response model
    serialization. Custom encoders are applied only after response validation
    and Pydantic serialization. Returning a ``Response`` instance or using a
    streaming response bypasses automatic application of this context; call
    :meth:`encode` explicitly in those responses when the same encoders are
    wanted.
    """

    context: dict[str, Any]
    custom_encoders: SerializerEncoderMap

    def __init__(
        self,
        *,
        context: Mapping[str, Any] | None = None,
        custom_encoders: Mapping[type[Any], SerializerEncoder] | None = None,
    ) -> None:
        self.context = dict(context or {})
        self.custom_encoders = dict(custom_encoders or {})

    def encode(self, obj: Any) -> Any:
        """Encode *obj* using this response's custom encoders."""
        return encode_with_scoped_encoders(obj, self.custom_encoders)
