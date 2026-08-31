"""Local retrieval: embed content, index it, search it (Phase 1).

    chunk.py           markdown -> passages, with citations and metadata
    embed.py           passages -> 384-dim vectors, on CPU, offline
    index.py           build/cache/search a collection
    catalogue_pass.py  the additive vector pass over catalogue rows (T1.6)

NOTHING HERE IMPORTS THE MODEL AT IMPORT TIME. `embed` loads its 113MB ONNX
session lazily and caches the failure as well as the success, so importing this
package costs nothing and a checkout without the vendored asset still runs.

EVERY ENTRY POINT DEGRADES RATHER THAN RAISES. No model, no tokenizer, no
content, an unreadable cache entry — each returns empty and logs. Retrieval is
an enhancement over the catalogue, and the catalogue is what answers when it is
unavailable.
"""
