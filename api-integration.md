## API Integration

This section documents the API endpoints used by the streaming application to integrate with both the RAG backend and frontend UI. You can use these APIs to build your own streaming applications that work with the existing infrastructure.

### Backend RAG API

The streaming application communicates with the Context-Aware RAG backend through two main endpoints:

#### 1. Initialize Ingestion Service

**Endpoint:** `POST /init`
**URL:** `http://<database_uri>/init`
**Purpose:** Initialize the ingestion service with a unique UUID

**Request Headers:**
```
Content-Type: application/json
```

**Request Body:**
```json
{
  "uuid": "123456" # matches UUID in CA-RAG deployment
}
```

**Response:**
- Status: 200 OK on success
- The UUID can be set via the `RAG_UUID` environment variable (defaults to "123456")

**Example Usage:**
```python
import requests
import json

response = requests.post(
    f"http://{database_uri}/init",
    headers={"Content-Type": "application/json"},
    data=json.dumps({"uuid": "123456"}),
    timeout=10
)
```

#### 2. Add Document to RAG System

**Endpoint:** `POST /add_doc`
**URL:** `http://<database_uri>/add_doc`
**Purpose:** Add a transcript document to the RAG system for ingestion

**Request Headers:**
```
Content-Type: application/json
```

**Request Body:**
```json
{
  "document": "This is the transcript text content",
  "doc_index": 0,
  "doc_metadata": {
    "is_first": true,
    "is_last": false,
    "file": "rtsp://stream0",
    "streamId": "stream0",
    "doc_id": "stream0",
    "chunkIdx": 0,
    "timestamp": "2024-01-01 12:00:00",
    "start_ntp": "2024-01-01T12:00:00.000Z",
    "end_ntp": "2024-01-01T12:00:30.000Z",
    "start_ntp_float": 1704110400.0,
    "end_ntp_float": 1704110430.0,
    "start_pts": 1704110400000000000,
    "end_pts": 1704110430000000000,
    "uuid": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

**Field Descriptions:**

**Top-level fields:**
- `document`: The actual transcript text content to be ingested
- `doc_index`: Sequential document index (increments globally)

**Metadata fields (`doc_metadata`):**
- `is_first`: Boolean indicating if this is the first document in the stream
- `is_last`: Boolean indicating if this is the last document in the stream (typically `false` for streaming)
- `file`: Source identifier, typically formatted as `rstp://{stream-id}`
- `streamId`: Stream identifier for filtering queries (e.g., "stream-0", "audio-feed", "transcript-source")
- `doc_id`: Document identifier, typically matches `streamId` for stream-based sources
- `chunkIdx`: Chunk index within the document, typically matches `doc_index`
- `timestamp`: Human-readable timestamp in format "YYYY-MM-DD HH:MM:SS" (UTC)
- `start_ntp`: Start time in NTP format "YYYY-MM-DDTHH:MM:SS.sssZ"
- `end_ntp`: End time in NTP format "YYYY-MM-DDTHH:MM:SS.sssZ"
- `start_ntp_float`: Start time as Unix timestamp (float, seconds since epoch)
- `end_ntp_float`: End time as Unix timestamp (float, seconds since epoch)
- `start_pts`: Start time as presentation timestamp (nanoseconds since epoch)
- `end_pts`: End time as presentation timestamp (nanoseconds since epoch)
- `uuid`: Unique identifier for this specific document (UUID4 format)

**Response:**
- Status: 200 OK on success
- Body: `{"status": "success"}`

**Example Usage:**
```python
import requests
import json
from datetime import datetime, timezone
import uuid

# Prepare document data
timestamp = datetime.now(timezone.utc)
doc_data = {
    'document': transcript_text,
    "doc_index": get_next_doc_id(),
    "doc_metadata": {
        "is_first": True,
        "is_last": False,
        "file": f"rstp://stream-{stream_id}",
        "streamId": f"stream-{stream_id}",
        "doc_id": f"stream-{stream_id}",
        "chunkIdx": doc_index,
        "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "start_ntp": timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-3] + "Z",
        "end_ntp": timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-3] + "Z",
        "start_ntp_float": timestamp.timestamp(),
        "end_ntp_float": timestamp.timestamp(),
        "start_pts": int(timestamp.timestamp() * 1e9),
        "end_pts": int(timestamp.timestamp() * 1e9),
        "uuid": str(uuid.uuid4())
    }
}

response = requests.post(
    f'http://{database_uri}/add_doc',
    headers={"Content-Type": "application/json"},
    data=json.dumps(doc_data)
)
```

### Frontend Real-Time API

The streaming application updates the frontend UI in real-time using the following endpoint:

#### Update Text Stream

**Endpoint:** `POST /api/update-text`

**URL:** `http://<frontend_uri>/api/update-text`

**Purpose:** Send real-time transcript updates to the frontend UI

**Request Headers:**
```
Content-Type: application/json
```

**Request Body:**
```json
{
  "text": "This is the partial or final transcript",
  "stream_id": "stream-0",
  "timestamp": "2024-01-01 12:00:00",
  "finalized": false,
  "uuid": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Key Fields:**
- `text`: The transcript text (partial or final)
- `stream_id`: Stream identifier matching the backend streamId
- `timestamp`: Human-readable timestamp
- `finalized`: Boolean indicating if this is a final transcript
- `uuid`: Optional UUID for tracking

**Example Usage:**
```python
import requests
from datetime import datetime, timezone

def send_to_frontend(transcript, stream_id, is_final=False):
    endpoint = f"http://{frontend_uri}/api/update-text"
    data = {
        "text": transcript,
        "stream_id": f"stream-{stream_id}",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "finalized": is_final,
        "uuid": str(uuid.uuid4()) if is_final else None
    }

    response = requests.post(endpoint, json=data)
    return response
```

### Building Your Own Streaming Application

To build your own streaming application that integrates with this RAG system:

1. **Initialize the RAG service** on startup with a unique UUID
2. **Batch your transcripts** - don't send every partial update to the backend
3. **Use consistent stream IDs** for proper stream association
4. **Include proper timing metadata** for time-based queries
5. **Send real-time updates** to the frontend for live monitoring

**Example Integration Pattern:**
```python
class StreamingTranscriber:
    def __init__(self, stream_id, database_uri, frontend_uri):
        self.stream_id = stream_id
        self.database_uri = database_uri
        self.frontend_uri = frontend_uri
        self.accumulated_text = ""
        self.min_export_chars = 500

        # Initialize RAG service
        self.initialize_rag_service()

    def initialize_rag_service(self):
        response = requests.post(
            f"http://{self.database_uri}/init",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"uuid": os.environ.get("RAG_UUID", "123456")}),
            timeout=10
        )
        assert response.status_code == 200

    def on_partial_transcript(self, text):
        # Send to frontend for real-time display
        self.send_to_frontend(text, finalized=False)

    def on_final_transcript(self, text):
        # Add to accumulator
        self.accumulated_text += f" {text}"

        # Send to frontend
        self.send_to_frontend(text, finalized=True)

        # Export to RAG backend when we have enough text
        if len(self.accumulated_text) >= self.min_export_chars:
            self.export_to_rag(self.accumulated_text)
            self.accumulated_text = ""

    def send_to_frontend(self, text, finalized=False):
        # Implementation as shown above
        pass

    def export_to_rag(self, text):
        # Implementation as shown above
        pass
```

This pattern ensures efficient batching for the RAG system while providing real-time updates to the frontend UI.
