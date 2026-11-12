from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.core.config import Settings
from app.core.tokens import AdmissionTokens, KeyConfigError, load_private_key, load_public_keys
from app.queue.models import Admission
from scripts.gen_jwt_key import generate
from tests.fakes.reservation_service import (
    TokenRejected,
    ValidatedAdmission,
    validate_admission_token,
)
from tests.fixtures.keys import TestKey

pytestmark = pytest.mark.unit

ADMITTED = datetime(2026, 11, 20, 12, 0, 0, 734_000, tzinfo=UTC)
ADMISSION = Admission(
    admission_id="8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
    user_id="2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    event_id="c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    admitted_at=ADMITTED,
    selection_expires_at=ADMITTED + timedelta(minutes=5),
    admission_expires_at=ADMITTED + timedelta(minutes=11),
)
CONTRACT_CLAIMS = {
    "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "admitted_at": "2026-11-20T12:00:00Z",
    "selection_expires_at": "2026-11-20T12:05:00Z",
    "admission_expires_at": "2026-11-20T12:11:00Z",
}


def _tokens(key: TestKey, tmp_path: Path, public_dir: Path | None = None) -> AdmissionTokens:
    return AdmissionTokens(
        load_private_key(key.write_private(tmp_path)),
        key.kid,
        load_public_keys(public_dir) if public_dir else None,
    )


def _b64url_json(segment: str) -> dict[str, object]:
    data: dict[str, object] = json.loads(
        base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    )
    return data


def _validate(token: str, tokens: AdmissionTokens, **overrides: object) -> ValidatedAdmission:
    args: dict[str, object] = {
        "user_id": ADMISSION.user_id,
        "event_id": ADMISSION.event_id,
        "now": ADMITTED + timedelta(minutes=1),
    }
    args.update(overrides)
    return validate_admission_token(token, tokens.jwks(), **args)  # type: ignore[arg-type]


# --- token shape ---------------------------------------------------------------


def test_header_is_rs256_with_kid(signing_key: TestKey, tmp_path: Path) -> None:
    token = _tokens(signing_key, tmp_path).sign(ADMISSION)
    assert _b64url_json(token.split(".")[0]) == {"alg": "RS256", "typ": "JWT", "kid": "test-key-1"}


def test_claims_are_exactly_the_admission_object(signing_key: TestKey, tmp_path: Path) -> None:
    token = _tokens(signing_key, tmp_path).sign(ADMISSION)
    claims = _b64url_json(token.split(".")[1])
    assert claims == CONTRACT_CLAIMS  # no iat, exp, nbf, iss or anything else
    assert list(claims) == list(CONTRACT_CLAIMS)  # stable claim order


def test_signing_is_deterministic(signing_key: TestKey, tmp_path: Path) -> None:
    first = _tokens(signing_key, tmp_path).sign(ADMISSION)
    assert _tokens(signing_key, tmp_path).sign(ADMISSION) == first  # new signer, same key
    assert _tokens(signing_key, tmp_path).sign(ADMISSION) == first


def test_different_admissions_give_different_tokens(signing_key: TestKey, tmp_path: Path) -> None:
    tokens = _tokens(signing_key, tmp_path)
    other = replace(ADMISSION, admission_id="other")
    assert tokens.sign(other) != tokens.sign(ADMISSION)


# --- JWKS ------------------------------------------------------------------------


def test_jwks_shape(signing_key: TestKey, tmp_path: Path) -> None:
    jwks = _tokens(signing_key, tmp_path).jwks()
    (key,) = jwks["keys"]
    assert list(key) == ["kty", "kid", "use", "alg", "n", "e"]
    assert (key["kty"], key["kid"], key["use"], key["alg"], key["e"]) == (
        "RSA",
        "test-key-1",
        "sig",
        "RS256",
        "AQAB",
    )
    assert "=" not in key["n"] and "+" not in key["n"] and "/" not in key["n"]


