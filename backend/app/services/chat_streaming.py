"""Wire framing and incremental extraction for structured chat responses."""

import asyncio
import json
import logging
from contextlib import suppress
from time import monotonic

import anyio
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)
MAX_MODEL_JSON_CHARS = 262_144


class ChatStreamingResponse(StreamingResponse):
    """Close our async generator even when the socket fails during a send."""

    async def stream_response(self, send):
        try:
            await super().stream_response(send)
        finally:
            # A disconnect can interrupt send() while the body generator is
            # suspended at yield. Starlette does not close it in that case.
            # Explicit closure cancels its producer immediately, without GC.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


class AnswerJsonStream:
    """Extract only the top-level answer string; validate the full JSON at EOF.

    A JSON escape (including a UTF-16 surrogate pair) is emitted only once it is
    complete. Other top-level values may precede answer. No JSON syntax or
    citation IDs are ever emitted as answer text.
    """

    def __init__(self):
        self.document = ""
        self._position = 0
        self._state = "object"
        self._key = None
        self._decoder = json.JSONDecoder()

    def feed(self, text: str) -> str:
        self.document += text
        if len(self.document) > MAX_MODEL_JSON_CHARS:
            raise ValueError("Model output exceeds the chat response limit")
        output = []
        while self._position < len(self.document):
            if self._state == "finished":
                break
            if self._state == "answer":
                character = self.document[self._position]
                if character == '"':
                    self._state = "finished"
                    break
                if character == "\\":
                    decoded = self._escape()
                    if decoded is None:
                        break
                    value, length = decoded
                    output.append(value)
                    self._position += length
                else:
                    if ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF:
                        raise ValueError("Invalid character in answer")
                    output.append(character)
                    self._position += 1
                continue

            character = self.document[self._position]
            if character in " \t\r\n":
                self._position += 1
                continue
            if self._state == "object":
                self._expect("{")
                self._state = "key"
            elif self._state == "key":
                try:
                    self._key, end = self._decoder.raw_decode(self.document, self._position)
                except json.JSONDecodeError:
                    break
                if not isinstance(self._key, str):
                    raise ValueError("Expected an object key")
                self._position = end
                self._state = "colon"
            elif self._state == "colon":
                self._expect(":")
                self._state = "value"
            elif self._state == "value":
                if self._key == "answer":
                    self._expect('"')
                    self._state = "answer"
                else:
                    try:
                        _, end = self._decoder.raw_decode(self.document, self._position)
                    except json.JSONDecodeError:
                        break
                    # A number can be a valid prefix of a longer number. Wait
                    # until the following object delimiter is also available.
                    tail = self.document[end:].lstrip()
                    if not tail:
                        break
                    self._position = end
                    self._state = "comma"
            else:
                self._expect(",")
                self._state = "key"
        return "".join(output)

    def _expect(self, character):
        if self.document[self._position] != character:
            raise ValueError("Invalid structured answer")
        self._position += 1

    def _escape(self):
        remaining = self.document[self._position:]
        if len(remaining) < 2:
            return None
        simple = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
        if remaining[1] in simple:
            return simple[remaining[1]], 2
        if remaining[1] != "u":
            raise ValueError("Invalid JSON escape")
        if len(remaining) < 6:
            return None
        if any(character not in "0123456789abcdefABCDEF" for character in remaining[2:6]):
            raise ValueError("Invalid Unicode escape")
        code = int(remaining[2:6], 16)
        if 0xD800 <= code <= 0xDBFF:
            if len(remaining) < 12:
                return None
            if remaining[6:8] != "\\u":
                raise ValueError("Incomplete Unicode surrogate pair")
            if any(character not in "0123456789abcdefABCDEF" for character in remaining[8:12]):
                raise ValueError("Invalid Unicode escape")
            low = int(remaining[8:12], 16)
            if not 0xDC00 <= low <= 0xDFFF:
                raise ValueError("Invalid Unicode surrogate pair")
            return chr(0x10000 + ((code - 0xD800) << 10) + low - 0xDC00), 12
        if 0xDC00 <= code <= 0xDFFF:
            raise ValueError("Unexpected low surrogate")
        return chr(code), 6


def encode_sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=True)}\n\n".encode("utf-8")


async def event_stream(events, *, timeout_seconds: float, request=None, heartbeat_seconds: float = 10):
    """Bound the producer, send heartbeats, and close it on disconnect/cancel.

    One producer task owns the generator for its entire lifetime, including
    async client contexts. A bounded queue applies backpressure without moving
    that generator between tasks. Never turn cancellation into a fallback.
    """
    iterator = events.__aiter__()
    queue = asyncio.Queue(maxsize=1)

    async def produce():
        try:
            async for event in iterator:
                await queue.put(event)
            await queue.put(None)
        except Exception as exc:
            logger.warning("chat: stream failed (%s)", type(exc).__name__)
            await queue.put(("error", {"code": "generation_failed", "message": "Unable to finish this response. Please try again."}))
        finally:
            with anyio.CancelScope(shield=True):
                await iterator.aclose()

    producer = asyncio.create_task(produce())
    deadline = monotonic() + timeout_seconds
    try:
        while True:
            if request is not None and await request.is_disconnected():
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                yield encode_sse("error", {"code": "timeout", "message": "The response took too long. Please try again."})
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=min(heartbeat_seconds, remaining))
            except TimeoutError:
                if monotonic() < deadline:
                    yield b": keep-alive\n\n"
                continue
            if item is None:
                yield encode_sse("error", {"code": "interrupted", "message": "The response ended before it was complete. Please try again."})
                return
            event, payload = item
            yield encode_sse(event, payload)
            if event in {"done", "error"}:
                return
    except Exception as exc:
        logger.warning("chat: stream failed (%s)", type(exc).__name__)
        yield encode_sse("error", {"code": "generation_failed", "message": "Unable to finish this response. Please try again."})
    finally:
        # Starlette cancels the response task on disconnect. Shield cleanup from
        # that AnyIO scope so the Gemini connection and producer are closed.
        with anyio.CancelScope(shield=True):
            producer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await producer
