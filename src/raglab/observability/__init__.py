"""Payload-free structured observability for the RAG lab."""

from .events import EventName, JsonlEventWriter, SafeEvent

__all__ = ["EventName", "JsonlEventWriter", "SafeEvent"]
