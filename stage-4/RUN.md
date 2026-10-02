# Pocketful service (stage 4)

From this directory (needs Docker and network access at *build* time only, to fetch Tailwind and the Inter font):

```
docker build -t pocketful4 . && docker run --rm -e PORT=8080 -p 8080:8080 pocketful4
```

Listens on `0.0.0.0:$PORT` (default 8080). `GET /health` returns `{"status":"ok"}`.
State is in memory; seed it with `POST /_test/reset`. No network is needed at run time:
the compiled Tailwind CSS, the app JS and the Inter font are served from the container (`/static/*`).

UI routes: `/`, `/requests`, `/split`, `/signup`, `/login`, `/authorizations`
(`/requests` and `/authorizations` return JSON unless `Accept: text/html`).

Stage 3 added `GET /statement`, `GET /me?as_of=&known_at=`, `POST /payments/{id}/corrections` and
`GET /payments/{id}/revisions`. Stage 4 adds `POST /payments/{id}/refunds` (payments carry `refund_of`)
and `POST /correction-batches` (settlement operator; revisions carry `correction_batch_id`).
The service is self-contained in this directory (`server.py`, `web/`, `tailwind.config.js`,
`Dockerfile`); nothing refers to the stage-1, stage-2 or stage-3 folders. Exports from stage-1, 2 and 3
services (including settlement membership, corrections and snapshots) are accepted by
`POST /_test/import`; fields added in stage 4 default when missing.
