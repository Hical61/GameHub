"""用隔离MySQL验证查单、共享控制、配置回报和指标，不读取已有业务库。"""
import argparse
import json
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from integration import Fixture, order, run_fixture, wait_for
from admin_token import token


def verify_lookup(fixture):
    """fixture为已就绪的隔离实例；验证元信息、尝试历史及权限，不返回玩家载荷。"""
    operator = fixture.admin()
    viewer = fixture.admin(role="viewer", games=["demo"])
    path = "/api/callback/demo/demo"
    status, accepted = fixture.request("POST", path, order("ops-api-order"), signed=True)
    assert status == 200
    wait_for(lambda: fixture.tasks(accepted["eventId"]) == [2], "查单样本投递完成")
    lookup = "/admin/events/demo/recharge_notify/demo/ops-api-order"
    status, event = fixture.request("GET", lookup, admin=viewer)
    assert status == 200 and event["id"] == accepted["eventId"]
    assert "payload" not in event and "playerId" not in json.dumps(event)
    task_id = event["tasks"][0]["id"]
    status, history = fixture.request("GET", f"/admin/attempts/demo/{task_id}/0", admin=viewer)
    assert status == 200 and history["items"][-1]["outcome"] == "success"
    read_paths = (lookup, f"/admin/attempts/demo/{task_id}/0", "/admin/tasks/demo/0?status=2&limit=1",
                  "/admin/control/demo", "/admin/config-status/demo/0", "/admin/runtime/demo", "/admin/stats/demo")
    for read_path in read_paths:
        assert fixture.request("GET", read_path, admin=viewer)[0] == 200
        assert fixture.request("GET", read_path, admin=fixture.admin(games=["unauthorized"]))[0] == 403
        assert fixture.request("GET", read_path)[0] == 401
    assert fixture.request("GET", "/admin/metrics", admin=viewer)[0] == 403
    assert fixture.request("GET", "/admin/runtime", admin=viewer)[0] == 403
    assert fixture.request("GET", "/admin/stats", admin=viewer)[0] == 403
    replay = f"/admin/tasks/demo/{task_id}/replay"
    assert fixture.request("POST", replay, admin=viewer)[0] == 403
    assert fixture.request("POST", replay, admin=fixture.admin(games=["unauthorized"]))[0] == 403
    assert fixture.request("POST", replay, admin=operator)[0] == 409
    status, page = fixture.request("GET", "/admin/tasks/demo/0?status=2&serverId=1&limit=2", admin=viewer)
    assert status == 200 and len(page["items"]) <= 2
    assert all(item["status"] == 2 and item["serverId"] == 1 for item in page["items"])
    for query in ("status=4", "limit=0", "limit=101", "serverId=0", "serverId=4294967296",
                  "createdFrom=-1", "createdBefore=253402300800", "status=1&status=2", "unknown=1",
                  "createdFrom=20&createdBefore=10"):
        assert fixture.request("GET", "/admin/tasks/demo/0?" + query, admin=operator)[0] == 400
    assert fixture.request("GET", "/admin/events/demo/recharge_notify/demo/missing", admin=viewer)[0] == 404
    return task_id


