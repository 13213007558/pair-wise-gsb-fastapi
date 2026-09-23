"""
Tests for Form/File multi-value semantics: aliases, nested models, union
types, query/body isolation, dependency cache consistency and Content-Type
error handling.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Form, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

app = FastAPI()


class AliasedForm(BaseModel):
    user_name: str = Field(alias="username")
    tags: list[str] = []


@app.post("/alias-strict")
def alias_strict(data: Annotated[AliasedForm, Form(strict=True)]):
    return {"user_name": data.user_name, "tags": data.tags}


class CatForm(BaseModel):
    cat_name: str
    lives: int = 9


class DogForm(BaseModel):
    dog_name: str
    good_boy: bool = True


@app.post("/union-form")
def union_form(data: Annotated[CatForm | DogForm, Form()]):
    return data


@app.post("/query-and-form")
def query_and_form(
    q: Annotated[str, Query()],
    data: Annotated[AliasedForm, Form()],
):
    return {"q": q, "user_name": data.user_name, "tags": data.tags}


@app.post("/same-alias")
def same_alias(
    query_value: Annotated[str, Query(alias="shared")],
    form_value: Annotated[str, Form(alias="shared")],
):
    return {"query_value": query_value, "form_value": form_value}


dependency_calls = []


def form_dependency(data: Annotated[AliasedForm, Form()]):
    dependency_calls.append(data.user_name)
    return data


@app.post("/cached-dependency")
def cached_dependency(
    first: Annotated[AliasedForm, Depends(form_dependency)],
    second: Annotated[AliasedForm, Depends(form_dependency)],
):
    return {"first": first.user_name, "second": second.user_name}


@app.post("/json-endpoint")
def json_endpoint(data: AliasedForm):
    return {"user_name": data.user_name}


client = TestClient(app)


def test_alias_duplicate_strict_error_uses_alias():
    response = client.post("/alias-strict", data={"username": ["a", "b"]})
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "multiple_values",
                "loc": ["body", "username"],
                "msg": "Expected a single value, but got 2 values",
                "input": ["a", "b"],
                "ctx": {"count": 2},
            }
        ]
    }


def test_alias_list_field():
    response = client.post(
        "/alias-strict", data={"username": "rick", "tags": ["a", "b"]}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"user_name": "rick", "tags": ["a", "b"]}


def test_union_form_cat():
    response = client.post("/union-form", data={"cat_name": "Tom", "lives": "7"})
    assert response.status_code == 200, response.text
    assert response.json() == {"cat_name": "Tom", "lives": 7}


def test_union_form_dog():
    response = client.post("/union-form", data={"dog_name": "Rex"})
    assert response.status_code == 200, response.text
    assert response.json() == {"dog_name": "Rex", "good_boy": True}


def test_union_form_invalid():
    response = client.post("/union-form", data={"cat_name": "Tom", "lives": "many"})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert len(detail) > 0
    for error in detail:
        assert error["loc"][0] == "body"
        assert "type" in error


def test_query_and_form_are_independent():
    response = client.post(
        "/query-and-form?q=fromquery",
        data={"username": "fromform", "tags": ["x", "y"]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "q": "fromquery",
        "user_name": "fromform",
        "tags": ["x", "y"],
    }


def test_same_alias_query_and_form_do_not_overwrite():
    response = client.post(
        "/same-alias?shared=queryval",
        data={"shared": "formval"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"query_value": "queryval", "form_value": "formval"}


def test_same_alias_query_and_form_duplicate_form_value():
    response = client.post(
        "/same-alias?shared=queryval",
        data={"shared": ["formval1", "formval2"]},
    )
    assert response.status_code == 200, response.text
    # Lenient mode: the last value wins for the scalar form field, the query
    # parameter is not affected
    assert response.json() == {"query_value": "queryval", "form_value": "formval2"}


def test_dependency_cache_consistent():
    dependency_calls.clear()
    response = client.post(
        "/cached-dependency",
        data={"username": "cached", "tags": ["a", "b"]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"first": "cached", "second": "cached"}
    # The dependency was solved only once thanks to the dependency cache
    assert dependency_calls == ["cached"]


def test_form_endpoint_with_json_body():
    response = client.post("/alias-strict", json={"username": "x"})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert len(detail) == 1
    assert detail[0]["type"] == "missing"
    assert detail[0]["loc"] == ["body", "username"]
    assert detail[0]["msg"] == "Field required"


def test_form_endpoint_without_content_type():
    response = client.post("/alias-strict", content="username=x")
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert len(detail) == 1
    assert detail[0]["type"] == "missing"
    assert detail[0]["loc"] == ["body", "username"]
    assert detail[0]["msg"] == "Field required"


def test_json_endpoint_with_form_body():
    response = client.post("/json-endpoint", data={"username": "x"})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert len(detail) == 1
    assert detail[0]["loc"] == ["body"]
    assert detail[0]["type"] == "model_attributes_type"
