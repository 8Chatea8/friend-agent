"""Langfuse 계측. 한 턴(chat-turn)과 먼저 말 걸기(greeting)를 트레이스로, 그 안의 단계를 span·generation 으로 남긴다.

- LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 둘 다 있을 때만 켠다. 없으면 아무것도 하지 않는 객체를 돌려준다.
- 입력·출력 원문은 LANGFUSE_CAPTURE_CONTENT=true 일 때만 보낸다. 그때도 전화번호, 이메일, 카드 번호는 가린다.
  꺼져 있으면 글자 수, 항목 수 같은 크기만 보낸다. 대화는 개인정보라서 기본값은 끔이다.
- metadata 에는 도구 이름, 확신도, 개수처럼 원문이 아닌 값만 넣는다.
- 계측이 실패해도 대화 처리는 멈추지 않는다.

/api/chat 은 스트리밍 생성기라서 이벤트를 꺼낼 때마다 다른 스레드에서 이어 돌 수 있다.
그래서 "지금 열린 span"(컨텍스트 변수)에 기대지 않고, 부모 관찰 객체를 직접 넘겨 자식을 만든다.

트레이스 속성(user_id, session_id, version, tags, metadata)은 Langfuse 권장 방식인 propagate_attributes 로 붙인다.
같은 이유로 턴 전체를 감싸지 않고, span 을 만드는 순간에만 짧게 감싼다. 이미 만든 span 에는 나중에 붙일 수 없으므로,
턴 중간에 알게 된 값(도구, 확신도, 안전 응답 여부)은 set_trace() 뒤에 만드는 span 부터 붙는다.
Langfuse 는 트레이스 속성이 어느 span 에 있든 트레이스 전체의 속성으로 다루고, 태그는 모든 span 의 것을 합친다.
값이 다르게 붙으면 어느 쪽을 쓸지 문서에 없으므로, 모르는 동안에는 임시값을 붙이지 않는다.
"""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.logs import log

KST = ZoneInfo("Asia/Seoul")

# 순서가 중요하다. 카드 번호를 먼저 가려야 그 일부가 전화번호로 잘못 가려지지 않는다.
MASKS = [
    (re.compile(r"(?<!\d)(?:\d{4}[- ]?){3}\d{4}(?!\d)|(?<!\d)\d{4}[- ]?\d{6}[- ]?\d{5}(?!\d)"), "[카드번호]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[이메일]"),
    (re.compile(r"(?<!\d)(?:\+82[- .]?|0)\d{1,2}[- .]?\d{3,4}[- .]?\d{4}(?!\d)"), "[전화번호]"),
]


def redact(value):
    """문자열(그리고 dict, list 안의 문자열)에서 전화번호, 이메일, 카드 번호를 가린다."""
    if isinstance(value, str):
        for pattern, label in MASKS:
            value = pattern.sub(label, value)
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def size_of(value):
    """원문 대신 보낼 크기 정보."""
    if value is None:
        return None
    if isinstance(value, str):
        return {"chars": len(value)}
    if isinstance(value, (list, tuple)):
        chars = sum(len(v.get("content", "")) if isinstance(v, dict) else len(str(v)) for v in value)
        return {"items": len(value), "chars": chars}
    if isinstance(value, dict):
        return {"keys": sorted(str(k) for k in value)}
    return {"type": type(value).__name__}


def now():
    return datetime.now(timezone.utc)


