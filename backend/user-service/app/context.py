from contextvars import ContextVar

# The current request's correlation ID. Set by CorrelationIdMiddleware; "-" outside a request.
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


def get_correlation_id() -> str:
    return correlation_id_var.get()
