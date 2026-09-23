from typing import Annotated, Optional

from fastapi import FastAPI, Form
from fastapi.testclient import TestClient
from pydantic import BaseModel

app = FastAPI()


class Inner(BaseModel):
    name: str
    age: Optional[int] = None
    tags: list[str] = []


@app.post("/model")
def model_ep(data: Annotated[Inner, Form()]):
    return data


client = TestClient(app)
r = client.post("/model", data={"name": "", "age": ""})
print("inner empty str:", r.status_code, r.json())
r = client.post("/model", data={"name": "x", "tags": ["", "a"]})
print("inner tags with empty:", r.status_code, r.json())
r = client.post("/model", data={"name": ["a", "b"]})
print("inner scalar dup:", r.status_code, r.json())