class Observation:
    """Langfuse 관찰 객체 하나를 감싼다. raw 가 None 이면 아무것도 하지 않는다.

    attrs 는 트레이스 속성이다. 한 트레이스의 루트와 자식이 같은 dict 를 함께 쓴다.
    """

    def __init__(self, raw=None, capture=False, attrs=None, tracer=None):
        self.raw = raw
        self.capture = capture
        self.attrs = attrs if attrs is not None else {}
        self.tracer = tracer

    def get_prompt(self, name, fallback=None):
        """Langfuse 프롬프트 관리에서 프롬프트를 가져온다. 못 가져오면 None(코드 문장을 쓴다)."""
        return self.tracer.get_prompt(name, fallback) if self.tracer else None

    @property
    def trace_id(self):
        """이 관찰이 속한 트레이스 ID. 메시지 meta 에 저장해 두었다가 사용자 반응 점수를 붙인다."""
        return getattr(self.raw, "trace_id", None)

    def set_trace(self, metadata=None, tags=None):
        """턴 중간에 알게 된 트레이스 속성. 이 뒤에 만드는 span 부터 붙는다."""
        if metadata:
            self.attrs.setdefault("metadata", {}).update({k: v for k, v in metadata.items() if v is not None})
        if tags:
            self.attrs["tags"] = list(dict.fromkeys([*self.attrs.get("tags", []), *tags]))
        return self

    def _propagate(self):
        from langfuse import propagate_attributes

        return propagate_attributes(**{k: v for k, v in self.attrs.items() if v not in (None, [], {})})

    def _content(self, value):
        return redact(value) if self.capture else size_of(value)

    def _call(self, what, fn):
        if self.raw is None:
            return None
        try:
            return fn()
        except Exception as error:  # 계측 실패로 대화가 멈추면 안 된다
            log("WARNING", "Langfuse 계측 실패", step=what, error=type(error).__name__)
            return None

    def _child(self, name, as_type, input=None, metadata=None, model=None, prompt=None):
        kwargs = {"name": name, "as_type": as_type, "metadata": metadata}
        if input is not None:
            kwargs["input"] = self._content(input)
        if model is not None:
            kwargs["model"] = model
        if prompt is not None:  # 트레이스에서 프롬프트 이름과 버전이 보인다
            kwargs["prompt"] = prompt

        def start():
            with self._propagate():  # 만드는 순간에만 감싼다. 스트리밍 중 다른 스레드로 넘어가도 문제없다
                return self.raw.start_observation(**kwargs)

        raw = self._call(name, start)
        return Observation(raw, self.capture, self.attrs, self.tracer)

    def span(self, name, input=None, metadata=None):
        return self._child(name, "span", input, metadata)

    def generation(self, name, model, input=None, metadata=None, prompt=None):
        return self._child(name, "generation", input, metadata, model, prompt)

    def update(self, input=None, output=None, metadata=None, model=None, usage=None,
               completion_start_time=None, level=None, status_message=None):
        kwargs = {}
        if input is not None:
            kwargs["input"] = self._content(input)
        if output is not None:
            kwargs["output"] = self._content(output)
        if metadata:
            kwargs["metadata"] = metadata
        if model is not None:
            kwargs["model"] = model
        if usage:  # {"input": 토큰 수, "output": 토큰 수}. 가짜 LLM처럼 모르면 넣지 않는다
            kwargs["usage_details"] = usage
        if completion_start_time is not None:
            kwargs["completion_start_time"] = completion_start_time
        if level is not None:
            kwargs["level"] = level
        if status_message is not None:
            kwargs["status_message"] = status_message
        if kwargs:
            self._call("update", lambda: self.raw.update(**kwargs))
        return self

    def end(self, **kwargs):
        if kwargs:
            self.update(**kwargs)
        self._call("end", lambda: self.raw.end())


NOOP = Observation()


