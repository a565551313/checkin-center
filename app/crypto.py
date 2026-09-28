"""凭据加解密（Fernet）与脱敏显示。

密钥只从环境变量 CHECKIN_FERNET_KEY 读取；没有密钥时拒绝启动，
避免有人在没加密的情况下把凭据写入数据库。
"""
import os

from cryptography.fernet import Fernet, InvalidToken


def get_fernet() -> Fernet:
    key = os.environ.get("CHECKIN_FERNET_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "缺少环境变量 CHECKIN_FERNET_KEY。"
            "用 `python -m app.crypto` 生成一个，写入 .env 后再启动。"
        )
    return Fernet(key.encode())


def encrypt(plain: "str | None") -> "str | None":
    if not plain:
        return None
    return get_fernet().encrypt(plain.encode()).decode()


def decrypt(token: "str | None") -> "str | None":
    if not token:
        return None
    try:
        return get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("凭据解密失败：密钥可能已更换。") from exc


def mask_login(login: "str | None") -> str:
    """脱敏：5***@qq.com / p***@gmail.com 风格；永远不返回原文。"""
    if not login:
        return "未设置"
    login = login.strip()
    if "@" in login:
        local, domain = login.split("@", 1)
        head = local[0] if local else "*"
        return f"{head}***@{domain}"
    if len(login) <= 2:
        return "***"
    return f"{login[0]}***{login[-1]}"


def mask_key(key: "str | None") -> str:
    if not key:
        return "未设置"
    key = key.strip()
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


if __name__ == "__main__":
    # 生成密钥：python -m app.crypto
    print(Fernet.generate_key().decode())
