"""resource.py 的 Qt 资源注册/注销入口（627 行）。"""


def test_qcleanup_resources_then_reinit():
    from src.app.common import resource

    resource.qCleanupResources()

    # 重新注册，避免影响依赖 :/qss 资源的后续测试
    resource.qInitResources()
