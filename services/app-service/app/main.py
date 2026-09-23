from fastapi import FastAPI

app = FastAPI(title="app-service")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
