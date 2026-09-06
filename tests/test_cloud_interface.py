"""CloudInterface（云盘页面）测试。

覆盖：_mask_username 表测试、页面真实构造（pan=None 不触网）、
set_pan 的三分支（正常用户名 / 无 user_name / None）与退出登录信号。
"""
from unittest.mock import MagicMock

import pytest

from src.app.view.cloud_interface import CloudInterface, _mask_username


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        (None, ""),
        ("13812345678", "138****5678"),  # 11 位手机号 → 隐藏中间 4 位
        ("1381234567", "1381234567"),  # 10 位不足不脱敏
        ("138123456789", "138123456789"),  # 12 位超长不脱敏
        ("abcdefghijk", "abcdefghijk"),  # 11 位非数字不脱敏
        ("alice", "alice"),  # 普通用户名
    ],
    ids=["empty", "none", "phone", "short", "long", "non-digit-11", "plain"],
)
def test_mask_username_parametrized(raw, expected):
    assert _mask_username(raw) == expected


class TestCloudInterface:
    def test_init_builds_account_cards(self, qapp):
        widget = CloudInterface()

        assert widget.pan is None
        assert widget.objectName() == "CloudInterface"
        assert widget.username_card is not None
        assert widget.logout_card is not None
        # 两张卡片都添加进了账户分组
        assert widget.username_card.parent() is widget.accountGroup
        assert widget.logout_card.parent() is widget.accountGroup

    def test_logout_card_click_emits_logout_requested(self, qapp):
        widget = CloudInterface()
        received = []
        widget.logoutRequested.connect(lambda: received.append(True))

        widget.logout_card.clicked.emit()

        assert received == [True]

    def test_set_pan_masks_phone_username(self, qapp):
        widget = CloudInterface()
        pan = MagicMock()
        pan.user_name = "13812345678"

        widget.set_pan(pan)

        assert widget.pan is pan
        assert widget.username_label.text() == "用户名: 138****5678"

    def test_set_pan_plain_username_shown_as_is(self, qapp):
        widget = CloudInterface()
        pan = MagicMock()
        pan.user_name = "alice"

        widget.set_pan(pan)

        assert widget.username_label.text() == "用户名: alice"

    def test_set_pan_without_user_name_keeps_label_empty(self, qapp):
        widget = CloudInterface()

        widget.set_pan(object())  # 无 user_name 属性

        assert widget.pan is not None
        assert widget.username_label.text() == ""

    def test_set_pan_none_keeps_label_empty(self, qapp):
        widget = CloudInterface()

        widget.set_pan(None)

        assert widget.pan is None
        assert widget.username_label.text() == ""