def verify_control(fixture):
    """共享开关使用CAS更新，重启仍保留，viewer不可修改，恢复不改变原订单身份。"""
    operator = fixture.admin()
    viewer = fixture.admin(role="viewer")
    path = "/admin/control/demo"
    status, initial = fixture.request("GET", path, admin=operator)
    assert status == 200
    revision = int(initial["revision"])
    change = {"expectedRevision": revision, "deliveryEnabled": False}
    assert fixture.request("POST", path, change, admin=viewer)[0] == 403
    assert fixture.request("POST", path, change, admin=fixture.admin(games=["unauthorized"]))[0] == 403
    status, paused = fixture.request("POST", path, change, admin=operator)
    assert status == 200 and not paused["deliveryEnabled"]
    assert fixture.request("POST", path, change, admin=operator)[0] == 409
    status, event = fixture.request("POST", "/api/callback/demo/demo", order("ops-api-paused"), signed=True)
    assert status == 200
    fixture._stop_child(fixture.hub)
    fixture.start_hub()
    runtime = fixture.request("GET", "/admin/runtime/demo", admin=operator)[1]
    assert not runtime["enabled"] and int(runtime["controlRevision"]) == int(paused["revision"])
    assert fixture.tasks(event["eventId"]) == [0]
    status, resumed = fixture.request("POST", path,
        {"expectedRevision": int(paused["revision"]), "deliveryEnabled": True,
         "maxConcurrentDeliveries": 2, "maxConcurrentPerServer": 1}, admin=operator)
    assert status == 200 and resumed["deliveryEnabled"]
    wait_for(lambda: fixture.tasks(event["eventId"]) == [2], "共享控制恢复原任务")


def verify_config(fixture):
    """配置摘要受签名及版本约束；旧报告无法覆盖新版本。"""
    operator = fixture.admin()
    viewer = fixture.admin(role="viewer", games=["demo"])
    # 让模拟接收端走通知->拉取->持久化应用->签名回报，另一服覆盖不启用配置回报的路径。
    receiver = fixture.receivers[0]
    receiver.hub = urlsplit(f"http://127.0.0.1:{fixture.http_port}")
    status, _ = fixture.request("POST", "/admin/config/demo/101",
        {"bizKey": "config:101", "serverId": 0, "content": {"syntheticValue": 1}}, admin=operator)
    assert status == 200
    status, config = fixture.request("GET", "/internal/config/demo/1/101", signed=True)
    assert status == 200
    report = {"version": 101, "contentHash": config["sha256"]}
    endpoint = "/internal/config-applied/demo/1"
    assert fixture.request("POST", endpoint, report)[0] == 401
    for method, internal_path, body in (("GET", "/internal/config/demo/1/101", None), ("POST", endpoint, report)):
        assert fixture.request(method, internal_path, body, signed=True, key_id="unknown-internal")[0] == 401
    assert fixture.request("POST", endpoint, dict(report, contentHash="0" * 64), signed=True)[0] == 409
    assert fixture.request("POST", endpoint, report, signed=True)[0] == 200
    assert fixture.request("POST", endpoint, report, signed=True)[0] == 200
    assert fixture.request("POST", "/admin/config/demo/102",
        {"bizKey": "config:102", "serverId": 0, "content": {"syntheticValue": 1.0}}, admin=operator)[0] == 200
    status, newer = fixture.request("GET", "/internal/config/demo/1/102", signed=True)
    assert status == 200
    assert fixture.request("POST", endpoint, {"version": 102, "contentHash": newer["sha256"]}, signed=True)[0] == 200
    assert fixture.request("POST", endpoint, report, signed=True)[0] == 409
    wait_for(lambda: (receiver.config_status() or {}).get("version") == 102, "接收端应用浮点配置并持久化")
    status, states = fixture.request("GET", "/admin/config-status/demo/0", admin=viewer)
    assert status == 200 and any(int(row["version"]) == 102 and row["serverId"] == 1 for row in states["items"])
    assert fixture.request("POST", "/internal/config-applied/demo/999", report, signed=True)[0] == 404


