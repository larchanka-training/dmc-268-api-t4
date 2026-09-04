from fastapi import FastAPI

app = FastAPI(title="DMC-268 API", version="0.1.0")


@app.get("/")
def read_root():
    return {"message": "Welcome to DMC-268 Team 4 API"}


@app.get("/health")
def health_check():
    return {"status": "ok"}
