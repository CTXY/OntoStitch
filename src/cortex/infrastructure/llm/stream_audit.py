"""Reconstruct a completed SSE response for wire auditing, not model decisions."""

import json


class UpstreamResponseIncomplete(ConnectionError):
    """The HTTP stream ended without a complete model result."""


def validate_stream_completion(body):
    error = body.get('error')
    if error:
        raise UpstreamResponseIncomplete(
            f"Upstream stream error: {error.get('code') or error.get('type')}: {error.get('message')}"
            if isinstance(error, dict) else f"Upstream stream error: {error}"
        )
    choices = body.get('choices') or []
    if not choices or any(not c.get('finish_reason') for c in choices):
        raise UpstreamResponseIncomplete('Upstream SSE ended without a terminal model result; discard partial output and retry the same dialogue')


def normalize_sse_frames(text):
    """Separate consecutive complete data objects when a gateway omits a blank line."""
    lines, complete = [], False
    for line in text.splitlines():
        valid = False
        if line.startswith("data:"):
            data = line[5:].strip()
            try:
                valid = data == "[DONE]" or isinstance(json.loads(data), dict)
            except ValueError:
                pass
            if complete and valid:
                lines.append("")
        lines.append(line)
        complete = valid
    return "\n".join(lines) + "\n\n"


def completion_from_sse(text):
    result, choices = {}, {}
    for event in normalize_sse_frames(text).split("\n\n"):
        data = "\n".join(line[5:].strip() for line in event.splitlines() if line.startswith("data:"))
        if not data or data == "[DONE]":
            continue
        chunk = json.loads(data)
        for key in ("id", "model", "usage", "error"):
            if chunk.get(key) is not None:
                result[key] = chunk[key]
        for choice in chunk.get("choices", []):
            index = choice.get("index", 0)
            row = choices.setdefault(index, {"index": index, "message": {"role": "assistant", "content": ""}, "finish_reason": None})
            delta = choice.get("delta") or {}
            for key in ("content", "reasoning_content"):
                if delta.get(key):
                    row["message"][key] = row["message"].get(key, "") + delta[key]
            for call in delta.get("tool_calls") or []:
                tools = row["message"].setdefault("tool_calls", [])
                position = call.get("index", 0)
                while len(tools) <= position:
                    tools.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                tool = tools[position]
                if call.get("id"):
                    tool["id"] = call["id"]
                for key in ("name", "arguments"):
                    tool["function"][key] += (call.get("function") or {}).get(key) or ""
            if choice.get("finish_reason"):
                row["finish_reason"] = choice["finish_reason"]
    result["choices"] = list(choices.values())
    result["streamed"] = True
    return result
