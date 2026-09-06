"""Pluggable LLM provider adapters.

Each adapter normalizes one provider to a common surface — list_models / complete
/ stream — so the generation path (backend/generate) is provider-agnostic. The
registry maps a stored provider id to its adapter; new providers plug in here.
"""
