from collections.abc import Sequence
from typing import Any

from fastapi.params import Security


class AnonymousSecurityType:
    def __repr__(self) -> str:
        return "AnonymousSecurity()"


AnonymousSecurity = AnonymousSecurityType()


class SecurityRequirement:
    def __init__(
        self,
        *dependencies: Security,
        allow_anonymous: bool = False,
    ) -> None:
        if not dependencies:
            raise ValueError("A security requirement needs at least one dependency")
        if not all(isinstance(dependency, Security) for dependency in dependencies):
            raise TypeError("Security requirement dependencies must use Security()")
        self.dependencies: tuple[Security, ...] = tuple(dependencies)
        self.allow_anonymous = allow_anonymous


def is_security_requirement(value: Any) -> bool:
    return isinstance(value, SecurityRequirement)