def verify_rotation(fixture):
    """服务实际验证JWT和回调新旧密钥；未知kid不能退回空密钥或legacy密钥。"""
    config = json.loads(fixture.config.read_text(encoding="utf-8"))
    fixture.env["GAMEHUB_TEST_JWT_NEW"] = secrets.token_hex(32)
    fixture.env["GAMEHUB_TEST_CALLBACK_NEW"] = secrets.token_hex(32)
    fixture.env["GAMEHUB_TEST_OUTBOUND_NEW"] = secrets.token_hex(32)
    fixture.env["GAMEHUB_TEST_EMPTY_SIGNATURE"] = ""
    config["auth"].update(jwtSecretEnv="GAMEHUB_TEST_JWT_NEW", jwtKeyId="admin-new",
        jwtPreviousKeys=[{"keyId": "admin-old", "secretEnv": "GAMEHUB_ADMIN_JWT_SECRET"}],
        jwtLegacySecretEnv="GAMEHUB_ADMIN_JWT_SECRET")
    config["games"][0]["channels"][0].update(secretEnv="GAMEHUB_TEST_CALLBACK_NEW", keyId="callback-new",
        previousKeys=[{"keyId": "callback-old", "secretEnv": "GAMEHUB_DEMO_CALLBACK_SECRET"}],
        legacySecretEnv="GAMEHUB_DEMO_CALLBACK_SECRET")
    config["games"][0].update(outboundSecretEnv="GAMEHUB_TEST_OUTBOUND_NEW", outboundKeyId="server-new",
        outboundPreviousKeys=[{"keyId": "server-old", "secretEnv": "GAMEHUB_DEMO_OUTBOUND_SECRET"}],
        outboundLegacySecretEnv="GAMEHUB_DEMO_OUTBOUND_SECRET")
    fixture._stop_child(fixture.hub)
    for receiver in fixture.receivers:
        receiver.secret = fixture.env["GAMEHUB_TEST_OUTBOUND_NEW"]
        receiver.key_id = "server-new"
        receiver.previous_keys = {"server-old": fixture.env["GAMEHUB_DEMO_OUTBOUND_SECRET"]}
        receiver.legacy_secret = fixture.env["GAMEHUB_DEMO_OUTBOUND_SECRET"]
    fixture.config.write_text(json.dumps(config), encoding="utf-8")
    fixture.start_hub()
    def jwt(secret, kid):
        return token(secret, "synthetic-operator", "operator", ["demo"], key_id=kid)
    for secret, kid in ((fixture.env["GAMEHUB_TEST_JWT_NEW"], "admin-new"),
                        (fixture.env["GAMEHUB_ADMIN_JWT_SECRET"], "admin-old"),
                        (fixture.env["GAMEHUB_ADMIN_JWT_SECRET"], "")):
        assert fixture.request("GET", "/admin/runtime/demo", admin=jwt(secret, kid))[0] == 200
    for secret, kid in (("", "unknown"), (fixture.env["GAMEHUB_ADMIN_JWT_SECRET"], "unknown"),
                        (fixture.env["GAMEHUB_TEST_JWT_NEW"], "")):
        assert fixture.request("GET", "/admin/runtime/demo", admin=jwt(secret, kid))[0] == 401
    for kid, env_name in (("callback-new", "GAMEHUB_TEST_CALLBACK_NEW"),
                          ("callback-old", "GAMEHUB_DEMO_CALLBACK_SECRET"), ("", "GAMEHUB_DEMO_CALLBACK_SECRET")):
        assert fixture.request("POST", "/api/callback/demo/demo", order("rotation-" + (kid or "legacy")),
            signed=True, key_id=kid, secret_env=env_name)[0] == 200
    assert fixture.request("POST", "/api/callback/demo/demo", order("rotation-unknown"),
        signed=True, key_id="unknown", secret_env="GAMEHUB_DEMO_CALLBACK_SECRET")[0] == 401

    for kid, environment in (("server-new", "GAMEHUB_TEST_OUTBOUND_NEW"),
                              ("server-old", "GAMEHUB_DEMO_OUTBOUND_SECRET"), ("", "GAMEHUB_DEMO_OUTBOUND_SECRET")):
        status, latest = fixture.request("GET", "/internal/config/demo/1/0", signed=True, key_id=kid, secret_env=environment)
        assert status == 200
        assert fixture.request("POST", "/internal/config-applied/demo/1",
            {"version": latest["version"], "contentHash": latest["sha256"]}, signed=True, key_id=kid,
            secret_env=environment)[0] == 200
    for kid, environment in (("unknown", "GAMEHUB_DEMO_OUTBOUND_SECRET"),
                              ("unknown", "GAMEHUB_TEST_EMPTY_SIGNATURE"), ("", "GAMEHUB_TEST_OUTBOUND_NEW")):
        assert fixture.request("GET", "/internal/config/demo/1/0", signed=True, key_id=kid, secret_env=environment)[0] == 401
        assert fixture.request("POST", "/internal/config-applied/demo/1", {"version": latest["version"],
            "contentHash": latest["sha256"]}, signed=True, key_id=kid, secret_env=environment)[0] == 401


