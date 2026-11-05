"""GameHub 内网管理 CLI；管理令牌仅从环境变量或文件读取，错误不回显响应实体。"""

import argparse
import ipaddress
import http.client
import json
import os
from pathlib import Path
import queue
import ssl
import sys
import socket
import threading
import time
import urllib.parse


MAX_RESPONSE_BYTES = 1024 * 1024
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7"))
MAX_UNIX_SECONDS = 253402300799


class HttpStatusError(ValueError):
    """仅保留 HTTP 状态码，不保存响应实体或凭据。"""

    def __init__(self, status):
        """status 为管理接口返回的数值状态码。"""
        self.status = status
        super().__init__("管理接口返回 HTTP " + str(status))


def validate_base_url(value):
    """只接受 HTTPS 或内网 IP 的 HTTP 源站，不执行 DNS 猜测或跟随重定向。"""
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("管理地址必须是 HTTPS 或内网 IP 的 HTTP 源站")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        if parsed.scheme == "http":
            address = ipaddress.ip_address(parsed.hostname)
            if not any(address.version == network.version and address in network for network in PRIVATE_NETWORKS):
                raise ValueError
    except ValueError:
        raise ValueError("HTTP 管理地址必须使用明确的回环或私有 IP，公网地址使用 HTTPS") from None
    return value.rstrip("/")


def load_token(environment, token_file=None):
    """从指定环境变量或最多 4096 字节的 UTF-8 文件读取 JWT，不记录内容。"""
    if token_file:
        with token_file.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("管理令牌文件过大")
        value = raw.decode("utf-8").strip()
    else:
        value = os.environ.get(environment, "").strip()
    if not value or len(value) > 4096 or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("管理令牌为空或格式错误")
    return value


def validate_output(output, inputs):
    """拒绝报告路径覆盖输入或硬链接别名。"""
    if output is None:
        return
    for source in filter(None, inputs):
        if output.resolve() == source.resolve() or (output.exists() and source.exists() and output.samefile(source)):
            raise ValueError("输出不能覆盖输入文件")


