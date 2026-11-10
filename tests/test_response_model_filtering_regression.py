"""
Regression tests: a declared ``response_model`` must filter internal-only
fields (password digests, admin notes) out of the final HTTP body on every
normal return path: dicts, model instances, subclasses with extra fields,
unions, and nested collections, including field aliases and
include/exclude combinations.

Expectations are checked against a hand-written manifest of public fields
(``PUBLIC_ENDPOINT_FIELDS``) plus hand-written expected bodies derived from
the public model declarations - never from the current encoder output - so
a filtering regression cannot silently drag the expectations along with it.
Raw response text is additionally scanned for fake leak markers.

Paths that bypass ``response_model`` by contract (returning ``Response`` /
``JSONResponse`` / ``StreamingResponse`` directly, and exception handlers)
are intentionally NOT covered here; see
tests/test_response_model_filtering_bypass_paths.py.
"""

import pytest
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

# Searchable fake markers for sensitive content. These never appear in a
# correctly filtered response body.
FAKE_PASSWORD_DIGEST = "FAKE-PASSWORD-DIGEST{sha256$9f3c2d7e}"
FAKE_ADMIN_NOTE = "FAKE-ADMIN-NOTE{internal-only-7d21}"
LEAK_MARKERS = (FAKE_PASSWORD_DIGEST, FAKE_ADMIN_NOTE)


class PublicUser(BaseModel):
    username: str
    email: str


