"""核对隔离集成测试窗口内的死锁；原始日志和引擎锁图只在内存中分析。"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import re
import subprocess
import time

from integration_errors import IntegrationDeadlineExceeded


DEADLOCK_COUNTER_FIXED_VERSION = (8, 0, 18)
DEADLOCK_COUNTER_BUG = "https://bugs.mysql.com/bug.php?id=77399"
DEADLOCK_DETECTED = "并发重复回调期间检测到新的数据库死锁"
OBSERVATION_UNAVAILABLE = "死锁观测不可用，不能确认并发重复回调没有死锁"

_IDENTITY_SQL = ("SELECT @@server_uuid,VERSION(); SHOW GLOBAL STATUS WHERE Variable_name "
                 "IN ('Innodb_deadlocks','Uptime')")
_METRIC_SQL = "SELECT `COUNT`,STATUS FROM information_schema.INNODB_METRICS WHERE NAME='lock_deadlocks'"
_LOG_PREFIXES = tuple(message.encode("utf-8") for message in (
    "数据库死锁重试", "处理请求时依赖服务出错", "投递时数据库操作失败", "数据库或迁移检查失败"))
_LOG_CODE = re.compile(rb"category=mysql\.common-server code=1213(?![0-9A-Za-z_])")


def counter_reliability(version, source):
    """version 为服务器版本，source 为计数来源；返回与容量报告一致的可靠性分类。"""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]|$)", version)
    if not match or "mariadb" in version.lower():
        return "unverified_version"
    if tuple(int(part) for part in match.groups()) < DEADLOCK_COUNTER_FIXED_VERSION:
        return "known_undercount" if source == "innodb_metrics.lock_deadlocks" else "unverified_legacy_counter"
    return "no_known_undercount"


@dataclass(frozen=True)
class _Counter:
    value: int
    source: str
    reliability: str


@dataclass(frozen=True)
class _Snapshot:
    identity: tuple = field(repr=False)
    uptime: int
    observed: float
    generation: int
    counter: object
    engine: object
    logs: object


class _Unavailable(Exception):
    pass


class _Detected(Exception):
    pass


def _counter(fixture, version, fields, enable_metric):
    source = "global_status.Innodb_deadlocks"
    value = fields.get("Innodb_deadlocks")
    if value is None:
        source = "innodb_metrics.lock_deadlocks"
        try:
            metric = fixture.sql(_METRIC_SQL).split("\t")
            if len(metric) == 2 and metric[1] != "enabled" and enable_metric:
                fixture.sql("SET GLOBAL innodb_monitor_enable='lock_deadlocks'")
                metric = fixture.sql(_METRIC_SQL).split("\t")
            if len(metric) != 2 or metric[1] != "enabled":
                return None
            value = metric[0]
        except (TimeoutError, subprocess.TimeoutExpired, IntegrationDeadlineExceeded):
            raise
        except Exception:
            return None
    if not re.fullmatch(r"[0-9]+", value):
        return None
    return _Counter(int(value), source, counter_reliability(version, source))


def _engine_fingerprint(fixture):
    # 不保存整份报告；仅比较死锁段的摘要，避免监控时间等无关字段造成误报。
    status = fixture.sql("SHOW ENGINE INNODB STATUS").replace("\\n", "\n")
    if "INNODB MONITOR OUTPUT" not in status or not re.search(r"\n-+\nTRANSACTIONS\n", status):
        raise _Unavailable()
    _, heading, report = status.partition("LATEST DETECTED DEADLOCK\n")
    if not heading:
        return (None,)
    report = re.split(r"\n-+\nTRANSACTIONS\n", report, maxsplit=1)[0]
    if "TRANSACTION" not in report or "WE ROLL BACK TRANSACTION" not in report:
        raise _Unavailable()
    return (hashlib.sha256(report.encode("utf-8")).digest(),)


def _log_offsets(fixture):
    for log in fixture.logs:
        if not log.closed:
            log.flush()
    paths = tuple(fixture.path.glob("hub*.log"))
    if not paths:
        raise _Unavailable()
    return {path.name: path.stat().st_size for path in paths}


def _new_log_deadlock(fixture, offsets):
    current = _log_offsets(fixture)
    if any(name not in current or current[name] < size for name, size in offsets.items()):
        raise _Unavailable()
    for name in current:
        with (fixture.path / name).open("rb") as stream:
            stream.seek(offsets.get(name, 0))
            while line := stream.readline(8192):
                if any(prefix in line for prefix in _LOG_PREFIXES) and _LOG_CODE.search(line):
                    return True
    return False


def _optional(action):
    try:
        return action()
    except (TimeoutError, subprocess.TimeoutExpired, IntegrationDeadlineExceeded):
        raise
    except Exception:
        return None


def _snapshot(fixture, enable_metric):
    lines = fixture.sql(_IDENTITY_SQL).splitlines()
    if not lines or len(identity := tuple(lines[0].split("\t"))) != 2 or not all(identity):
        raise _Unavailable()
    fields = dict(line.split("\t") for line in lines[1:])
    if not re.fullmatch(r"[0-9]+", fields.get("Uptime", "")):
        raise _Unavailable()
    return _Snapshot(identity, int(fields["Uptime"]), time.monotonic(),
                     fixture.__dict__.get("database_generation", 0),
                     _counter(fixture, identity[1], fields, enable_metric),
                     _optional(lambda: _engine_fingerprint(fixture)),
                     _optional(lambda: _log_offsets(fixture)))


def _assert_unchanged(fixture, start, end):
    log_deadlock = _optional(lambda: _new_log_deadlock(fixture, start.logs)) if start.logs is not None else None
    if log_deadlock or (start.engine is not None and end.engine is not None
                        and end.engine != (None,) and end.engine != start.engine):
        raise _Detected()
    same_counter = (start.counter is not None and end.counter is not None
                    and start.counter.source == end.counter.source)
    if same_counter and end.counter.value > start.counter.value:
        raise _Detected()
    elapsed = end.observed - start.observed
    if (start.identity != end.identity or start.generation != end.generation
            or end.uptime + 2 < start.uptime + elapsed
            or (same_counter and end.counter.value < start.counter.value)
            or (start.engine is not None and start.engine != (None,) and end.engine == (None,))):
        raise _Unavailable()
    if same_counter and all(counter.reliability == "no_known_undercount" for counter in (start.counter, end.counter)):
        return
    # 旧 MySQL 可能漏计；缺失计数也只能由完整的日志窗口和前后引擎锁图共同补足。
    if start.engine is None or end.engine is None or log_deadlock is None:
        raise _Unavailable()


@contextmanager
def assert_no_new_deadlocks(fixture):
    """fixture 为本次隔离实例；包围待测操作，无新死锁时正常退出，否则抛固定文本的 AssertionError。"""
    try:
        start = _snapshot(fixture, enable_metric=True)
    except (TimeoutError, subprocess.TimeoutExpired, IntegrationDeadlineExceeded):
        raise
    except Exception:
        raise AssertionError(OBSERVATION_UNAVAILABLE) from None
    yield
    try:
        _assert_unchanged(fixture, start, _snapshot(fixture, enable_metric=False))
    except (TimeoutError, subprocess.TimeoutExpired, IntegrationDeadlineExceeded):
        raise
    except _Detected:
        raise AssertionError(DEADLOCK_DETECTED) from None
    except Exception:
        raise AssertionError(OBSERVATION_UNAVAILABLE) from None