def verify_timeout_metrics(fixture):
    """真实超时产生互斥结果；ACK丢失后按原任务恢复，模拟账本仍只有一笔。"""
    receiver = fixture.receivers[0]
    pause = threading.Event()
    receiver.pause = pause
    try:
        status, event = fixture.request("POST", "/api/callback/demo/demo", order("ops-metric-timeout"), signed=True)
        assert status == 200
        event_id = int(event["eventId"])
        wait_for(lambda: fixture.sql(f"SELECT last_error FROM gamehub.delivery_task WHERE event_id={event_id}") == "network_timeout",
                 "真实超时被持久记录")
    finally:
        pause.set()
        receiver.pause = None
    # 基础故障套件会把maxAttempts降为1；同一任务显式重排仍保留业务身份。
    state, task_id = fixture.sql(f"SELECT status,id FROM gamehub.delivery_task WHERE event_id={event_id}").split("\t")
    if int(state) == 3:
        assert fixture.request("POST", f"/admin/tasks/demo/{task_id}/replay", admin=fixture.admin())[0] == 200
    wait_for(lambda: fixture.tasks(event["eventId"]) == [2], "超时任务以原身份恢复")
    assert fixture.ledger(1, "ops-metric-timeout") == 1


def verify(fixture):
    """对已就绪fixture执行新运营API验收，无返回值。"""
    verify_lookup(fixture)
    verify_control(fixture)
    verify_config(fixture)
    # 控制测试重启过进程；耗时指标仅在当前进程实际处理请求后出现。
    status, sample = fixture.request("POST", "/api/callback/demo/demo", order("ops-metric-sample"), signed=True)
    assert status == 200
    wait_for(lambda: fixture.tasks(sample["eventId"]) == [2], "当前进程指标样本完成")
    verify_timeout_metrics(fixture)
    status, metrics = fixture.request("GET", "/admin/metrics", admin=fixture.admin(role="viewer"))
    assert status == 200
    for metric in ("gamehub_db_queries_total", "gamehub_delivery_results_total", "gamehub_callback_seconds_bucket",
                   "gamehub_delivery_seconds_bucket", "gamehub_game_tasks", "# TYPE gamehub_completion_seconds histogram"):
        assert metric in metrics["message"], "当前进程缺少预期指标：" + metric
    values = dict(line.rsplit(" ", 1) for line in metrics["message"].splitlines() if line and not line.startswith("#"))
    results = {name: int(value) for name, value in values.items()
               if name.startswith('gamehub_delivery_results_total{game="demo",')}
    succeeded = results.get('gamehub_delivery_results_total{game="demo",result="success"}', 0)
    failed = int(values['gamehub_delivery_failures_total{game="demo"}'])
    assert results.get('gamehub_delivery_results_total{game="demo",result="timeout"}', 0) >= 1
    assert sum(results.values()) == succeeded + failed, "投递结果分类必须互斥，不能重复累计超时或总失败"
    verify_rotation(fixture)
    print("通过：业务查单、持久尝试、筛选授权、共享控制重启、配置应用与密钥轮换")


def main():
    """仅接受二进制和MySQL工具目录，由Fixture创建并清理独立实例。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--mysql-bin", required=True, type=Path)
    args = parser.parse_args()
    run_fixture(Fixture(args.binary, args.mysql_bin), verifier=verify)


if __name__ == "__main__":
    main()
