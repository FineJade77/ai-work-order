from fastapi import FastAPI

app = FastAPI(title="tool-gateway")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
