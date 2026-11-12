"""Test RSA key pairs for admission-token signing, generated once per session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@dataclass(frozen=True)
class TestKey:
    __test__ = False  # not a pytest test class

    kid: str
    private_pem: bytes
    public_pem: bytes

    def write_private(self, directory: Path) -> Path:
        path = directory / f"{self.kid}.private.pem"
        path.write_bytes(self.private_pem)
        return path


def make_key(kid: str) -> TestKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return TestKey(
        kid=kid,
        private_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        public_pem=key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


@pytest.fixture(scope="session")
def signing_key() -> TestKey:
    return make_key("test-key-1")


@pytest.fixture(scope="session")
def rotated_key() -> TestKey:
    """A second key for rotation tests (old key still published in JWKS)."""
    return make_key("test-key-2")
