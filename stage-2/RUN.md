# Pocketful service (stage 2)

From this directory (needs Docker and network access at *build* time only, to fetch Tailwind and the Inter font):

```
docker build -t pocketful2 . && docker run --rm -e PORT=8080 -p 8080:8080 pocketful2
```

Listens on `0.0.0.0:$PORT` (default 8080). `GET /health` returns `{"status":"ok"}`.
State is in memory; seed it with `POST /_test/reset`. No network is needed at run time:
the compiled Tailwind CSS, the app JS and the Inter font are served from the container (`/static/*`).

UI routes: `/`, `/requests`, `/split`, `/signup`, `/login`, `/authorizations`
(`/requests` and `/authorizations` return JSON unless `Accept: text/html`).
