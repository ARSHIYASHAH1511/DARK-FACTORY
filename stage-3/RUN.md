# Pocketful service (stage 3)

From this directory (needs Docker and network access at *build* time only, to fetch Tailwind and the Inter font):

```
docker build -t pocketful3 . && docker run --rm -e PORT=8080 -p 8080:8080 pocketful3
```

Listens on `0.0.0.0:$PORT` (default 8080). `GET /health` returns `{"status":"ok"}`.
State is in memory; seed it with `POST /_test/reset`. No network is needed at run time:
the compiled Tailwind CSS, the app JS and the Inter font are served from the container (`/static/*`).

UI routes: `/`, `/requests`, `/split`, `/signup`, `/login`, `/authorizations`
(`/requests` and `/authorizations` return JSON unless `Accept: text/html`).

Stage 3 adds `GET /statement`, `GET /me?as_of=&known_at=`, `POST /payments/{id}/corrections` and
`GET /payments/{id}/revisions`. The service is self-contained in this directory (`server.py`, `web/`,
`tailwind.config.js`, `Dockerfile`); nothing refers to the stage-1 or stage-2 folders. Exports from
stage-1 and stage-2 services are accepted by `POST /_test/import`.
