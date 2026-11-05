"""生成内网联调用的短期管理 JWT，密钥从环境变量读取。"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import time


def encode(value):
    """将 JSON 编码成 Base64URL，不带末尾的填充符。"""
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")


def token(secret, subject, role, games, expiry=900, issuer="gamehub-admin", audience="gamehub", key_id=""):
    """生成 HS256 JWT，写入有效期、角色和可访问的游戏。"""
    now = int(time.time())
    header_value = {"alg": "HS256", "typ": "JWT"}
    if key_id:
        if not re.fullmatch(r"[A-Za-z0-9_:.-]{1,64}", key_id) or key_id in (".", ".."):
            raise ValueError("密钥标识必须是 1 至 64 字节的 ASCII 标识")
        header_value["kid"] = key_id
    header = encode(header_value)
    payload = encode({"sub": subject, "role": role, "games": games, "iat": now, "nbf": now,
                      "exp": now + expiry, "iss": issuer, "aud": audience})
    message = header + b"." + payload
    digest = base64.urlsafe_b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).rstrip(b"=")
    return (message + b"." + digest).decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--role", choices=("viewer", "operator"), default="viewer")
    parser.add_argument("--game", action="append", required=True)
    parser.add_argument("--expiry", type=int, default=900)
    parser.add_argument("--key-id", default="", help="轮换配置中的 jwtKeyId；不指定时生成旧协议令牌")
    parser.add_argument("--secret-env", default="GAMEHUB_ADMIN_JWT_SECRET", help="当前签发密钥的环境变量名")
    args = parser.parse_args()
    secret = os.environ.get(args.secret_env, "")
    if len(secret) < 32 or not 1 <= args.expiry <= 3600:
        parser.error("密钥需要至少 32 字节，有效期请设为 1 至 3600 秒")
    try:
        print(token(secret, args.subject, args.role, args.game, args.expiry, key_id=args.key_id))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
