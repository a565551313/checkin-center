import os
import pytest
from cryptography.fernet import Fernet

os.environ["CHECKIN_FERNET_KEY"] = Fernet.generate_key().decode()

from app import crypto


def test_encrypt_decrypt():
    plain = "my-secret-password-123"
    token = crypto.encrypt(plain)
    assert token is not None
    assert token != plain
    decrypted = crypto.decrypt(token)
    assert decrypted == plain


def test_safe_decrypt():
    plain = "test-secret"
    token = crypto.encrypt(plain)
    val, ok = crypto.safe_decrypt(token)
    assert ok is True
    assert val == plain

    val_none, ok_none = crypto.safe_decrypt(None)
    assert ok_none is True
    assert val_none is None

    val_bad, ok_bad = crypto.safe_decrypt("corrupted-token-12345")
    assert ok_bad is False
    assert val_bad is None


def test_mask_login():
    assert crypto.mask_login("user@example.com") == "u***@example.com"
    assert crypto.mask_login("54321@qq.com") == "5***@qq.com"
    assert crypto.mask_login("ab") == "***"
    assert crypto.mask_login("admin") == "a***n"
    assert crypto.mask_login("") == "未设置"
    assert crypto.mask_login(None) == "未设置"


def test_mask_key():
    assert crypto.mask_key("sk-1234567890abcdef") == "sk-***cdef"
    assert crypto.mask_key("12345678") == "***"
    assert crypto.mask_key("") == "未设置"
    assert crypto.mask_key(None) == "未设置"
