"""Admission tokens: RS256 JWTs whose claims are exactly the admission object,
plus the JWKS document Reservation uses to verify them.

Tokens are not stored. They are re-signed from the admission on every poll;
RS256 (PKCS#1 v1.5) is deterministic, so the same admission and key always
produce the identical token string.

Key rotation: put the new private key at JWT_PRIVATE_KEY_PATH with a new
JWT_KID, and drop the old public key into JWT_PUBLIC_KEYS_DIR as <old-kid>.pem
(or <old-kid>.public.pem). Remove that file 11 minutes later, once no token
signed by the old key can still be in use.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings
from app.queue.models import Admission

ALGORITHM = "RS256"
MIN_KEY_BITS = 2048
_PUBLIC_SUFFIXES = (".public.pem", ".pem")


class KeyConfigError(ValueError):
    """The signing or verification keys are missing or unusable."""


@dataclass(frozen=True, slots=True)
class PublicKey:
    kid: str
    key: RSAPublicKey

    def to_jwk(self) -> dict[str, str]:
        numbers: dict[str, Any] = RSAAlgorithm.to_jwk(self.key, as_dict=True)
        return {
            "kty": "RSA",
            "kid": self.kid,
            "use": "sig",
            "alg": ALGORITHM,
            "n": numbers["n"],
            "e": numbers["e"],
        }


class AdmissionTokens:
    def __init__(
        self,
        private_key: RSAPrivateKey,
        kid: str,
        extra_public_keys: list[PublicKey] | None = None,
    ):
        _check_rsa_size(private_key.key_size, f"signing key {kid!r}")
        self._private_key = private_key
        self.kid = kid
        active = PublicKey(kid, private_key.public_key())
        self._published = [active]
        for extra in extra_public_keys or []:
            if extra.kid == kid:
                if _public_der(extra.key) != _public_der(active.key):
                    raise KeyConfigError(
                        f"public key file for kid {kid!r} does not match the signing key"
                    )
                continue  # the active key is already published
            _check_rsa_size(extra.key.key_size, f"public key {extra.kid!r}")
            self._published.append(extra)

    @classmethod
    def from_settings(cls, settings: Settings) -> AdmissionTokens:
        private_key = load_private_key(settings.jwt_private_key_path)
        extras = (
            load_public_keys(settings.jwt_public_keys_dir) if settings.jwt_public_keys_dir else []
        )
        return cls(private_key, settings.jwt_kid, extras)

    def sign(self, admission: Admission) -> str:
        """The admission token: header {alg, typ, kid}, claims = admission object."""
        return jwt.encode(
            admission.to_public(),
            self._private_key,
            algorithm=ALGORITHM,
            headers={"kid": self.kid},
        )

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """GET /.well-known/jwks.json body: the active key first, then older keys."""
        return {"keys": [key.to_jwk() for key in self._published]}


def load_private_key(path: Path) -> RSAPrivateKey:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise KeyConfigError(f"cannot read JWT private key at {path}: {exc}") from exc
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise KeyConfigError(f"{path} is not an unencrypted PEM private key") from exc
    if not isinstance(key, RSAPrivateKey):
        raise KeyConfigError(f"{path} is not an RSA key (RS256 needs RSA)")
    return key


def load_public_keys(directory: Path) -> list[PublicKey]:
    """Every <kid>.pem / <kid>.public.pem in directory, sorted by kid."""
    if not directory.is_dir():
        raise KeyConfigError(f"JWT_PUBLIC_KEYS_DIR {directory} is not a directory")
    keys: dict[str, PublicKey] = {}
    for path in sorted(directory.iterdir()):
        kid = _kid_from_filename(path.name)
        if kid is None:
            continue
        try:
            key = serialization.load_pem_public_key(path.read_bytes())
        except ValueError as exc:
            raise KeyConfigError(f"{path} is not a PEM public key") from exc
        if not isinstance(key, RSAPublicKey):
            raise KeyConfigError(f"{path} is not an RSA public key")
        if kid in keys:
            raise KeyConfigError(f"two public key files for kid {kid!r} in {directory}")
        keys[kid] = PublicKey(kid, key)
    return [keys[kid] for kid in sorted(keys)]


def _kid_from_filename(name: str) -> str | None:
    for suffix in _PUBLIC_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return None


def _check_rsa_size(bits: int, what: str) -> None:
    if bits < MIN_KEY_BITS:
        raise KeyConfigError(f"{what} is {bits}-bit RSA; at least {MIN_KEY_BITS} bits required")


def _public_der(key: RSAPublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
