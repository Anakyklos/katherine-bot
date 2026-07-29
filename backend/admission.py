"""Admission runtime primitives for the active ``/chat`` path.

The module is deliberately independent from FastAPI, Supabase, Groq, embeddings,
engine, memory, clock, randomness, filesystem, and network I/O. Environment
variables are read only when :meth:`AdmissionRuntimeConfig.from_env` is called.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass, field
from typing import Any, Mapping

from .admission_contracts import (
    AdmissionError,
    MESSAGE_MAX_ESTIMATED_UNITS,
    RequestIdentity,
    estimate_text_units,
)

MESSAGE_HMAC_DOMAIN = b"message"
NETWORK_HMAC_DOMAIN = b"network"
UNKNOWN_NETWORK_IDENTITY = "unknown"

ADMITTED = "admitted"
REQUEST_REPLAY_UNAVAILABLE = "request_replay_unavailable"
REQUEST_ID_CONFLICT = "request_id_conflict"
USER_RATE_LIMITED = "user_rate_limited"
NETWORK_RATE_LIMITED = "network_rate_limited"
APPLICATION_RATE_LIMITED = "application_rate_limited"
USER_DAILY_REQUEST_QUOTA_EXCEEDED = "user_daily_request_quota_exceeded"
USER_DAILY_UNIT_QUOTA_EXCEEDED = "user_daily_unit_quota_exceeded"
INVALID_ADMISSION_INPUT = "invalid_admission_input"

ADMISSION_DECISIONS = frozenset(
    {
        ADMITTED,
        REQUEST_REPLAY_UNAVAILABLE,
        REQUEST_ID_CONFLICT,
        USER_RATE_LIMITED,
        NETWORK_RATE_LIMITED,
        APPLICATION_RATE_LIMITED,
        USER_DAILY_REQUEST_QUOTA_EXCEEDED,
        USER_DAILY_UNIT_QUOTA_EXCEEDED,
        INVALID_ADMISSION_INPUT,
    }
)

_EXPECTED_RETRY_AFTER = {
    ADMITTED: 0,
    REQUEST_REPLAY_UNAVAILABLE: 0,
    REQUEST_ID_CONFLICT: 0,
    INVALID_ADMISSION_INPUT: 0,
    USER_RATE_LIMITED: 60,
    NETWORK_RATE_LIMITED: 60,
    APPLICATION_RATE_LIMITED: 60,
    USER_DAILY_REQUEST_QUOTA_EXCEEDED: 86400,
    USER_DAILY_UNIT_QUOTA_EXCEEDED: 86400,
}


class AdmissionConfigurationError(Exception):
    """Sanitised, non-secret configuration failure."""

    code = "invalid_admission_configuration"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __repr__(self) -> str:
        return "AdmissionConfigurationError()"


class AdmissionUnavailable(Exception):
    """Sanitised failure for an unavailable or malformed admission store."""

    code = "admission_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __repr__(self) -> str:
        return "AdmissionUnavailable()"


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _validate_secret_bytes(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise AdmissionConfigurationError()
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError:
        raise AdmissionConfigurationError() from None
    if not decoded.strip():
        raise AdmissionConfigurationError()
    return value


@dataclass(frozen=True)
class AdmissionRuntimeConfig:
    """Immutable process-wide admission configuration.

    ``secret_bytes`` is excluded from repr and must contain at least 32 valid,
    non-whitespace UTF-8 bytes. Trusted proxy CIDRs are parsed once at startup.
    """

    secret_bytes: bytes = field(repr=False)
    trusted_proxy_networks: tuple[IPNetwork, ...] = ()

    def __post_init__(self) -> None:
        _validate_secret_bytes(self.secret_bytes)
        if not isinstance(self.trusted_proxy_networks, tuple):
            raise AdmissionConfigurationError()
        if any(
            not isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network))
            for network in self.trusted_proxy_networks
        ):
            raise AdmissionConfigurationError()

    @classmethod
    def from_values(
        cls,
        secret: object,
        trusted_proxy_cidrs: object = None,
    ) -> "AdmissionRuntimeConfig":
        if not isinstance(secret, str) or not secret or not secret.strip():
            raise AdmissionConfigurationError()
        try:
            secret_bytes = secret.encode("utf-8")
        except Exception:
            raise AdmissionConfigurationError() from None
        _validate_secret_bytes(secret_bytes)

        if trusted_proxy_cidrs is None or trusted_proxy_cidrs == "":
            networks: tuple[IPNetwork, ...] = ()
        else:
            if not isinstance(trusted_proxy_cidrs, str):
                raise AdmissionConfigurationError()
            parsed: list[IPNetwork] = []
            for raw_token in trusted_proxy_cidrs.split(","):
                token = raw_token.strip()
                if not token:
                    raise AdmissionConfigurationError()
                try:
                    parsed.append(ipaddress.ip_network(token, strict=False))
                except ValueError:
                    raise AdmissionConfigurationError() from None
            networks = tuple(parsed)

        return cls(secret_bytes=secret_bytes, trusted_proxy_networks=networks)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, object] | None = None,
    ) -> "AdmissionRuntimeConfig":
        if env is None:
            import os

            source: Mapping[str, object] = os.environ
        else:
            source = env
        return cls.from_values(
            source.get("ADMISSION_HMAC_SECRET"),
            source.get("TRUSTED_PROXY_CIDRS"),
        )


def _parse_ip(value: object) -> IPAddress | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_trusted(address: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    return any(
        address.version == network.version and address in network
        for network in networks
    )


def resolve_network_identity(
    peer_host: object,
    x_forwarded_for: object,
    trusted_proxy_networks: tuple[IPNetwork, ...],
) -> str:
    """Resolve a canonical network identity without trusting arbitrary headers."""

    peer = _parse_ip(peer_host)
    if peer is None:
        return UNKNOWN_NETWORK_IDENTITY

    if not _is_trusted(peer, trusted_proxy_networks):
        return str(peer)

    if not isinstance(x_forwarded_for, str) or not x_forwarded_for:
        return UNKNOWN_NETWORK_IDENTITY

    parsed_chain: list[IPAddress] = []
    for raw_token in x_forwarded_for.split(","):
        token = raw_token.strip()
        if not token:
            return UNKNOWN_NETWORK_IDENTITY
        address = _parse_ip(token)
        if address is None:
            return UNKNOWN_NETWORK_IDENTITY
        parsed_chain.append(address)

    if not parsed_chain:
        return UNKNOWN_NETWORK_IDENTITY

    for address in reversed(parsed_chain):
        if not _is_trusted(address, trusted_proxy_networks):
            return str(address)

    return str(parsed_chain[0])


def compute_hmac_sha256(secret_bytes: bytes, domain: bytes, payload: str) -> str:
    """Compute an exact lowercase HMAC-SHA256 with domain separation."""

    validated_secret = _validate_secret_bytes(secret_bytes)
    if domain not in (MESSAGE_HMAC_DOMAIN, NETWORK_HMAC_DOMAIN):
        raise ValueError("unsupported admission HMAC domain")
    if not isinstance(payload, str):
        raise TypeError("admission HMAC payload must be a str")
    return hmac.new(
        validated_secret,
        domain + b"\x00" + payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _is_lower_hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True, repr=False)
class AdmissionRequest:
    user_id: str
    request_id: str
    message_hmac_sha256: str
    network_hmac_sha256: str
    estimated_units: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.user_id, str)
            or not self.user_id
            or not self.user_id.strip()
            or len(self.user_id) > 128
        ):
            raise AdmissionUnavailable()
        try:
            canonical_request_id = RequestIdentity(self.request_id).request_id
        except AdmissionError:
            raise AdmissionUnavailable() from None
        if not _is_lower_hex64(self.message_hmac_sha256):
            raise AdmissionUnavailable()
        if not _is_lower_hex64(self.network_hmac_sha256):
            raise AdmissionUnavailable()
        if (
            isinstance(self.estimated_units, bool)
            or not isinstance(self.estimated_units, int)
            or not 1 <= self.estimated_units <= MESSAGE_MAX_ESTIMATED_UNITS
        ):
            raise AdmissionUnavailable()
        object.__setattr__(self, "request_id", canonical_request_id)

    def __repr__(self) -> str:
        return "AdmissionRequest(<redacted>)"

    def rpc_params(self) -> dict[str, object]:
        return {
            "p_user_id": self.user_id,
            "p_request_id": self.request_id,
            "p_message_hmac_sha256": self.message_hmac_sha256,
            "p_network_hmac_sha256": self.network_hmac_sha256,
            "p_estimated_units": self.estimated_units,
        }


@dataclass(frozen=True)
class AdmissionResult:
    decision: str
    retry_after_seconds: int

    def __post_init__(self) -> None:
        if self.decision not in ADMISSION_DECISIONS:
            raise AdmissionUnavailable()
        if (
            isinstance(self.retry_after_seconds, bool)
            or not isinstance(self.retry_after_seconds, int)
            or self.retry_after_seconds != _EXPECTED_RETRY_AFTER[self.decision]
        ):
            raise AdmissionUnavailable()


def build_admission_request(
    *,
    user_id: object,
    request_identity: RequestIdentity,
    message: str,
    network_identity: str,
    config: AdmissionRuntimeConfig,
) -> AdmissionRequest:
    if not isinstance(user_id, str) or not user_id or len(user_id) > 128:
        raise AdmissionUnavailable()
    if not isinstance(request_identity, RequestIdentity):
        raise AdmissionUnavailable()
    if not isinstance(message, str) or not isinstance(network_identity, str):
        raise AdmissionUnavailable()
    if not isinstance(config, AdmissionRuntimeConfig):
        raise AdmissionUnavailable()

    if network_identity != UNKNOWN_NETWORK_IDENTITY:
        parsed_network_identity = _parse_ip(network_identity)
        if (
            parsed_network_identity is None
            or network_identity != str(parsed_network_identity)
        ):
            raise AdmissionUnavailable()

    return AdmissionRequest(
        user_id=user_id,
        request_id=request_identity.request_id,
        message_hmac_sha256=compute_hmac_sha256(
            config.secret_bytes, MESSAGE_HMAC_DOMAIN, message
        ),
        network_hmac_sha256=compute_hmac_sha256(
            config.secret_bytes, NETWORK_HMAC_DOMAIN, network_identity
        ),
        estimated_units=estimate_text_units(message),
    )


def parse_admission_result(payload: Any) -> AdmissionResult:
    """Validate an RPC result exactly and fail closed on any mismatch."""

    if not isinstance(payload, list) or len(payload) != 1:
        raise AdmissionUnavailable()
    row = payload[0]
    if not isinstance(row, dict) or set(row) != {
        "decision",
        "retry_after_seconds",
    }:
        raise AdmissionUnavailable()

    decision = row.get("decision")
    retry_after = row.get("retry_after_seconds")
    if not isinstance(decision, str) or decision not in ADMISSION_DECISIONS:
        raise AdmissionUnavailable()
    if isinstance(retry_after, bool) or not isinstance(retry_after, int):
        raise AdmissionUnavailable()
    if retry_after < 0 or retry_after != _EXPECTED_RETRY_AFTER[decision]:
        raise AdmissionUnavailable()

    return AdmissionResult(decision=decision, retry_after_seconds=retry_after)


def reserve_admission_sync(client: Any, request: AdmissionRequest) -> AdmissionResult:
    """Call the admission RPC through a duck-typed Supabase client."""

    if client is None or not isinstance(request, AdmissionRequest):
        raise AdmissionUnavailable()
    try:
        response = client.rpc("reserve_admission", request.rpc_params()).execute()
        payload = response.data
    except Exception:
        raise AdmissionUnavailable() from None
    return parse_admission_result(payload)
