"""Cheap market intel providers (search + optional Gemini). No UI scraping."""

from backend.app.intel.search_brief import IntelBrief, fetch_intel_brief

__all__ = ["IntelBrief", "fetch_intel_brief"]
