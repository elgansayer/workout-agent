"""Fail-closed public-origin and reverse-proxy trust boundary for the web app.

Uvicorn's built-in proxy header handling is deliberately disabled in production.
This middleware therefore sees the real socket peer, validates the public Host,
accepts forwarding metadata only from explicitly trusted proxy networks, and
normalises the ASGI scope before FastAPI, OAuth, rate limiting, or audit logging
consume request origin information.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
import os
from typing import Any, Awaitable, Callable, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit

ASGIApp = Callable[
    [MutableMapping[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
    Awaitable[None],
]
IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network

_FORWARDING_HEADERS = frozenset(
    {
        b"forwarded",
        b"x-forwarded-for",
        b"x-real-ip",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"x-forwarded-port",
    }
)


class ProxySecurityConfigurationError(RuntimeError):
    """Raised when the public-origin/proxy trust policy is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class ProxySecurityConfig:
    """Validated reverse-proxy security configuration."""

    public_scheme: str
    public_host: str
    allowed_hosts: frozenset[str]
    trusted_proxy_networks: tuple[IPNetwork, ...]

    @property
    def public_origin(self) -> str:
        return f"{self.public_scheme}://{self.public_host}"


def _normalise_host(value: str, *, scheme: str | None = None) -> str:
    raw = value.strip().lower()
    if (
        not raw
        or any(character in raw for character in ("/", "\\", "\r", "\n", "\t", " "))
        or "@" in raw
        or "," in raw
    ):
        raise ProxySecurityConfigurationError(f"Invalid host value: {value!r}")

    try:
        parsed = urlsplit(f"//{raw}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProxySecurityConfigurationError(f"Invalid host value: {value!r}") from exc

    if not hostname:
        raise ProxySecurityConfigurationError(f"Invalid host value: {value!r}")

    hostname = hostname.rstrip(".").lower()
    if not hostname:
        raise ProxySecurityConfigurationError(f"Invalid host value: {value!r}")

    if scheme == "https" and port == 443:
        port = None
    elif scheme == "http" and port == 80:
        port = None

    formatted_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{formatted_host}:{port}" if port is not None else formatted_host


def _parse_public_url(value: str, *, production: bool) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        if production:
            raise ProxySecurityConfigurationError(
                "WEB_PUBLIC_URL is required when APP_ENV=production"
            )
        raw = "http://localhost"

    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ProxySecurityConfigurationError("WEB_PUBLIC_URL must use http or https")
    if production and parsed.scheme != "https":
        raise ProxySecurityConfigurationError(
            "WEB_PUBLIC_URL must use https when APP_ENV=production"
        )
    if (
        not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ProxySecurityConfigurationError(
            "WEB_PUBLIC_URL must be an origin only (scheme and host, with optional port)"
        )

    return parsed.scheme, _normalise_host(parsed.netloc, scheme=parsed.scheme)


def load_proxy_security_config(
    env: Mapping[str, str] | None = None,
) -> ProxySecurityConfig:
    """Validate public-origin and trusted-proxy settings.

    Production must provide a canonical HTTPS origin. The canonical host is
    always accepted. WEB_ALLOWED_HOSTS can add public aliases. Forwarding
    headers are ignored unless the immediate socket peer belongs to one of the
    exact CIDRs in WEB_TRUSTED_PROXY_CIDRS.
    """

    environment = os.environ if env is None else env
    app_env = environment.get("APP_ENV", "development").strip().lower()
    scheme, canonical_host = _parse_public_url(
        environment.get("WEB_PUBLIC_URL", ""),
        production=app_env == "production",
    )

    allowed_hosts = {canonical_host}
    for raw_host in environment.get("WEB_ALLOWED_HOSTS", "").split(","):
        if raw_host.strip():
            allowed_hosts.add(_normalise_host(raw_host, scheme=scheme))

    networks: list[IPNetwork] = []
    for raw_network in environment.get("WEB_TRUSTED_PROXY_CIDRS", "").split(","):
        candidate = raw_network.strip()
        if not candidate:
            continue
        try:
            networks.append(ip_network(candidate, strict=False))
        except ValueError as exc:
            raise ProxySecurityConfigurationError(
                f"Invalid WEB_TRUSTED_PROXY_CIDRS entry: {candidate!r}"
            ) from exc

    return ProxySecurityConfig(
        public_scheme=scheme,
        public_host=canonical_host,
        allowed_hosts=frozenset(allowed_hosts),
        trusted_proxy_networks=tuple(networks),
    )


def _header_values(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> list[str]:
    return [
        value.decode("latin-1").strip()
        for key, value in headers
        if key.lower() == name
    ]


def _peer_ip(scope: Mapping[str, Any]) -> IPAddress | None:
    client = scope.get("client")
    if not client:
        return None
    try:
        return ip_address(str(client[0]))
    except ValueError:
        return None


def _is_trusted(address: IPAddress | None, networks: Sequence[IPNetwork]) -> bool:
    if address is None:
        return False
    return any(address in network for network in networks)


def _parse_forwarded_ips(values: Sequence[str]) -> list[IPAddress]:
    result: list[IPAddress] = []
    for value in values:
        for token in value.split(","):
            candidate = token.strip()
            if not candidate:
                raise ValueError("empty forwarded address")
            result.append(ip_address(candidate))
    return result


def _resolve_client_ip(
    peer: IPAddress,
    forwarded: Sequence[IPAddress],
    trusted_networks: Sequence[IPNetwork],
) -> IPAddress:
    if not forwarded:
        return peer

    chain = [*forwarded, peer]
    index = len(chain) - 1
    while index >= 0 and _is_trusted(chain[index], trusted_networks):
        index -= 1

    if index >= 0:
        return chain[index]

    # Every address in the chain is trusted. Retain the left-most forwarded
    # address so downstream code still receives a stable, validated client.
    return forwarded[0]


def _canonical_server(public_host: str, scheme: str) -> tuple[str, int]:
    parsed = urlsplit(f"//{public_host}")
    hostname = parsed.hostname or public_host
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return hostname, port


class ProxySecurityMiddleware:
    """Validate Host and sanitise all proxy-controlled origin metadata."""

    def __init__(self, app: ASGIApp, config: ProxySecurityConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        original_headers = list(scope.get("headers", []))
        host_values = _header_values(original_headers, b"host")
        if len(host_values) != 1:
            await self._reject(scope, send)
            return

        try:
            incoming_host = _normalise_host(
                host_values[0],
                scheme=self.config.public_scheme,
            )
        except ProxySecurityConfigurationError:
            await self._reject(scope, send)
            return

        if incoming_host not in self.config.allowed_hosts:
            await self._reject(scope, send)
            return

        peer = _peer_ip(scope)
        trusted_peer = _is_trusted(peer, self.config.trusted_proxy_networks)
        resolved_client = peer

        if trusted_peer and peer is not None:
            forwarded_host_values = _header_values(
                original_headers, b"x-forwarded-host"
            )
            if forwarded_host_values:
                forwarded_host = forwarded_host_values[-1].split(",")[-1].strip()
                try:
                    normalised_forwarded_host = _normalise_host(
                        forwarded_host,
                        scheme=self.config.public_scheme,
                    )
                except ProxySecurityConfigurationError:
                    await self._reject(scope, send)
                    return
                if normalised_forwarded_host not in self.config.allowed_hosts:
                    await self._reject(scope, send)
                    return

            forwarded_proto_values = _header_values(
                original_headers, b"x-forwarded-proto"
            )
            if forwarded_proto_values:
                forwarded_proto = (
                    forwarded_proto_values[-1].split(",")[-1].strip().lower()
                )
                if forwarded_proto not in {"http", "https"}:
                    await self._reject(scope, send)
                    return

            xff_values = _header_values(original_headers, b"x-forwarded-for")
            real_ip_values = _header_values(original_headers, b"x-real-ip")
            try:
                if xff_values:
                    forwarded_ips = _parse_forwarded_ips(xff_values)
                elif real_ip_values:
                    if len(real_ip_values) != 1:
                        raise ValueError("duplicate x-real-ip")
                    forwarded_ips = [ip_address(real_ip_values[0])]
                else:
                    forwarded_ips = []
            except ValueError:
                await self._reject(scope, send)
                return

            resolved_client = _resolve_client_ip(
                peer,
                forwarded_ips,
                self.config.trusted_proxy_networks,
            )

        sanitised_scope = dict(scope)
        sanitised_headers = [
            (key, value)
            for key, value in original_headers
            if key.lower() not in _FORWARDING_HEADERS and key.lower() != b"host"
        ]
        sanitised_headers.append((b"host", self.config.public_host.encode("ascii")))
        sanitised_scope["headers"] = sanitised_headers
        sanitised_scope["scheme"] = (
            "wss"
            if scope.get("type") == "websocket" and self.config.public_scheme == "https"
            else "ws"
            if scope.get("type") == "websocket"
            else self.config.public_scheme
        )
        sanitised_scope["server"] = _canonical_server(
            self.config.public_host,
            self.config.public_scheme,
        )

        if resolved_client is not None:
            original_client = scope.get("client")
            client_port = (
                int(original_client[1])
                if original_client and len(original_client) > 1
                else 0
            )
            sanitised_scope["client"] = (str(resolved_client), client_port)

        state = dict(scope.get("state") or {})
        state["validated_origin"] = self.config.public_origin
        state["validated_client_ip"] = (
            str(resolved_client) if resolved_client is not None else "unknown"
        )
        state["trusted_proxy"] = trusted_peer
        sanitised_scope["state"] = state

        await self.app(sanitised_scope, receive, send)

    @staticmethod
    async def _reject(
        scope: Mapping[str, Any],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        body = b"Bad Request"
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
