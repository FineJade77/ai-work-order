from fastapi import FastAPI

app = FastAPI(title="agent-runtime")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
