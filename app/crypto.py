"""数据源密码 AES 加密（落库前加密，密钥走环境变量，不入库）。

见避坑清单 #28 / 假设 A14：meta 库一旦泄露不得裸奔后端凭证。
使用 Fernet（AES-128-CBC + HMAC），密钥由 DB_SECRET_KEY 派生为 32 字节。
"""
import base64
import hashlib

from cryptography.fernet import Fernet

from .config import settings


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.DB_SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    """加密明文，返回可安全落库的 token 字符串。"""
    if plain is None:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """解密 token 还原明文。"""
    if not token:
        return ""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
