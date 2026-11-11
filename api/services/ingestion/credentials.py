"""Encrypt a source credential and bind its ciphertext to the source ID."""

import os

from cryptography.fernet import Fernet, InvalidToken


def _cipher() -> Fernet:
    try:
        return Fernet(os.environ["API_INGESTION_SECRET_KEY"].encode("ascii"))
    except (KeyError, ValueError) as error:
        raise ValueError("Ingestion credentials are unavailable") from error


def protect(sourceId: str, secret: str) -> str:
    return _cipher().encrypt(f"{sourceId}\n{secret}".encode()).decode("ascii")


def reveal(sourceId: str, ciphertext: str) -> str:
    try:
        storedId, secret = _cipher().decrypt(ciphertext.encode("ascii")).decode().split("\n", 1)
    except (InvalidToken, ValueError) as error:
        raise ValueError("Ingestion credentials are unavailable") from error
    if storedId != sourceId:
        raise ValueError("Ingestion credentials are unavailable")
    return secret
