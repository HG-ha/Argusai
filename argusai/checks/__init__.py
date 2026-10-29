"""Built-in checks.

Importing this package registers every shipped check into the central
:class:`~argusai.core.check.Registry`. User-defined checks can be added by
importing ``argusai.core.check.register`` and decorating their own class.
"""

from argusai.checks import (  # noqa: F401
    recon,
    js_reverse,
    ai_recon,
    headers,
    tls,
    secrets,
    auth,
    fuzzer,
    injection,
    scanner,
    sqli_deep,
    xss,
    business,
    access_control,
    upload,
    identity,
    http_desync,
)