def test_token_verifies_with_published_jwk(signing_key: TestKey, tmp_path: Path) -> None:
    tokens = _tokens(signing_key, tmp_path)
    (jwk,) = tokens.jwks()["keys"]
    claims = jwt.decode(
        tokens.sign(ADMISSION),
        jwt.PyJWK.from_json(json.dumps(jwk)).key,
        algorithms=["RS256"],
    )
    assert claims == CONTRACT_CLAIMS


# --- rotation --------------------------------------------------------------------


def test_rotation_publishes_old_key_and_signs_with_new(
    signing_key: TestKey, rotated_key: TestKey, tmp_path: Path
) -> None:
    old = _tokens(signing_key, tmp_path)
    old_token = old.sign(ADMISSION)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / f"{signing_key.kid}.pem").write_bytes(signing_key.public_pem)

    new = _tokens(rotated_key, tmp_path, public_dir)
    new_token = new.sign(ADMISSION)

    assert [k["kid"] for k in new.jwks()["keys"]] == ["test-key-2", "test-key-1"]
    assert jwt.get_unverified_header(new_token)["kid"] == "test-key-2"
    assert new_token != old_token
    # Both tokens for the same admission validate against the new JWKS.
    assert _validate(old_token, new).admission_id == _validate(new_token, new).admission_id


def test_active_key_in_public_dir_is_listed_once(signing_key: TestKey, tmp_path: Path) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / f"{signing_key.kid}.public.pem").write_bytes(signing_key.public_pem)

    jwks = _tokens(signing_key, tmp_path, public_dir).jwks()

    assert [k["kid"] for k in jwks["keys"]] == ["test-key-1"]


def test_public_file_for_active_kid_must_match(
    signing_key: TestKey, rotated_key: TestKey, tmp_path: Path
) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / f"{signing_key.kid}.pem").write_bytes(rotated_key.public_pem)

    with pytest.raises(KeyConfigError, match="does not match"):
        _tokens(signing_key, tmp_path, public_dir)


def test_public_dir_ignores_other_files_and_rejects_duplicates(
    signing_key: TestKey, rotated_key: TestKey, tmp_path: Path
) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "README.txt").write_text("not a key")
    (public_dir / "old.pem").write_bytes(rotated_key.public_pem)
    assert [k.kid for k in load_public_keys(public_dir)] == ["old"]

    (public_dir / "old.public.pem").write_bytes(rotated_key.public_pem)
    with pytest.raises(KeyConfigError, match="two public key files"):
        load_public_keys(public_dir)


# --- key loading ------------------------------------------------------------------


def test_from_settings_with_generated_keys(tmp_path: Path) -> None:
    private_path, _public_path = generate(tmp_path, "queue-local-1")
    settings = Settings(
        mysql_url="mysql+asyncmy://u:p@h/db",
        redis_url="redis://h",
        rabbitmq_url="amqp://h",
        jwt_private_key_path=private_path,
        jwt_kid="queue-local-1",
        jwt_public_keys_dir=None,
    )
    tokens = AdmissionTokens.from_settings(settings)
    assert _validate(tokens.sign(ADMISSION), tokens).user_id == ADMISSION.user_id


def test_missing_private_key_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(KeyConfigError, match="cannot read"):
        load_private_key(tmp_path / "nope.pem")


def test_non_pem_private_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.pem"
    path.write_text("hello")
    with pytest.raises(KeyConfigError, match="not an unencrypted PEM"):
        load_private_key(path)


def test_non_rsa_private_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ec.pem"
    path.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(KeyConfigError, match="not an RSA key"):
        load_private_key(path)


def test_short_rsa_key_rejected() -> None:
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.raises(KeyConfigError, match="at least 2048"):
        AdmissionTokens(weak, "weak")


# --- the Reservation-side contract --------------------------------------------------


