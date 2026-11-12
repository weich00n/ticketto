import uuid

import pytest

from app.qr import InvalidQrCode, QrSigner, render_png

KEYS = {"k1": "k1-secret-" + "x" * 40, "k0": "k0-secret-" + "y" * 40}


def test_signed_payload_verifies():
    signer = QrSigner(KEYS, "k1")
    ticket_id = str(uuid.uuid4())
    key_id, signature = signer.sign(ticket_id)

    code = signer.verify(QrSigner.payload(ticket_id, key_id, signature))

    assert (code.ticket_id, code.key_id, code.signature) == (ticket_id, "k1", signature)


def test_tickets_signed_with_an_older_key_still_verify_after_rotation():
    ticket_id = str(uuid.uuid4())
    key_id, signature = QrSigner(KEYS, "k0").sign(ticket_id)

    assert (
        QrSigner(KEYS, "k1").verify(QrSigner.payload(ticket_id, key_id, signature)).key_id == "k0"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.replace("TKT1", "TKT2"),
        lambda p: p[:-1] + ("A" if p[-1] != "A" else "B"),  # tampered signature
        lambda p: p.replace(".k1.", ".zz."),  # unknown key
        lambda p: p.rsplit(".", 1)[0],  # no signature
        lambda p: p.upper(),  # non-canonical ticket ID
        lambda p: "",
        lambda p: p + "." + "x" * 300,
    ],
)
def test_forged_or_malformed_payloads_are_rejected(mutate):
    signer = QrSigner(KEYS, "k1")
    ticket_id = str(uuid.uuid4())
    payload = QrSigner.payload(ticket_id, *signer.sign(ticket_id))

    with pytest.raises(InvalidQrCode):
        signer.verify(mutate(payload))


def test_signature_for_one_ticket_does_not_work_for_another():
    signer = QrSigner(KEYS, "k1")
    key_id, signature = signer.sign(str(uuid.uuid4()))

    with pytest.raises(InvalidQrCode):
        signer.verify(QrSigner.payload(str(uuid.uuid4()), key_id, signature))


def test_render_png_returns_a_png():
    assert render_png("TKT1.k1.test.sig").startswith(b"\x89PNG\r\n\x1a\n")
