import os
from app import notifier


def test_build_report_text():
    results = [
        {
            "site": "脚本王",
            "status": "成功",
            "conclusion": "签到成功",
            "metric_value": "100",
            "streak": "3",
            "note": "账号 u***@qq.com",
        },
        {
            "site": "皮卡丘token商店",
            "status": "失败",
            "conclusion": "登录失败",
            "metric_value": "—",
            "streak": "—",
            "note": "账号 p***@gmail.com",
        },
    ]
    text = notifier.build_report_text(
        mode="scheduled",
        business_date="2026-09-29",
        total=2,
        succeeded=1,
        failed=1,
        results=results,
    )
    assert "自动定时签到 报告" in text
    assert "共 2 个账号 | ✅ 成功 1 | ❌ 失败 1" in text
    assert "脚本王" in text
    assert "皮卡丘token商店" in text