def test_valid_token_passes_reservation_checks(signing_key: TestKey, tmp_path: Path) -> None:
    tokens = _tokens(signing_key, tmp_path)
    result = _validate(tokens.sign(ADMISSION), tokens)
    assert result == ValidatedAdmission(
        admission_id=ADMISSION.admission_id,
        user_id=ADMISSION.user_id,
        event_id=ADMISSION.event_id,
        selection_expires_at=datetime(2026, 11, 20, 12, 5, tzinfo=UTC),
        admission_expires_at=datetime(2026, 11, 20, 12, 11, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"user_id": "someone-else"}, "user_mismatch"),
        ({"event_id": "another-event"}, "event_mismatch"),
        ({"now": datetime(2026, 11, 20, 12, 5, tzinfo=UTC)}, "selection_expired"),
        ({"now": datetime(2026, 11, 20, 12, 10, tzinfo=UTC)}, "selection_expired"),
    ],
)
def test_misused_token_rejected(
    signing_key: TestKey, tmp_path: Path, overrides: dict[str, object], reason: str
) -> None:
    tokens = _tokens(signing_key, tmp_path)
    with pytest.raises(TokenRejected) as exc:
        _validate(tokens.sign(ADMISSION), tokens, **overrides)
    assert exc.value.reason == reason


def test_selection_deadline_uses_whole_seconds(signing_key: TestKey, tmp_path: Path) -> None:
    """Admitted at 12:00:00.734: the token says 12:05:00Z, so 12:04:59.9 is the last chance."""
    tokens = _tokens(signing_key, tmp_path)
    token = tokens.sign(ADMISSION)
    _validate(token, tokens, now=datetime(2026, 11, 20, 12, 4, 59, 900_000, tzinfo=UTC))
    with pytest.raises(TokenRejected):
        _validate(token, tokens, now=datetime(2026, 11, 20, 12, 5, 0, 100_000, tzinfo=UTC))


def test_tampered_claims_rejected(signing_key: TestKey, tmp_path: Path) -> None:
    tokens = _tokens(signing_key, tmp_path)
    header, _payload, signature = tokens.sign(ADMISSION).split(".")
    forged_claims = {**CONTRACT_CLAIMS, "selection_expires_at": "2026-11-20T13:00:00Z"}
    forged_payload = base64.urlsafe_b64encode(json.dumps(forged_claims).encode()).rstrip(b"=")
    with pytest.raises(TokenRejected) as exc:
        _validate(f"{header}.{forged_payload.decode()}.{signature}", tokens)
    assert exc.value.reason == "signature"


def test_token_from_unknown_key_rejected(
    signing_key: TestKey, rotated_key: TestKey, tmp_path: Path
) -> None:
    attacker = AdmissionTokens(load_private_key(rotated_key.write_private(tmp_path)), "test-key-1")
    with pytest.raises(TokenRejected) as exc:
        _validate(attacker.sign(ADMISSION), _tokens(signing_key, tmp_path))
    assert exc.value.reason == "signature"

    stranger = AdmissionTokens(load_private_key(rotated_key.write_private(tmp_path)), "who")
    with pytest.raises(TokenRejected) as exc:
        _validate(stranger.sign(ADMISSION), _tokens(signing_key, tmp_path))
    assert exc.value.reason == "unknown_kid"


def test_algorithm_confusion_rejected(signing_key: TestKey, tmp_path: Path) -> None:
    """HS256 signed with the public key as the secret, and unsigned 'none' tokens."""
    tokens = _tokens(signing_key, tmp_path)
    header = {"alg": "HS256", "typ": "JWT", "kid": "test-key-1"}
    body = ".".join(
        base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()
        for part in (header, CONTRACT_CLAIMS)
    )
    with pytest.raises(TokenRejected) as exc:
        _validate(f"{body}.c2ln", tokens)
    assert exc.value.reason == "algorithm"

    unsigned = jwt.encode(CONTRACT_CLAIMS, key="", algorithm="none", headers={"kid": "test-key-1"})
    with pytest.raises(TokenRejected) as exc:
        _validate(unsigned, tokens)
    assert exc.value.reason == "algorithm"


def test_garbage_rejected(signing_key: TestKey, tmp_path: Path) -> None:
    with pytest.raises(TokenRejected) as exc:
        _validate("not-a-jwt", _tokens(signing_key, tmp_path))
    assert exc.value.reason == "malformed"
