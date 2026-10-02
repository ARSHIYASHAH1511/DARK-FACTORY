# Pocketful service (stage 1)

From this directory (needs only Docker; no network needed at run time):

```
docker build -t pocketful . && docker run --rm -e PORT=8080 -p 8080:8080 pocketful
```

Listens on `0.0.0.0:$PORT` (default 8080). `GET /health` returns `{"status":"ok"}`.
State is in memory; seed it with `POST /_test/reset`.
