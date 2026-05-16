# Coolify Deployment

This repo can be deployed in Coolify as a Docker Compose application.

## Services

- `api`: FastAPI app exposed on port `8000`
- `worker`: OCR background worker
- `redis`: queue and OCR job state

## Required Environment Variables

Set these in the Coolify application's environment screen:

```env
GEMINI_API_KEY=...
GROQ_API_KEY=...
SERPER_API_KEY=...
ADMIN_TOKEN=...
```

`ADMIN_TOKEN` is optional unless you use the admin endpoints.

## Coolify Setup

1. Create a new resource from this Git repository.
2. Choose Docker Compose.
3. Use `docker-compose.yml` as the compose file.
4. Set the API public domain to service `api` on port `8000`.
5. Add the environment variables above.
6. Deploy.

The compose file creates persistent volumes for the SQLite cache, EasyOCR models, and Redis data. The OCR worker downloads EasyOCR models on first start, so the first OCR request can take longer than later requests.

