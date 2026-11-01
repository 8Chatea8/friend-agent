"""LLM 호출을 한곳에 모아 둔다.

OpenAI와 Gemini는 같은 OpenAI SDK로 부르고, 주소(base_url)와 모델 이름만 다르다.
Claude는 Anthropic SDK로 따로 부른다. 앱 안에서 쓰는 메시지 형식은 OpenAI 형식이라서, ClaudeLLM이 Claude 형식으로 바꿔 보낸다.
fake 는 키 없이 화면과 흐름을 확인하거나 테스트할 때 쓰는 가짜 응답이다.

메서드마다 usage(dict)를 넘기면 입력·출력 토큰 수를 {"input": n, "output": m} 으로 채운다(Langfuse 계측용).
LLM 객체 하나를 여러 요청이 같이 쓰므로, 토큰 수를 객체에 저장하지 않고 부른 쪽이 넘긴 dict 에 채운다.
"""
import json
import re
import time


def _fill(usage, input_tokens, output_tokens):
    if usage is not None and input_tokens is not None and output_tokens is not None:
        usage.update(input=input_tokens, output=output_tokens)


class OpenAICompatibleLLM:
    can_reason = True  # 도구 선택, 기억 정리를 모델에게 맡길 수 있다

    def __init__(self, api_key, model, base_url=None):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)
        self.model = model

    @staticmethod
    def _usage(response, usage):
        if response.usage:
            _fill(usage, response.usage.prompt_tokens, response.usage.completion_tokens)

    def complete_json(self, system, user, usage=None):
        """도구 선택처럼 정해진 형식의 답이 필요할 때. JSON 하나를 받아 dict로 돌려준다."""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        self._usage(response, usage)
        return json.loads(response.choices[0].message.content)

    def complete_text(self, messages, usage=None):
        response = self.client.chat.completions.create(model=self.model, messages=messages)
        self._usage(response, usage)
        return response.choices[0].message.content.strip()

    def chat_stream(self, messages, usage=None):
        """답을 만들어지는 대로 조금씩 돌려준다(스트리밍). 토큰 수는 마지막 이벤트에 온다."""
        response = self.client.chat.completions.create(model=self.model, messages=messages, stream=True,
                                                       stream_options={"include_usage": True})
        for event in response:
            if event.choices and event.choices[0].delta.content:
                yield event.choices[0].delta.content
            if getattr(event, "usage", None):
                self._usage(event, usage)


class ClaudeLLM:
    can_reason = True

    # 안전 분류기가 요청을 거절하면, 같은 요청을 다른 Claude 모델이 이어서 답하게 한다.
    BETAS = ["server-side-fallback-2026-07-01"]
    # 대화 앱이라 생각 깊이(effort)를 낮게 둔다. 답이 빨라지고 비용이 줄어든다.
    OUTPUT_CONFIG = {"effort": "low"}
    MAX_TOKENS = 16000
    REFUSAL_NOTICE = "이 요청에는 답을 만들지 못했어요. 다른 말로 다시 보내 주세요."

    def __init__(self, api_key, model):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, timeout=60)
        self.model = model

    @staticmethod
    def to_claude(messages):
        """OpenAI 형식 메시지를 (system, messages)로 나눈다.

        Claude는 시스템 지시문을 messages 밖의 system 으로 받고, 대화가 user 차례로 시작해야 한다.
        같은 차례가 이어지면 하나로 합친다.
        """
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = []
        for m in messages:
            if m["role"] == "system":
                continue
            if turns and turns[-1]["role"] == m["role"]:
                turns[-1]["content"] += "\n\n" + m["content"]
            else:
                turns.append({"role": m["role"], "content": m["content"]})
        if not turns or turns[0]["role"] != "user":
            turns.insert(0, {"role": "user", "content": "(대화를 이어 갑니다)"})
        return system, turns

    def _request(self, system, turns):
        return dict(model=self.model, max_tokens=self.MAX_TOKENS, system=system, messages=turns,
                    output_config=self.OUTPUT_CONFIG, betas=self.BETAS, fallbacks="default")

    def _text(self, response):
        if response.stop_reason == "refusal":
            return self.REFUSAL_NOTICE
        return "".join(block.text for block in response.content if block.type == "text").strip()

    @staticmethod
    def _usage(response, usage):
        u = getattr(response, "usage", None)
        if u is not None:
            _fill(usage, getattr(u, "input_tokens", None), getattr(u, "output_tokens", None))

    def complete_json(self, system, user, usage=None):
        """정해진 형식의 답이 필요할 때. 답에서 JSON 객체 하나를 찾아 dict로 돌려준다."""
        system = system + "\n\n반드시 JSON 객체 하나만 출력한다. 설명이나 코드 블록 표시는 붙이지 않는다."
        response = self.client.beta.messages.create(**self._request(system, [{"role": "user", "content": user}]))
        self._usage(response, usage)
        if response.stop_reason == "refusal":
            raise ValueError("Claude가 요청을 거절했습니다.")
        return parse_json_object(self._text(response))

    def complete_text(self, messages, usage=None):
        system, turns = self.to_claude(messages)
        response = self.client.beta.messages.create(**self._request(system, turns))
        self._usage(response, usage)
        return self._text(response)

    def chat_stream(self, messages, usage=None):
        """답을 만들어지는 대로 조금씩 돌려준다(스트리밍)."""
        system, turns = self.to_claude(messages)
        with self.client.beta.messages.stream(**self._request(system, turns)) as stream:
            sent = False
            for text in stream.text_stream:
                sent = True
                yield text
            final = stream.get_final_message()
            self._usage(final, usage)
            if final.stop_reason == "refusal" and not sent:
                yield self.REFUSAL_NOTICE


