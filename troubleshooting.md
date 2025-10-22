## Troubleshooting

### Chat Errors

The most common error is an incorrect HTTP Endpoint. Go to `Settings -> HTTP Endpoint` and ensure it is set to "Context-Aware RAG — Non-Streaming (Experimental)":

<img src="docs/settings.jpg" alt="Settings" style="max-width: 300px; width: 100%;" />

### Container Logs
View logs for any service with:
```bash
docker logs --tail=50 <service-name>
```

### Service Health
Check if services are responding:
```bash
curl http://localhost:8000/health  # RAG Retrieval
curl http://localhost:8001/health  # RAG Ingestion
curl http://localhost:9091/healthz # Milvus
curl http://localhost:7474         # Neo4j
```

### Clean Start
To start fresh and remove persisted data:
```bash
docker compose -f external/context-aware-rag/docker/deploy/compose.yaml down -v
docker compose -f deploy/docker-compose.yaml --profile replay down -v
```
