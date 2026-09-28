import os
from app import runner


def test_parse_script_json():
    # Valid json
    valid = runner._parse_script_json('{"ok": true, "message": "签到成功"}')
    assert valid["ok"] is True
    assert valid["message"] == "签到成功"

    # Json with leading log lines
    leading = runner._parse_script_json('some debug logs...\n{"ok": true, "points": 50}')
    assert leading["ok"] is True
    assert leading["points"] == 50

    # Empty
    empty = runner._parse_script_json("")
    assert empty["ok"] is False

    # Invalid text
    invalid = runner._parse_script_json("not a json at all")
    assert invalid["ok"] is False


def test_is_network_error():
    assert runner._is_network_error({"error_type": "network"}) is True
    assert runner._is_network_error({"error": "请求超时 Connection timeout"}) is True
    assert runner._is_network_error({"error": "密码错误"}) is False


def test_summarize():
    status, conclusion, mlabel, mvalue, streak = runner._summarize(
        "jiaobenwang", {"ok": True, "message": "成功", "points": "120"}
    )
    assert status == "success"
    assert mlabel == "贡献分"
    assert mvalue == "120"

    status_fail, conclusion_fail, _, _, _ = runner._summarize(
        "jiaobenwang", {"ok": False, "error": "网络异常"}
    )
    assert status_fail == "failed"
    assert "网络异常" in conclusion_fail
