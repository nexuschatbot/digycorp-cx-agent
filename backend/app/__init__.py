"""Elite dealership CX agent.

`app.clock` is imported here, and the import is not incidental: it pins the
process timezone to the dealership's, which is the assumption every naive
datetime in this package is written under. Importing it at package level means
any entry point — uvicorn, the CLI, pytest, a one-off script — gets the same
clock without having to remember to ask for it. See app/clock.py for the
measured failures that made this necessary.
"""

from app import clock as clock  # noqa: F401  (imported for its side effect)
