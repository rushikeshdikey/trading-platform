"""Tests for app/auth.py — password hashing + Fernet encrypt/decrypt round-trip."""
from __future__ import annotations

from app.auth import (
    decrypt_str,
    encrypt_str,
    hash_password,
    needs_rehash,
    verify_password,
)


def test_hash_then_verify_roundtrips():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True


def test_verify_rejects_wrong_password():
    h = hash_password("right")
    assert verify_password("wrong", h) is False


def test_verify_rejects_garbage_hash():
    assert verify_password("anything", "not-a-real-hash") is False


def test_each_hash_is_unique_due_to_salt():
    a = hash_password("same")
    b = hash_password("same")
    assert a != b
    assert verify_password("same", a)
    assert verify_password("same", b)


def test_needs_rehash_false_for_fresh_hash():
    h = hash_password("abc")
    assert needs_rehash(h) is False


def test_fernet_roundtrips_strings():
    blob = encrypt_str("kite-secret-xyz")
    assert blob != b"kite-secret-xyz"  # actually encrypted, not just encoded
    assert decrypt_str(blob) == "kite-secret-xyz"


def test_fernet_changes_each_call():
    """Two encrypts of the same plaintext should not produce identical
    ciphertext — Fernet includes a random IV."""
    a = encrypt_str("secret")
    b = encrypt_str("secret")
    assert a != b
    assert decrypt_str(a) == decrypt_str(b) == "secret"
