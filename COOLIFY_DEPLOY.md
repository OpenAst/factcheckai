# Coolify Deployment

This repo can be deployed in Coolify as a Docker Compose application.

## Services

- `api`: FastAPI app exposed on port `8000`

## Required Environment Variables

Set these in the Coolify application's environment screen:

```env
GEMINI_API_KEY=...
GROQ_API_KEY=...
SERPER_API_KEY=...
ADMIN_TOKEN=...
LOG_LEVEL=INFO
```

`ADMIN_TOKEN` is optional unless you use the admin endpoints.
`LOG_LEVEL` is optional and defaults to `INFO`. Use `DEBUG`, `WARNING`, or `ERROR` when you want more or less log output.

## Coolify Setup

1. Create a new resource from this Git repository.
2. Choose Docker Compose.
3. Use `docker-compose.yml` as the compose file.
4. Set the API public domain to service `api` on port `8000`.
5. Add the environment variables above.
6. Deploy.

The compose file uses `expose` instead of `ports` for the API. This lets Coolify's proxy route traffic to the container without reserving host port `8000`, which avoids port allocation conflicts on shared servers.

The compose file creates a persistent volume for the SQLite cache.
