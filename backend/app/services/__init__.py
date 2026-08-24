"""Service layer — ALL business logic and the ONLY code that touches the DB.

Tools (agent path) and REST routes (direct path) are both thin wrappers over
these functions, calling them in-process (AD-I, AD-J). Swap the implementation
for a real CRM/DMS adapter and both callers keep working.

Services return plain dicts/lists, never ORM objects, so a caller can serialise
a result straight to JSON without a lazy-load surprise.
"""