class AliasedPublicUser(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    username: str = Field(alias="userName")
    email: str


class PublicTeam(BaseModel):
    name: str
    members: list[PublicUser]


class PublicProfile(BaseModel):
    username: str
    email: str
    full_name: str


class UserRecord(BaseModel):
    """Internal record: public fields plus sensitive internals."""

    username: str
    email: str
    password_digest: str
    admin_note: str


class InternalUser(PublicUser):
    """Subclass of the public model carrying extra internal fields."""

    password_digest: str
    admin_note: str


class TeamRecord(BaseModel):
    name: str
    members: list[UserRecord]
    admin_note: str


def make_user_record(username: str = "jdoe", email: str = "jdoe@example.com"):
    return UserRecord(
        username=username,
        email=email,
        password_digest=FAKE_PASSWORD_DIGEST,
        admin_note=FAKE_ADMIN_NOTE,
    )


app = FastAPI()


@app.get("/dict", response_model=PublicUser)
def get_dict():
    record = make_user_record()
    return {
        "username": record.username,
        "email": record.email,
        "password_digest": record.password_digest,
        "admin_note": record.admin_note,
    }


@app.get("/model", response_model=PublicUser)
def get_model():
    return make_user_record()


@app.get("/subclass", response_model=PublicUser)
def get_subclass():
    return InternalUser(
        username="jdoe",
        email="jdoe@example.com",
        password_digest=FAKE_PASSWORD_DIGEST,
        admin_note=FAKE_ADMIN_NOTE,
    )


@app.get("/union", response_model=PublicUser | PublicTeam)
def get_union(kind: str = "user"):
    if kind == "team":
        return TeamRecord(
            name="core",
            members=[make_user_record()],
            admin_note=FAKE_ADMIN_NOTE,
        )
    return make_user_record()


@app.get("/nested-list", response_model=list[PublicUser])
def get_nested_list():
    return [make_user_record("jdoe"), make_user_record("asmith", "asmith@example.com")]


@app.get("/nested-dict", response_model=dict[str, PublicUser])
def get_nested_dict():
    return {"admin": make_user_record()}


@app.get("/nested-object", response_model=PublicTeam)
def get_nested_object():
    return TeamRecord(
        name="core",
        members=[make_user_record()],
        admin_note=FAKE_ADMIN_NOTE,
    )


@app.get("/alias", response_model=AliasedPublicUser)
def get_alias():
    return make_user_record()


@app.get(
    "/alias-by-name", response_model=AliasedPublicUser, response_model_by_alias=False
)
def get_alias_by_name():
    return make_user_record()


@app.get(
    "/include-exclude",
    response_model=PublicProfile,
    response_model_include={"username", "email", "full_name"},
    response_model_exclude={"full_name"},
)
def get_include_exclude():
    return {
        "username": "jdoe",
        "email": "jdoe@example.com",
        "full_name": "John Doe",
        "password_digest": FAKE_PASSWORD_DIGEST,
        "admin_note": FAKE_ADMIN_NOTE,
    }


@app.get(
    "/nested-include-exclude",
    response_model=PublicTeam,
    response_model_include={"name", "members"},
    response_model_exclude={"members": {"__all__": {"email"}}},
)
def get_nested_include_exclude():
    return TeamRecord(
        name="core",
        members=[make_user_record()],
        admin_note=FAKE_ADMIN_NOTE,
    )


@app.get("/broken", response_model=PublicUser)
def get_broken():
    # Missing the required public field "email"; must fail validation
    # instead of falling back to sending the raw object.
    return {
        "username": "jdoe",
        "password_digest": FAKE_PASSWORD_DIGEST,
        "admin_note": FAKE_ADMIN_NOTE,
    }


# Independent, hand-written manifest of the only field names each endpoint
# may ever expose (at any nesting level). Derived from the public model
# declarations above, not from observed encoder output.
PUBLIC_ENDPOINT_FIELDS: dict[str, set[str]] = {
    "/dict": {"username", "email"},
    "/model": {"username", "email"},
    "/subclass": {"username", "email"},
    "/union": {"username", "email", "name", "members"},
    "/nested-list": {"username", "email"},
    "/nested-dict": {"username", "email"},
    "/nested-object": {"name", "members", "username", "email"},
    "/alias": {"userName", "email"},
    "/alias-by-name": {"username", "email"},
    "/include-exclude": {"username", "email"},
    "/nested-include-exclude": {"name", "members", "username"},
}


def collect_keys(body, skip_top_level: bool = False, _depth: int = 0):
    keys = set()
    if isinstance(body, dict):
        for key, value in body.items():
            if not (skip_top_level and _depth == 0):
                keys.add(key)
            keys |= collect_keys(value, skip_top_level, _depth + 1)
    elif isinstance(body, list):
        for item in body:
            keys |= collect_keys(item, skip_top_level, _depth)
    return keys


# Endpoints whose top-level keys are dynamic data (e.g. dict[str, Model]),
# not field names; only nested keys are checked against the manifest.
DYNAMIC_TOP_LEVEL_KEYS = {"/nested-dict"}


def assert_public_response(response, path: str, expected):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == expected
    keys = collect_keys(body, skip_top_level=path in DYNAMIC_TOP_LEVEL_KEYS)
    unexpected = keys - PUBLIC_ENDPOINT_FIELDS[path]
    assert not unexpected, f"{path} exposed non-public fields: {unexpected}"
    for marker in LEAK_MARKERS:
        assert marker not in response.text, f"{path} leaked marker {marker!r}"


client = TestClient(app)


def test_dict_return_is_filtered():
    response = client.get("/dict")
    assert_public_response(
        response, "/dict", {"username": "jdoe", "email": "jdoe@example.com"}
    )


def test_model_instance_return_is_filtered():
    response = client.get("/model")
    assert_public_response(
        response, "/model", {"username": "jdoe", "email": "jdoe@example.com"}
    )


def test_subclass_with_extra_fields_is_filtered():
    response = client.get("/subclass")
    assert_public_response(
        response, "/subclass", {"username": "jdoe", "email": "jdoe@example.com"}
    )


def test_union_user_branch_is_filtered():
    response = client.get("/union")
    assert_public_response(
        response, "/union", {"username": "jdoe", "email": "jdoe@example.com"}
    )


def test_union_team_branch_is_filtered_recursively():
    response = client.get("/union", params={"kind": "team"})
    assert_public_response(
        response,
        "/union",
        {
            "name": "core",
            "members": [{"username": "jdoe", "email": "jdoe@example.com"}],
        },
    )


def test_nested_list_is_filtered():
    response = client.get("/nested-list")
    assert_public_response(
        response,
        "/nested-list",
        [
            {"username": "jdoe", "email": "jdoe@example.com"},
            {"username": "asmith", "email": "asmith@example.com"},
        ],
    )


def test_nested_dict_is_filtered():
    response = client.get("/nested-dict")
    assert_public_response(
        response,
        "/nested-dict",
        {"admin": {"username": "jdoe", "email": "jdoe@example.com"}},
    )


def test_nested_object_is_filtered_recursively():
    response = client.get("/nested-object")
    assert_public_response(
        response,
        "/nested-object",
        {
            "name": "core",
            "members": [{"username": "jdoe", "email": "jdoe@example.com"}],
        },
    )


def test_alias_used_by_default_and_internal_fields_filtered():
    response = client.get("/alias")
    assert_public_response(
        response, "/alias", {"userName": "jdoe", "email": "jdoe@example.com"}
    )


def test_alias_disabled_and_internal_fields_filtered():
    response = client.get("/alias-by-name")
    assert_public_response(
        response,
        "/alias-by-name",
        {"username": "jdoe", "email": "jdoe@example.com"},
    )


def test_include_exclude_combination_is_filtered():
    response = client.get("/include-exclude")
    assert_public_response(
        response, "/include-exclude", {"username": "jdoe", "email": "jdoe@example.com"}
    )


def test_nested_include_exclude_combination_is_filtered():
    response = client.get("/nested-include-exclude")
    assert_public_response(
        response,
        "/nested-include-exclude",
        {"name": "core", "members": [{"username": "jdoe"}]},
    )


def test_invalid_return_raises_instead_of_sending_raw_object():
    with pytest.raises(ResponseValidationError):
        client.get("/broken")


def test_invalid_return_500_body_contains_no_internal_data():
    unraised_client = TestClient(app, raise_server_exceptions=False)
    response = unraised_client.get("/broken")
    assert response.status_code == 500, response.text
    for marker in LEAK_MARKERS:
        assert marker not in response.text
    # The raw, unfiltered object must not fall back into the error body.
    assert "jdoe" not in response.text
