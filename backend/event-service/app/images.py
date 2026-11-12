MAX_IMAGE_BYTES = 300 * 1024

# Each type is checked against the file's first bytes, so the declared type can't be trusted alone.
_SIGNATURES = {
    "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/webp": lambda data: data[:4] == b"RIFF" and data[8:12] == b"WEBP",
}


def detect_image_type(data: bytes) -> str | None:
    return next((name for name, matches in _SIGNATURES.items() if matches(data)), None)
