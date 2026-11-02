"""사용자 반응(좋아요, 별로예요, 신고)을 Langfuse 점수로 보내기. 실제 Langfuse 로는 보내지 않는다."""
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import main, observe
from app.main import app, store

client = TestClient(app)
USER = "local-user"  # 테스트는 AUTH=none 이라 모든 요청이 이 사용자다


@pytest.fixture
def scores(monkeypatch):
    """트레이스는 메모리 exporter 로 받고, create_score 는 받은 인자만 기록한다."""
    def use(capture):
        key = uuid.uuid4().hex
        settings = SimpleNamespace(langfuse_public_key=f"pk-{key}", langfuse_secret_key=f"sk-{key}",
                                   langfuse_base_url="http://127.0.0.1:9", env="test", commit="local",
                                   langfuse_capture_content=capture)
        tracer = observe.make_tracer(settings, span_exporter=InMemorySpanExporter())
        sent = []
        monkeypatch.setattr(tracer.client, "create_score", lambda **kw: sent.append(kw))
        monkeypatch.setattr(main, "tracer", tracer)
        return sent
    return use


def answer(message):
    lines = [json.loads(line) for line in client.post("/api/chat", json={"message": message}).text.splitlines()]
    return next(e["message_id"] for e in lines if e["type"] == "done")


def react(message_id, **body):
    return client.post("/api/report", json={"message_id": message_id, **body})


def test_answer_keeps_trace_id(scores):
    scores(capture=False)
    mid = answer("오늘 좀 피곤해")
    assert store.get_message(USER, mid)["meta"]["trace_id"]


def test_like_then_dislike_update_one_user_feedback_score(scores):
    sent = scores(capture=False)
    mid = answer("오늘 좀 피곤해")
    trace_id = store.get_message(USER, mid)["meta"]["trace_id"]
    assert react(mid, kind="like").json() == {"ok": True, "scored": True}
    assert react(mid, kind="dislike").json() == {"ok": True, "scored": True}
    assert [(s["name"], s["value"], s["data_type"]) for s in sent] == [("user-feedback", 1.0, "BOOLEAN"),
                                                                        ("user-feedback", 0.0, "BOOLEAN")]
    assert {s["score_id"] for s in sent} == {f"{trace_id}-user-feedback"}
    assert sent[0]["timestamp"] == sent[1]["timestamp"]  # 같은 id·이름·시각이라 덮어쓴다
    assert sent[0]["trace_id"] == trace_id


def test_report_sends_reason_only_when_capture_on(scores):
    sent = scores(capture=True)
    mid = answer("오늘 좀 피곤해")
    react(mid, kind="report", reason="이상해요. 연락은 010-1234-5678")
    assert sent[-1]["name"] == "report" and sent[-1]["value"] == 1.0 and sent[-1]["data_type"] == "BOOLEAN"
    assert sent[-1]["comment"] == "이상해요. 연락은 [전화번호]"

    sent = scores(capture=False)
    mid = answer("오늘 좀 피곤해")
    react(mid, kind="report", reason="이상해요")
    assert "comment" not in sent[-1]


def test_old_report_request_still_works(scores):
    sent = scores(capture=False)
    mid = answer("오늘 좀 피곤해")
    assert react(mid, reason="").status_code == 200  # kind 없이 보내던 예전 신고 요청
    assert sent[-1]["name"] == "report"


def test_cannot_score_someone_elses_message(scores):
    sent = scores(capture=False)
    other = store.add_message("someone-else", "assistant", "남의 답")
    assert react(other["id"], kind="like").status_code == 404
    assert sent == []


def test_cannot_score_user_message(scores):
    scores(capture=False)
    mine = store.add_message(USER, "user", "내가 보낸 말")
    assert react(mine["id"], kind="like").status_code == 404


def test_unknown_kind_is_rejected(scores):
    scores(capture=False)
    mid = answer("오늘 좀 피곤해")
    assert react(mid, kind="love").status_code == 422


def test_without_langfuse_keys_skips_score(monkeypatch):
    monkeypatch.setattr(main, "tracer", observe.DISABLED)
    mid = answer("오늘 좀 피곤해")
    assert react(mid, kind="like").json() == {"ok": True, "scored": False}
    assert react(mid, kind="report", reason="x").json() == {"ok": True, "scored": False}