def parse_json_object(text):
    """모델 답에서 첫 번째 JSON 객체를 꺼낸다. 없으면 ValueError."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("JSON 응답을 찾지 못했습니다.")
    return json.loads(match.group(0))


class FakeLLM:
    """AI를 부르지 않는다. 도구 선택과 기억 정리는 키워드 규칙이 맡고(can_reason=False),
    답은 지시문에 들어 있는 도구 결과와 캐릭터 이름으로 짧게 만든다.

    비용이 들지 않고 같은 입력에는 늘 같은 답을 돌려주므로, 키 없이 화면을 확인하거나 테스트할 때 쓴다.
    """

    can_reason = False
    model = "fake"

    def __init__(self, delay=0.02):
        self.delay = delay

    def _reply(self, messages):
        system = messages[0]["content"]
        polite = "존댓말" in system
        name = re.search(r"이름: (.+)", system)
        name = name.group(1).strip() if name else "친구"
        tool_lines = []
        if "[도구 결과]" in system:
            block = system.split("[도구 결과]", 1)[1].split("\n\n", 1)[0]
            tool_lines = [line[2:] for line in block.splitlines() if line.startswith("- ")]
        user = messages[-1]["content"] if messages[-1]["role"] == "user" else ""

        if "[되묻기]" in system:
            body = "무슨 말인지 조금 더 알려 줄래요?" if polite else "무슨 말인지 조금만 더 알려 줄래?"
        elif tool_lines:
            body = " ".join(tool_lines)
        elif user.startswith("(앱을 열었다"):  # 먼저 말 걸기
            body = f"안녕하세요, {name}예요. 오늘은 어떻게 지내요?" if polite else f"안녕, 나 {name}! 오늘은 어떻게 지냈어?"
        else:
            body = "그랬군요. 조금 더 이야기해 줄래요?" if polite else "그랬구나. 조금 더 얘기해 줄래?"
        return f"(가짜 응답) {body}"

    # 가짜 LLM은 토큰을 쓰지 않으므로 usage 를 채우지 않는다
    def complete_text(self, messages, usage=None):
        return self._reply(messages)

    def chat_stream(self, messages, usage=None):
        text = self._reply(messages)
        for i in range(0, len(text), 4):
            time.sleep(self.delay)
            yield text[i:i + 4]


def make_llm(settings):
    if settings.provider == "fake":
        return FakeLLM()
    if settings.provider == "claude":
        return ClaudeLLM(settings.api_key, settings.model)
    return OpenAICompatibleLLM(settings.api_key, settings.model, settings.base_url)
