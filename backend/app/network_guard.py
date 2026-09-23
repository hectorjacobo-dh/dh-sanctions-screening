"""Restrict which networks may reach the service.

The problem this solves
-----------------------
Binding to 0.0.0.0 makes the app reachable by anything that can route to this
machine. On the corporate network that is the intent. But a laptop moves: on
café or hotel Wi-Fi, or a home network, the same bind makes an internal
compliance tool — holding partner data and UBO personal data — reachable by
every stranger on that network. Login stands in the way, but an exposed login
form is still an invitation to guess, and there is no reason for it to be
visible at all outside DH.

So requests are allowed only from an explicit set of networks. Loopback is
always permitted, so the operator's own use works on any network.

This is defence in depth, not authentication. A private range is not proof of
being inside Delivery Hero — plenty of home routers hand out 10.x addresses.
It is a cheap, reliable way to ensure the service simply is not there when the
laptop is somewhere it should not be serving.
"""

from __future__ import annotations

import ipaddress
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Always allowed: the machine itself.
_LOOPBACK = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def parse_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse a comma-separated CIDR list, skipping anything malformed."""
    networks = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            networks.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            logger.warning("ignoring invalid entry in ALLOWED_NETWORKS: %r", chunk)
    return networks


class NetworkGuardMiddleware(BaseHTTPMiddleware):
    """Reject requests from outside the permitted networks."""

    def __init__(self, app, allowed: str, enabled: bool = True) -> None:  # noqa: ANN001
        super().__init__(app)
        self.networks = parse_networks(allowed)
        self.enabled = enabled
        if enabled:
            logger.info(
                "network guard active: loopback + %s",
                ", ".join(str(n) for n in self.networks) or "(nothing else)",
            )

    def _permitted(self, client: str | None) -> bool:
        if not client:
            # No client address to judge — refuse rather than assume.
            return False
        try:
            address = ipaddress.ip_address(client)
        except ValueError:
            return False
        if any(address in net for net in _LOOPBACK):
            return True
        return any(address in net for net in self.networks)

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        if not self.enabled:
            return await call_next(request)

        client = request.client.host if request.client else None
        if self._permitted(client):
            return await call_next(request)

        logger.warning(
            "refused %s %s from %s (outside permitted networks)",
            request.method,
            request.url.path,
            client,
        )
        # Deliberately terse and 403 rather than 404: no hint about what runs
        # here, and no login form rendered for a stranger to guess against.
        return JSONResponse(
            status_code=403,
            content={"detail": "This service is not available from your network."},
        )