class Tracer:
    def __init__(self, client=None, capture=False, version="local", commit="local", prompt_label="staging"):
        self.client = client
        self.capture = capture
        self.version = version  # Cloud Run 이 넣어 주는 K_REVISION. 없으면 local
        self.commit = commit    # COMMIT_SHA
        self.prompt_label = prompt_label  # ENV 가 production 이면 production, 그 밖에는 staging
        self._prompt_warned = set()

    def get_prompt(self, name, fallback=None):
        """프롬프트 관리에서 prompt_label 의 버전을 가져온다. SDK 가 캐시하므로 매번 서버에 묻지 않는다.

        키가 없거나, 가져오지 못해 SDK 가 fallback 문장을 돌려주면 None 을 돌려준다.
        그러면 부른 쪽이 코드에 남겨 둔 같은 문장을 쓰고, 그 generation 에는 프롬프트를 연결하지 않는다.
        """
        if self.client is None:
            return None
        try:
            prompt = self.client.get_prompt(name, label=self.prompt_label, type="text", fallback=fallback)
        except Exception as error:
            prompt, reason = None, type(error).__name__
        else:
            reason = "fallback" if getattr(prompt, "is_fallback", False) else None
        if reason is None:
            return prompt
        if name not in self._prompt_warned:  # 같은 경고가 턴마다 쌓이지 않게 한 번만 남긴다
            self._prompt_warned.add(name)
            log("WARNING", "Langfuse 프롬프트를 가져오지 못해 코드 문장을 씁니다", prompt=name,
                label=self.prompt_label, reason=reason)
        return None

    @property
    def enabled(self):
        return self.client is not None

    def trace(self, name, as_type="span", input=None, metadata=None, model=None,
              user_id=None, trace_metadata=None, tags=None):
        """새 트레이스의 루트 관찰을 연다. 트레이스 이름은 루트 관찰 이름을 따른다.

        user_id 는 서버가 로그인으로 확인한 사용자 ID 다(이메일은 넣지 않는다).
        session_id 는 사용자 ID 와 한국 날짜로 만들어, 같은 날의 대화를 한 세션으로 묶는다.
        """
        if self.client is None:
            return NOOP
        attrs = {"version": self.version, "metadata": {"commit": self.commit}}
        if user_id:
            attrs["user_id"] = str(user_id)
            attrs["session_id"] = f"{user_id}:{datetime.now(KST).date().isoformat()}"
        root = Observation(self.client, self.capture, attrs, self)
        root.set_trace(metadata=trace_metadata, tags=tags)
        return root._child(name, as_type, input, metadata, model)

    def score(self, trace_id, name, value, comment=None, timestamp=None):
        """트레이스에 BOOLEAN 점수(1 또는 0)를 붙인다. 보냈으면 True.

        키가 없거나 trace_id 가 없으면(계측 전에 만든 메시지 등) 보내지 않고 False 를 돌려준다.
        score_id 를 '<트레이스 ID>-<점수 이름>' 으로 고정하고 timestamp 를 같게 보내면, 같은 점수를 다시 보낼 때
        새로 쌓이지 않고 덮어쓴다(예: 좋아요를 별로예요로 바꿀 때).
        comment 는 사용자가 쓴 글이라 원문 설정(capture)이 켜져 있을 때만, 가려서 보낸다.
        """
        if self.client is None or not trace_id:
            return False
        options = {"name": name, "value": float(value), "trace_id": trace_id, "data_type": "BOOLEAN",
                   "score_id": f"{trace_id}-{name}"}
        if timestamp is not None:
            options["timestamp"] = timestamp
        if comment and self.capture:
            options["comment"] = redact(comment)[:500]
        try:
            self.client.create_score(**options)
        except Exception as error:
            log("WARNING", "Langfuse 점수 보내기 실패", score=name, error=type(error).__name__)
            return False
        self.flush()
        return True

    def flush(self):
        """Cloud Run 은 응답을 닫은 뒤 CPU 를 거의 주지 않으므로, 응답을 닫기 전에 보낸다."""
        if self.client is None:
            return
        try:
            self.client.flush()
        except Exception as error:
            log("WARNING", "Langfuse 전송 실패", error=type(error).__name__)


DISABLED = Tracer()


def make_tracer(settings, **client_options):
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return DISABLED
    from langfuse import Langfuse

    options = {"public_key": settings.langfuse_public_key, "secret_key": settings.langfuse_secret_key,
               "environment": settings.env, "release": settings.commit}
    if settings.langfuse_base_url:
        options["base_url"] = settings.langfuse_base_url
    client = Langfuse(**options, **client_options)
    return Tracer(client, settings.langfuse_capture_content,
                  version=getattr(settings, "revision", None) or "local", commit=settings.commit,
                  prompt_label="production" if settings.env == "production" else "staging")
