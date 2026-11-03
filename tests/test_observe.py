"""Langfuse 계측. 실제 Langfuse 로 보내지 않고, 메모리 exporter 로 받은 span 을 확인한다."""
import json
import uuid
from types import SimpleNamespace

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import agent, observe
from app.llm import FakeLLM
from app.observe import redact
from app.store.memory_store import MemoryStore


def make(capture):
    exporter = InMemorySpanExporter()
    key = uuid.uuid4().hex  # Langfuse 클라이언트는 키마다 하나라서 테스트마다 새 키를 쓴다
    settings = SimpleNamespace(langfuse_public_key=f"pk-{key}", langfuse_secret_key=f"sk-{key}",
                               langfuse_base_url="http://127.0.0.1:9", env="test", commit="local",
                               langfuse_capture_content=capture)
    return observe.make_tracer(settings, span_exporter=exporter), exporter


def spans_by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


def attr(span, key):
    value = span.attributes.get(f"langfuse.observation.{key}")
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class TokenLLM:
    """토큰 수를 채우는 가짜 모델. 모델이 도구와 기억을 고르는 경로(can_reason)를 탄다."""
    can_reason = True
    model = "token-model"

    def complete_json(self, system, user, usage=None):
        usage.update(input=11, output=3)
        if "도구" in system:
            return {"tools": [], "confidence": 0.9, "reason": "일상 대화"}
        return {"add": [], "update": [], "remove": []}

    def complete_text(self, messages, usage=None):
        usage.update(input=7, output=2)
        return "안녕"

    def chat_stream(self, messages, usage=None):
        yield "그랬"
        yield "구나"
        usage.update(input=20, output=5)


def test_redact_masks_phone_email_card():
    text = "010-1234-5678, 01012345678, +82 10-1234-5678, 02-123-4567, a.b@example.co.kr, 1234-5678-9012-3456"
    assert redact(text) == "[전화번호], [전화번호], [전화번호], [전화번호], [이메일], [카드번호]"


def test_redact_keeps_dates_and_times():
    text = "2026-11-02 08:30에 만나, 3시 15분"
    assert redact(text) == text


def test_redact_walks_messages():
    messages = [{"role": "user", "content": "메일은 me@test.com"}]
    assert redact(messages) == [{"role": "user", "content": "메일은 [이메일]"}]


def test_chat_turn_structure_and_metadata():
    tracer, exporter = make(capture=True)
    list(agent.run_turn("오늘 좀 피곤해. 내 번호는 010-1234-5678", "u1", FakeLLM(delay=0), MemoryStore(), False, tracer))
    spans = spans_by_name(exporter)
    assert {"chat-turn", "safety", "choose-tools", "reply", "memory-update"} <= set(spans)
    root = spans["chat-turn"]
    for name in ("safety", "choose-tools", "reply", "memory-update"):
        assert spans[name].parent.span_id == root.context.span_id
    assert attr(spans["choose-tools"], "type") == "generation"
    assert attr(spans["choose-tools"], "model.name") == "fake"
    assert attr(spans["choose-tools"], "metadata.decided_by") == "rule"
    assert attr(spans["choose-tools"], "metadata.tools") == []
    assert attr(spans["reply"], "completion_start_time")
    assert "[전화번호]" in attr(root, "input") and "010-1234-5678" not in attr(root, "input")


def test_content_is_sizes_only_when_capture_off():
    tracer, exporter = make(capture=False)
    message = "오늘 좀 피곤해"
    list(agent.run_turn(message, "u1", FakeLLM(delay=0), MemoryStore(), False, tracer))
    root = spans_by_name(exporter)["chat-turn"]
    assert attr(root, "input") == {"chars": len(message)}
    assert message not in json.dumps([dict(s.attributes) for s in exporter.get_finished_spans()], ensure_ascii=False)


def test_generations_carry_token_usage():
    tracer, exporter = make(capture=False)
    list(agent.run_turn("오늘 좀 피곤해", "u1", TokenLLM(), MemoryStore(), False, tracer))
    spans = spans_by_name(exporter)
    assert attr(spans["reply"], "usage_details") == {"input": 20, "output": 5}
    assert attr(spans["choose-tools"], "usage_details") == {"input": 11, "output": 3}
    assert attr(spans["choose-tools"], "model.name") == "token-model"
    assert attr(spans["choose-tools"], "metadata.confidence") == 0.9
    assert attr(spans["choose-tools"], "metadata.decided_by") == "model"


def test_greeting_is_its_own_trace():
    tracer, exporter = make(capture=False)
    assert agent.greet("new-user", FakeLLM(delay=0), MemoryStore(), tracer)
    span = spans_by_name(exporter)["greeting"]
    assert span.parent is None
    assert attr(span, "type") == "generation"
    assert attr(span, "model.name") == "fake"


def test_tracing_is_off_without_keys():
    settings = SimpleNamespace(langfuse_public_key="", langfuse_secret_key="")
    assert observe.make_tracer(settings) is observe.DISABLED
