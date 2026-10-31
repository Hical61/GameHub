"""供集成测试共用的异常，不依赖 Fixture 或其他测试模块。"""


class IntegrationDeadlineExceeded(Exception):
    """整轮测试超过时限，随后仍要保存诊断并停止本次进程。"""
