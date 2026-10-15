"""
Error hierarchy.

Every external dependency raises a subclass of UpstreamError, so the API layer
maps the whole family to HTTP 502 in one place instead of enumerating
provider-specific exceptions at each call site. Adding a new dependency means
adding a subclass here, not touching the routers.
"""


class QueryDeskError(RuntimeError):
    """Base for every error this application raises deliberately."""


class UpstreamError(QueryDeskError):
    """
    A dependency we don't control failed, or isn't configured.

    Distinct from bad user input: these become 502s, because the request was
    fine and something behind us wasn't.
    """


class LLMError(UpstreamError):
    """Fireworks failed, returned an unusable response, or isn't configured."""


class VectorStoreError(UpstreamError):
    """Qdrant failed or isn't configured."""


class TicketingError(UpstreamError):
    """The ticketing service failed, rejected the SR, or isn't configured."""


class RedisStoreError(UpstreamError):
    """Redis failed or isn't configured."""


class PostgresStoreError(UpstreamError):
    """Postgres (the orchestrator's own Neon database) failed or isn't configured."""


class JevError(UpstreamError):
    """Jev (via JevStation) failed, returned an unusable response, or isn't configured."""
