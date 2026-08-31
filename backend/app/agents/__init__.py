"""Agent prompts as versioned Markdown (Phase 2).

    base/v1.md       sections every agent carries
    aisha/v1.md      the customer-facing journey prose
    sales/v1.md      routing card; body lands in Phase 5
    testdrive/v1.md  routing card; body lands in Phase 5
    finance/v1.md    routing card; body lands in Phase 5
    active.json      which version of each is live

loader.py reads them once per process and caches per (agent, version) — see its
docstring on why reading per turn would break the prompt cache.
"""
