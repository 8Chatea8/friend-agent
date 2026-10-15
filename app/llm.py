"""LLM 호출을 한곳에 모아 둔다.

OpenAI와 Gemini는 같은 OpenAI SDK로 부르고, 주소(base_url)와 모델 이름만 다르다.
fake 는 키 없이 화면과 흐름을 확인하거나 테스트할 때 쓰는 가짜 응답이다.
"""
import json
import re
import time


class OpenAICompatibleLLM:
    can_reason = True  # 도구 선택, 기억 정리를 모델에게 맡길 수 있다

    def __init__(self, api_key, model, base_url=None):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)
        self.model = model

    def complete_json(self, system, user):
        """도구 선택처럼 정해진 형식의 답이 필요할 때. JSON 하나를 받아 dict로 돌려준다."""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)

    def complete_text(self, messages):
        response = self.client.chat.completions.create(model=self.model, messages=messages)
        return response.choices[0].message.content.strip()

    def chat_stream(self, messages):
        """답을 만들어지는 대로 조금씩 돌려준다(스트리밍)."""
        response = self.client.chat.completions.create(model=self.model, messages=messages, stream=True)
        for event in response:
            if event.choices and event.choices[0].delta.content:
                yield event.choices[0].delta.content


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

    def complete_text(self, messages):
        return self._reply(messages)

    def chat_stream(self, messages):
        text = self._reply(messages)
        for i in range(0, len(text), 4):
            time.sleep(self.delay)
            yield text[i:i + 4]


def make_llm(settings):
    if settings.provider == "fake":
        return FakeLLM()
    return OpenAICompatibleLLM(settings.api_key, settings.model, settings.base_url)
