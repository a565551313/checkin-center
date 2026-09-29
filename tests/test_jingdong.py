"""京东签到站点的等效实现测试：全部使用 mock HTTP 与假凭据，不碰真实网络。"""
import importlib.util
import json
from pathlib import Path

import pytest
import requests

from app import config, runner

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "app" / "scripts"

FAKE_PT_KEY = "FAKE_PT_KEY_FOR_TEST_ONLY"
FAKE_PT_PIN = "jd_testuser1"


def load_jd_module():
    spec = importlib.util.spec_from_file_location(
        "jd_checkin", SCRIPTS_DIR / "京东签到.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


jd = load_jd_module()


class FakeResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """可编排的假会话：get 固定返回，post 按队列依次返回响应或抛异常。"""

    def __init__(self, get_resp=None):
        self.get_resp = get_resp
        self.post_queue = []
        self.gets = []
        self.posts = []

    def get(self, url, **kw):
        self.gets.append(url)
        return self.get_resp

    def post(self, url, **kw):
        self.posts.append(url)
        item = self.post_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_steps():
    messages = []

    def step(msg):
        messages.append(msg)

    return step, messages


def ok_session():
    return FakeSession(get_resp=FakeResp('{"retcode":"0","data":{}}'))


# ---------- 凭据解析 ----------

def test_parse_cookie_value_ok():
    key, pin = jd.parse_cookie_value("pt_key=AAA;pt_pin=BBB;")
    assert key == "AAA"
    assert pin == "BBB"


def test_parse_cookie_value_missing():
    assert jd.parse_cookie_value("pt_key=AAA;") == (None, None)
    assert jd.parse_cookie_value("") == (None, None)
    assert jd.parse_cookie_value(None) == (None, None)


def test_load_credentials_from_env(monkeypatch):
    monkeypatch.setenv("JD_PT_KEY", FAKE_PT_KEY)
    monkeypatch.setenv("JD_PT_PIN", FAKE_PT_PIN)
    assert jd.load_credentials(None) == (FAKE_PT_KEY, FAKE_PT_PIN)


def test_load_credentials_from_file_fields(tmp_path, monkeypatch):
    monkeypatch.delenv("JD_PT_KEY", raising=False)
    monkeypatch.delenv("JD_PT_PIN", raising=False)
    p = tmp_path / "cred.md"
    p.write_text("pt_key：FILEKEY123\npt_pin：jd_fileuser9\n", encoding="utf-8")
    assert jd.load_credentials(str(p)) == ("FILEKEY123", "jd_fileuser9")


def test_load_credentials_from_cookie_line(tmp_path, monkeypatch):
    monkeypatch.delenv("JD_PT_KEY", raising=False)
    monkeypatch.delenv("JD_PT_PIN", raising=False)
    p = tmp_path / "cred.md"
    p.write_text("Cookie：pt_key=CK123;pt_pin=jd_cookieuser2;\n", encoding="utf-8")
    assert jd.load_credentials(str(p)) == ("CK123", "jd_cookieuser2")


def test_crypto_mask():
    assert jd.crypto_mask("jd_testuser1") == "j***1"
    assert jd.crypto_mask("") == "未设置"
    assert jd.crypto_mask("ab") == "***"


# ---------- Cookie 校验 ----------

def test_validate_cookie_ok():
    assert jd.validate_cookie(ok_session(), "pt_key=A;pt_pin=B;") is True


def test_validate_cookie_invalid():
    s = FakeSession(get_resp=FakeResp('{"retcode":"1004","data":{}}'))
    assert jd.validate_cookie(s, "pt_key=A;pt_pin=B;") is False


# ---------- 领豆响应解析 ----------

def test_parse_claim_success():
    r = jd.parse_claim_response(
        '{"code":"0","message":"success",'
        '"data":{"dailyAward":{"beanAward":{"beanCount":"5"}}}}')
    assert r["status"] == "success"
    assert r["beans"] == "5"


def test_parse_claim_already():
    r = jd.parse_claim_response('{"code":"0","message":"今日已签到","data":{}}')
    assert r["status"] == "already"


def test_parse_claim_busy():
    r = jd.parse_claim_response(
        '{"code":"1001","message":"当前签到人数较多，请稍晚再来"}')
    assert r["status"] == "busy"
    assert r["message"] == "当前签到人数较多，请稍晚再来"


def test_parse_claim_cookie_invalid():
    r = jd.parse_claim_response('{"code":"3","message":"登录已过期，请重新登录"}')
    assert r["status"] == "cookie_invalid"


def test_parse_claim_rejected():
    r = jd.parse_claim_response('{"code":"2001","message":"活动太火爆"}')
    assert r["status"] == "rejected"


# ---------- 完整流程（mock） ----------

def test_run_checkin_success(monkeypatch):
    monkeypatch.setattr(jd, "RETRY_DELAY", 0)
    s = ok_session()
    s.post_queue = [
        FakeResp('{"code":"0","message":"ok","data":{}}'),  # signBeanIndex：未签到
        FakeResp('{"code":"0","message":"success",'
                 '"data":{"dailyAward":{"beanAward":{"beanCount":"5"}}}}'),
    ]
    step, messages = make_steps()
    res = jd.run_checkin(s, FAKE_PT_KEY, FAKE_PT_PIN, step)
    assert res["ok"] is True
    assert res["beans"] == "5"
    assert "5 京豆" in res["message"]
    assert "正在验证 Cookie" in messages
    assert "Cookie 有效，正在领取京豆" in messages
    assert "签到成功" in messages


def test_run_checkin_already_via_index(monkeypatch):
    monkeypatch.setattr(jd, "RETRY_DELAY", 0)
    s = ok_session()
    s.post_queue = [FakeResp('{"code":"0","message":"今日已签到","data":{}}')]
    step, messages = make_steps()
    res = jd.run_checkin(s, FAKE_PT_KEY, FAKE_PT_PIN, step)
    assert res["ok"] is True
    assert res["already"] is True
    assert len(s.posts) == 1  # 只查了状态，没有再调领取接口


def test_run_checkin_busy_no_retry(monkeypatch):
    """京东明确业务拒绝（人数较多）时不再重试，直接透出京东原话。"""
    monkeypatch.setattr(jd, "RETRY_DELAY", 0)
    s = ok_session()
    s.post_queue = [
        FakeResp('{"code":"0","message":"ok","data":{}}'),
        FakeResp('{"code":"1001","message":"当前签到人数较多，请稍晚再来"}'),
    ]
    step, messages = make_steps()
    res = jd.run_checkin(s, FAKE_PT_KEY, FAKE_PT_PIN, step)
    assert res["ok"] is False
    assert res["error_type"] == "platform_rejected"
    assert res["error"] == "京东签到未通过：当前签到人数较多，请稍晚再来"
    assert len(s.posts) == 2  # index + 1 次领取，没有重试
    assert any("京东返回：当前签到人数较多，请稍晚再来" in m for m in messages)


def test_run_checkin_cookie_invalid():
    s = FakeSession(get_resp=FakeResp('{"retcode":"1004","data":{}}'))
    step, messages = make_steps()
    res = jd.run_checkin(s, FAKE_PT_KEY, FAKE_PT_PIN, step)
    assert res["ok"] is False
    assert res["error_type"] == "cookie_invalid"
    assert res["error"] == "签到接口拒绝了当前 Cookie，请重新录入后再试"


def test_run_checkin_retry_then_success(monkeypatch):
    """第一次领取网络异常，第二次成功：验证有限重试。"""
    monkeypatch.setattr(jd, "RETRY_DELAY", 0)
    s = ok_session()
    s.post_queue = [
        FakeResp('{"code":"0","message":"ok","data":{}}'),
        requests.RequestException("connection reset"),
        FakeResp('{"code":"0","message":"success",'
                 '"data":{"dailyAward":{"beanAward":{"beanCount":"8"}}}}'),
    ]
    step, messages = make_steps()
    res = jd.run_checkin(s, FAKE_PT_KEY, FAKE_PT_PIN, step)
    assert res["ok"] is True
    assert res["beans"] == "8"
    assert len(s.posts) == 3  # index + 2 次领取
    assert any("第 2 次" in m for m in messages)


def test_run_checkin_network_all_fail(monkeypatch):
    """3 次领取全部网络异常：最终报网络异常。"""
    monkeypatch.setattr(jd, "RETRY_DELAY", 0)
    s = ok_session()
    s.post_queue = [
        FakeResp('{"code":"0","message":"ok","data":{}}'),
        requests.RequestException("timeout"),
        requests.RequestException("timeout"),
        requests.RequestException("timeout"),
    ]
    step, messages = make_steps()
    res = jd.run_checkin(s, FAKE_PT_KEY, FAKE_PT_PIN, step)
    assert res["ok"] is False
    assert res["error_type"] == "network"
    assert "网络异常" in res["error"]
    assert len(s.posts) == 4  # index + 3 次领取


def test_run_checkin_dry_run():
    s = ok_session()
    step, messages = make_steps()
    res = jd.run_checkin(s, FAKE_PT_KEY, FAKE_PT_PIN, step, dry_run=True)
    assert res["ok"] is True
    assert res["dry_run"] is True
    assert s.posts == []  # dry-run 不调任何领取接口


# ---------- 与现有框架的集成 ----------

def test_runner_summarize_jingdong():
    status, conclusion, mlabel, mvalue, streak = runner._summarize(
        "jingdong",
        {"ok": True, "message": "签到成功，获得 5 京豆", "beans": "5"},
    )
    assert status == "success"
    assert mlabel == "京豆"
    assert mvalue == "5"
    assert "5 京豆" in conclusion


def test_config_jingdong_registered():
    site = config.SITES["jingdong"]
    assert site["name"] == "京东"
    assert site["script"] == "京东签到.py"
    assert (config.SCRIPTS_DIR / site["script"]).is_file()
    assert set(site["cred_env"]) == {"login", "password"}
    field_keys = [f["key"] for f in site["fields"]]
    assert field_keys == ["login", "password"]
