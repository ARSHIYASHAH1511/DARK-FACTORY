# --- build the UI: Tailwind compiled to a static stylesheet, Inter downloaded and self-hosted ---
FROM node:20-alpine AS ui
WORKDIR /ui
RUN npm install --no-audit --no-fund --no-save tailwindcss@3.4.17 @fontsource-variable/inter@5
COPY tailwind.config.js ./
COPY web ./web
RUN mkdir -p /out \
 && npx tailwindcss -c tailwind.config.js -i web/input.css -o /out/app.css --minify \
 && cp node_modules/@fontsource-variable/inter/files/inter-latin-wght-normal.woff2 /out/inter.woff2 \
 && cp web/index.html web/app.js /out/

# --- runtime: stdlib-only Python, no network needed ---
FROM python:3.12-slim
WORKDIR /app
COPY server.py .
COPY --from=ui /out ./static
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-u", "server.py"]
