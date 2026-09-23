from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

app = FastAPI()


@app.post("/debug")
async def debug(request: Request):
    body = await request.body()
    form = await request.form()
    return {
        "content_type": request.headers.get("content-type"),
        "raw": body.decode(),
        "form_items": [(k, v) for k, v in form.multi_items()],
    }


client = TestClient(app)
r = client.post("/debug", data=[("q", "a"), ("q", "b")])
print(r.json())
r = client.post("/debug", data={"q": "a"})
print(r.json())
