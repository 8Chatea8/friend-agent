"""도구 선택 프롬프트 A/B 테스트. 실제 Langfuse 에는 연결하지 않는다."""
import json
import uuid
from types import SimpleNamespace

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import agent, observe
from app.observe import Tracer, ab_variant
from app.router import ROUTER_PROMPT_NAME, ROUTER_TEMPLATE, router_prompt
from app.store.memory_store import MemoryStore


class Prompt:
    """Langfuse 에서 가져온 텍스트 프롬프트처럼 행동한다."""

    def __init__(self, label, version):
        self.name, self.version, self.labels, self.is_fallback = ROUTER_PROMPT_NAME, version, [label], False
        self.prompt = ROUTER_TEMPLATE

    def compile(self, **variables):
        return router_prompt()


class FakeClient:
    """라벨마다 정해 둔 프롬프트를 돌려준다. 없는 라벨은 SDK 처럼 fallback 프롬프트를 돌려준다."""

    def __init__(self, **versions):
        self.prompts = {label.replace("_", "-"): Prompt(label.replace("_", "-"), v) for label, v in versions.items()}
        self.asked = []

    def get_prompt(self, name, label=None, type=None, fallback=None):
        self.asked.append(label)
        return self.prompts.get(label) or SimpleNamespace(is_fallback=True)


def user_in(group):
    return next(f"user-{i}" for i in range(1000) if ab_variant(f"user-{i}") == group)


def test_same_user_always_same_group_and_split_is_even():
    assert {ab_variant("same-user") for _ in range(20)} == {ab_variant("same-user")}
    share_a = sum(ab_variant(str(uuid.uuid4())) == "a" for _ in range(2000)) / 2000
    assert 0.45 < share_a < 0.55


def test_off_uses_environment_label():
    client = FakeClient(staging=2, prod_a=3, prod_b=4)
    tracer = Tracer(client=client, prompt_label="staging", ab_test=False)
    prompt = tracer.get_prompt(ROUTER_PROMPT_NAME, ROUTER_TEMPLATE, variant=tracer.variant_for(user_in("a")))
    assert prompt.version == 2 and client.asked == ["staging"]


def test_on_uses_assigned_group_label():
    client = FakeClient(production=1, prod_a=3, prod_b=4)
    tracer = Tracer(client=client, prompt_label="staging", ab_test=True)
    assert tracer.get_prompt(ROUTER_PROMPT_NAME, variant=tracer.variant_for(user_in("a"))).version == 3
    assert tracer.get_prompt(ROUTER_PROMPT_NAME, variant=tracer.variant_for(user_in("b"))).version == 4
    assert client.asked == ["prod-a", "prod-b"]


def test_missing_group_label_falls_back_to_production():
    client = FakeClient(production=1, prod_a=3)
    tracer = Tracer(client=client, ab_test=True)
    assert tracer.get_prompt(ROUTER_PROMPT_NAME, variant="b").version == 1
    assert client.asked == ["prod-b", "production"]


def test_nothing_fetched_uses_code_prompt():
    tracer = Tracer(client=FakeClient(), ab_test=True)
    assert tracer.get_prompt(ROUTER_PROMPT_NAME, variant="a") is None


def test_trace_gets_ab_variant_and_linked_prompt(monkeypatch):
    class JsonLLM:
        can_reason, model = True, "json-model"

        def complete_json(self, system, user, usage=None):
            return {"tools": [], "confidence": 0.9, "reason": "일상"} if "도구" in system else {"add": []}

        def chat_stream(self, messages, usage=None):
            yield "응"

    key = uuid.uuid4().hex
    settings = SimpleNamespace(langfuse_public_key=f"pk-{key}", langfuse_secret_key=f"sk-{key}",
                               langfuse_base_url="http://127.0.0.1:9", env="staging", commit="c",
                               langfuse_capture_content=False, ab_test=True)
    exporter = InMemorySpanExporter()
    tracer = observe.make_tracer(settings, span_exporter=exporter)
    fake = FakeClient(production=1, prod_a=3, prod_b=4)
    monkeypatch.setattr(tracer.client, "get_prompt", fake.get_prompt)
    uid = user_in("b")
    list(agent.run_turn("안녕", uid, JsonLLM(), MemoryStore(), False, tracer))
    span = next(s for s in exporter.get_finished_spans() if s.name == "choose-tools")
    assert json.loads(span.attributes["langfuse.trace.metadata.ab_variant"]) == "b"
    assert span.attributes["langfuse.observation.prompt.name"] == ROUTER_PROMPT_NAME
    assert span.attributes["langfuse.observation.prompt.version"] == 4
    assert fake.asked == ["prod-b"]


def test_root_span_carries_ab_variant_confidence_and_safety(monkeypatch):
    """Langfuse 화면의 트레이스 metadata 는 루트 span 의 값이라, 루트에 있어야 그룹별로 거를 수 있다."""
    class JsonLLM:
        can_reason, model = True, "json-model"

        def complete_json(self, system, user, usage=None):
            return {"tools": [], "confidence": 0.9, "reason": "일상"}

        def chat_stream(self, messages, usage=None):
            yield "응"

    key = uuid.uuid4().hex
    settings = SimpleNamespace(langfuse_public_key=f"pk-{key}", langfuse_secret_key=f"sk-{key}",
                               langfuse_base_url="http://127.0.0.1:9", env="staging", commit="c",
                               langfuse_capture_content=False, ab_test=True)
    exporter = InMemorySpanExporter()
    tracer = observe.make_tracer(settings, span_exporter=exporter)
    monkeypatch.setattr(tracer.client, "get_prompt", FakeClient(production=1, prod_a=3, prod_b=4).get_prompt)
    uid = user_in("a")
    list(agent.run_turn("안녕", uid, JsonLLM(), MemoryStore(), False, tracer))
    root = next(s for s in exporter.get_finished_spans() if s.name == "chat-turn")
    assert json.loads(root.attributes["langfuse.trace.metadata.ab_variant"]) == "a"
    assert json.loads(root.attributes["langfuse.observation.metadata.confidence"]) == 0.9
    assert json.loads(root.attributes["langfuse.observation.metadata.safety"]) is False
