"""Reading an OpenAI-style SSE stream after the fact: the trailing usage object and the completion text."""

import json


def parse_sse_tail(buf: bytes) -> tuple[dict | None, int]:
    """Extract the trailing usage object + count content chunks from an SSE
    byte buffer. Returns (usage|None, delta_chunks_seen_in_buffer)."""
    usage = None
    chunks = 0
    for raw_line in buf.split(b"\n"):
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        chunks += 1
        if b'"usage"' in payload:
            try:
                obj = json.loads(payload)
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
    return usage, chunks


def collect_stream_text(buf: bytes, limit: int) -> str:
    """Best-effort completion text from buffered SSE (log_bodies mode)."""
    out: list[str] = []
    for raw_line in buf.split(b"\n"):
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            obj = json.loads(payload)
            for choice in obj.get("choices", []):
                delta = choice.get("delta") or choice.get("message") or {}
                if delta.get("content"):
                    out.append(delta["content"])
        except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
            continue
    return "".join(out)[:limit]
