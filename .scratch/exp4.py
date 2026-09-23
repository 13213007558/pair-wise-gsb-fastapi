from typing import Annotated

from fastapi import FastAPI, Form
from fastapi.testclient import TestClient
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    name: str


@app.post("/form")
def form_ep(q: Annotated[str, Form()]):
    return {"q": q}


@app.post("/json")
def json_ep(item: Item):
    return item


client = TestClient(app)

# form endpoint, JSON body
r = client.post("/form", json={"q": "x"})
print("form ep + json body:", r.status_code, r.json())

# form endpoint, no content type
r = client.post("/form", content="q=x")
print("form ep + no ct:", r.status_code, r.json())

# json endpoint, form body
r = client.post("/json", data={"name": "x"})
print("json ep + form body:", r.status_code, r.json())

# does pydantic pick up field_info.strict set post-hoc?
from pydantic import Field, TypeAdapter

class M(BaseModel):
    age: int = Field(strict=True)

try:
    print("pydantic strict field:", M(age="70"))
except Exception as e:
    print("pydantic strict field error:", e.errors()[0]["type"])

class M2(BaseModel):
    age: int

M2.model_fields["age"].strict = True
M2.model_rebuild(force=True)
try:
    print("post-hoc strict:", M2(age="70"))
except Exception as e:
    print("post-hoc strict error:", e.errors()[0]["type"])
