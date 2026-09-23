from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel

app = FastAPI()


@app.post("/strict-scalar")
def strict_scalar(q: Annotated[str, Form(strict=True)]):
    return {"q": q}


@app.post("/strict-int")
def strict_int(age: Annotated[int, Form(strict=True)]):
    return {"age": age}


@app.post("/strict-opt-int")
def strict_opt_int(age: Annotated[Optional[int], Form(strict=True)] = None):
    return {"age": age}


class Inner(BaseModel):
    name: str
    age: Optional[int] = None
    tags: list[str] = []


@app.post("/model-lenient")
def model_lenient(data: Annotated[Inner, Form()]):
    return data


@app.post("/model-strict")
def model_strict(data: Annotated[Inner, Form(strict=True)]):
    return data


@app.post("/strict-file")
def strict_file(f: Annotated[UploadFile, File(strict=True)]):
    return {"filename": f.filename}


client = TestClient(app)

r = client.post("/strict-scalar", data={"q": ["a", "b"]})
print("strict dup:", r.status_code, r.json())
r = client.post("/strict-scalar", data={"q": ""})
print("strict empty str:", r.status_code, r.json())
r = client.post("/strict-int", data={"age": "70"})
print("strict int ok:", r.status_code, r.json())
r = client.post("/strict-int", data={"age": ""})
print("strict int empty:", r.status_code, r.json())
r = client.post("/strict-opt-int", data={"age": ""})
print("strict opt int empty:", r.status_code, r.json())
r = client.post("/strict-opt-int", content="")
print("strict opt int missing:", r.status_code, r.json())
r = client.post("/model-lenient", data={"name": "x", "age": ""})
print("model lenient empty age:", r.status_code, r.json())
r = client.post("/model-strict", data={"name": "x", "age": ""})
print("model strict empty age:", r.status_code, r.json())
r = client.post("/model-strict", data={"name": ["a", "b"]})
print("model strict dup:", r.status_code, r.json())
r = client.post("/model-strict", data={"name": "x", "tags": ["a", "b"]})
print("model strict list ok:", r.status_code, r.json())
r = client.post("/strict-file", files={"f": ("", b"")})
print("strict empty file:", r.status_code, r.json())
r = client.post("/strict-file", files=[("f", ("a.txt", b"1")), ("f", ("b.txt", b"2"))])
print("strict dup file:", r.status_code, r.json())