class Client:
    """有整轮截止时间和响应上限的标准库 HTTP 客户端。"""

    def __init__(self, base_url, token, timeout=5, budget=60, connection_factory=None):
        """参数为源站、令牌、单请求时限和整轮预算；连接工厂仅用于测试。"""
        self.base_url = validate_base_url(base_url)
        self.token = token
        self.timeout = timeout
        self.deadline = time.monotonic() + budget
        self.connection_factory = connection_factory
        self.addresses = None

    def connect(self, parsed, deadline):
        """限时解析并固定地址，连接及 TLS 握手继续共用同一截止点。"""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if self.addresses is None:
            results = queue.Queue(maxsize=1)
            def resolve():
                try:
                    results.put(socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM))
                except OSError:
                    results.put(None)
            resolver = threading.Thread(target=resolve, daemon=True)
            resolver.start()
            try:
                self.addresses = results.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError("管理地址解析超过预算") from None
            if not self.addresses:
                raise ValueError("管理地址解析失败")
        for family, sock_type, protocol, _canonical, address in self.addresses:
            transport = socket.socket(family, sock_type, protocol)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("管理连接超过预算")
                transport.settimeout(remaining)
                transport.connect(address)
                if parsed.scheme == "https":
                    context = ssl.create_default_context()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("管理 TLS 握手超过预算")
                    transport.settimeout(remaining)
                    transport = context.wrap_socket(transport, server_hostname=parsed.hostname)
                return transport
            except OSError:
                transport.close()
        raise ValueError("管理接口连接或证书校验失败")

    def request(self, method, path, body=None):
        """发送一次请求，不自动重试写操作；返回有界 JSON 响应。"""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("管理操作总预算已耗尽")
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        parsed = urllib.parse.urlsplit(self.base_url)
        factory = self.connection_factory or (http.client.HTTPSConnection if parsed.scheme == "https"
                                               else http.client.HTTPConnection)
        request_timeout = min(self.timeout, remaining)
        request_deadline = time.monotonic() + request_timeout
        connection = factory(parsed.hostname, parsed.port, timeout=request_timeout)
        transport = []
        def abort():
            active_socket = transport[0] if transport else connection.sock
            if active_socket is not None:
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(request_timeout, abort)
        timer.daemon = True
        try:
            if self.connection_factory is None:
                connection.sock = self.connect(parsed, request_deadline)
            request_timeout = request_deadline - time.monotonic()
            if request_timeout <= 0:
                raise TimeoutError("管理请求超过预算")
            timer.interval = request_timeout
            timer.start()
            connection.request(method, path, body=data,
                headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
            transport.append(connection.sock)
            with connection.getresponse() as response:
                if response.status != 200:
                    raise HttpStatusError(response.status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("管理响应超过大小上限")
                if time.monotonic() > self.deadline or timer.finished.is_set():
                    raise TimeoutError("管理操作总预算已耗尽")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("管理响应必须是 JSON 对象")
                return value
        except (http.client.HTTPException, OSError):
            raise ValueError("管理接口连接或证书校验失败") from None
        finally:
            timer.cancel()
            connection.close()


def segment(value):
    """编码单一路径标识，拒绝会被服务端规范路径校验拒绝的输入。"""
    if not value or value in (".", "..") or any(char not in
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for char in value):
        raise ValueError("路径业务标识包含不支持的字符")
    return value


def execute(client, args):
    """执行已授权的管理子命令；归档只做有界批次，不自动重排或补发。"""
    game = segment(args.game)
    if args.command == "lookup":
        path = "/admin/events/" + "/".join(map(segment, (game, args.task_type, args.namespace, args.biz_key)))
        return client.request("GET", path)
    if args.command == "attempts":
        return client.request("GET", f"/admin/attempts/{game}/{args.task_id}/{args.after}")
    if args.command == "tasks":
        if args.created_from is not None and args.created_before is not None and args.created_from >= args.created_before:
            raise ValueError("创建时间范围无效")
        query = {}
        for name, key in (("status", "status"), ("server_id", "serverId"), ("created_from", "createdFrom"),
                          ("created_before", "createdBefore"), ("limit", "limit")):
            value = getattr(args, name)
            if value is not None:
                query[key] = value
        return client.request("GET", f"/admin/tasks/{game}/{args.after}?" + urllib.parse.urlencode(query))
    if args.command == "replay":
        return client.request("POST", f"/admin/tasks/{game}/{args.task_id}/replay")
    if args.command == "config-status":
        return client.request("GET", f"/admin/config-status/{game}/{args.after}")
    if args.command == "control-get":
        return client.request("GET", f"/admin/control/{game}")
    if args.command == "control-set":
        body = {"expectedRevision": args.expected_revision}
        for name, key in (("delivery_enabled", "deliveryEnabled"),
                          ("game_limit", "maxConcurrentDeliveries"), ("server_limit", "maxConcurrentPerServer")):
            value = getattr(args, name)
            if value is not None:
                body[key] = value == "true" if name == "delivery_enabled" else value
        if len(body) == 1:
            raise ValueError("控制更新至少指定一个变更字段")
        return client.request("POST", f"/admin/control/{game}", body)
    result = {"batches": 0, "archivedTasks": 0, "redactedEvents": 0, "exhausted": False}
    for _ in range(args.max_batches):
        value = client.request("POST", f"/admin/archive/{game}",
            {"keepDays": args.keep_days, "batchSize": args.batch_size, "redactPayload": args.redact_payload})
        counts = [value.get(key) for key in ("archivedTasks", "redactedEvents")]
        if any(type(count) is not int or count < 0 or count > args.batch_size for count in counts):
            raise ValueError("归档响应计数无效")
        result["batches"] += 1
        result["archivedTasks"] += counts[0]
        result["redactedEvents"] += counts[1]
        if not any(counts):
            result["exhausted"] = True
            break
    return result


def integer(minimum, maximum):
    """返回限定整数范围的 argparse 转换函数。"""
    def parse(value):
        result = int(value)
        if not minimum <= result <= maximum:
            raise argparse.ArgumentTypeError("整数参数超出范围")
        return result
    return parse


def parser():
    """构造命令行接口；凭据不会出现在命令行参数或输出中。"""
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--url", required=True)
    value.add_argument("--token-env", default="GAMEHUB_ADMIN_TOKEN")
    value.add_argument("--token-file", type=Path)
    value.add_argument("--timeout", type=integer(1, 60), default=5)
    value.add_argument("--budget", type=integer(1, 86400), default=60)
    value.add_argument("--output", type=Path)
    commands = value.add_subparsers(dest="command", required=True)
    for name in ("lookup", "tasks", "replay", "attempts", "config-status", "control-get", "control-set", "archive"):
        command = commands.add_parser(name)
        command.add_argument("--game", required=True)
        if name == "lookup":
            for field in ("task-type", "namespace", "biz-key"):
                command.add_argument("--" + field, required=True)
        if name in ("tasks", "attempts", "config-status"):
            command.add_argument("--after", type=integer(0, 2**63 - 1), default=0)
        if name in ("attempts", "replay"):
            command.add_argument("--task-id", type=integer(1, 2**63 - 1), required=True)
        if name == "tasks":
            command.add_argument("--status", type=integer(0, 3))
            command.add_argument("--server-id", type=integer(1, 0xffffffff))
            command.add_argument("--created-from", type=integer(0, MAX_UNIX_SECONDS))
            command.add_argument("--created-before", type=integer(0, MAX_UNIX_SECONDS))
            command.add_argument("--limit", type=integer(1, 100), default=100)
        if name == "control-set":
            command.add_argument("--expected-revision", type=integer(0, 2**63 - 1), required=True)
            command.add_argument("--delivery-enabled", choices=("true", "false"))
            command.add_argument("--game-limit", type=integer(0, 32))
            command.add_argument("--server-limit", type=integer(0, 32))
        if name == "archive":
            command.add_argument("--keep-days", type=integer(1, 36500), required=True)
            command.add_argument("--batch-size", type=integer(1, 1000), default=100)
            command.add_argument("--max-batches", type=integer(1, 10000), default=10)
            command.add_argument("--redact-payload", action="store_true")
    return value


def main():
    """执行管理操作，错误只报告固定类别，不输出令牌和第三方响应。"""
    args = parser().parse_args()
    try:
        validate_output(args.output, (args.token_file,))
        client = Client(args.url, load_token(args.token_env, args.token_file), args.timeout, args.budget)
        value = execute(client, args)
        content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if client.token in content:
            raise ValueError("管理响应包含认证凭据，已拒绝输出")
        if args.output:
            args.output.write_text(content, encoding="utf-8", newline="\n")
        else:
            print(content, end="")
        return 0
    except HttpStatusError as error:
        print("管理接口返回 HTTP " + str(error.status) + "，未自动重试；请核对权限和任务当前状态", file=sys.stderr)
        return 1
    except (OSError, ValueError, TimeoutError):
        print("管理操作失败，请检查地址、权限、参数与总预算；写操作状态请查询确认后再执行", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
