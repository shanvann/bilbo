"""Python contract for the BILBO HTTP layer.

Business logic for each route group lives here as plain Python functions.
The dashboard's Flask handlers are thin dispatchers that parse the request,
call into these functions, and `jsonify` (or `send_file`) the result. The
upcoming control-api will call the same functions, which lets steps 5-6
collapse the dashboard route handlers down to a reverse proxy without
losing any behavior.
"""
from bilbo.api import (
    entries,
    training,
    corrections,
    frames,
    models,
    inference,
    stats,
    system,
    air_quality,
    recap,
)

__all__ = [
    "entries",
    "training",
    "corrections",
    "frames",
    "models",
    "inference",
    "stats",
    "system",
    "air_quality",
    "recap",
]
