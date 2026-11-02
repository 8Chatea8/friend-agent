"""도구 선택 평가. data/eval/tool_selection.jsonl 의 메시지마다 고른 도구와 핵심 인자가 정답과 맞는지 센다.

실행:
  python scripts/eval_tools.py                    # .env 의 LLM 으로 모델 선택기를 평가 (코드의 프롬프트, 호출 40번, 비용 발생)
  python scripts/eval_tools.py --label staging    # Langfuse 프롬프트 관리의 tool-router(staging 라벨)로 평가
  python scripts/eval_tools.py --rule             # 키워드 규칙 선택기를 평가 (비용 없음)

--label 을 주면 .env 의 Langfuse 키로 그 라벨의 프롬프트를 가져온다. 가져오지 못하면 코드 문장으로 대신하지 않고 멈춘다.

정답의 날짜와 시각은 '2026-10-12(월) 14:00 한국 시간'을 지금으로 보고 만들었다. 평가도 그 시각을 지금으로 쓴다.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.llm import make_llm  # noqa: E402
from app.observe import make_tracer  # noqa: E402
from app.router import ROUTER_PROMPT_NAME, ROUTER_TEMPLATE, model_choose, rule_choose  # noqa: E402

NOW = datetime.fromisoformat("2026-10-12T14:00:00+09:00")
LOCATION = "서울"


def args_match(expected, actual):
    """정답에 적힌 인자만 비교한다. due_at 은 분 단위까지 같은 시각이면 맞다."""
    wrong = []
    for key, want in expected.items():
        got = actual.get(key)
        if key == "due_at":
            try:
                same = datetime.fromisoformat(str(got)).replace(tzinfo=None, second=0) == datetime.fromisoformat(want)
            except ValueError:
                same = False
        elif key in ("from", "to"):
            same = str(got or "")[:7] == want[:7]   # 월까지만 비교
        elif key == "day":   # 정답은 '오늘로부터 며칠 뒤', 결과는 date(YYYY-MM-DD)
            try:
                same = (datetime.fromisoformat(str(actual.get("date"))).date() - NOW.date()).days == want
            except ValueError:
                same = False
            got = actual.get("date")
        else:
            same = want in str(got or "")
        if not same:
            wrong.append(f"{key}: 정답 {want}, 결과 {got}")
    return wrong


def load_prompt(settings, label):
    """Langfuse 에서 tool-router 의 label 버전을 가져온다. 못 가져오면 None."""
    tracer = make_tracer(settings)
    if not tracer.enabled:
        print("LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 없어 --label 로 평가할 수 없습니다.")
        return None
    tracer.prompt_label = label
    prompt = tracer.get_prompt(ROUTER_PROMPT_NAME, fallback=ROUTER_TEMPLATE)
    if prompt is None:
        print(f"Langfuse 에서 {ROUTER_PROMPT_NAME}({label}) 프롬프트를 가져오지 못해 평가하지 않습니다.")
    return prompt


def main():
    parser = argparse.ArgumentParser(description="도구 선택 평가")
    parser.add_argument("--rule", action="store_true", help="키워드 규칙 선택기를 평가 (비용 없음)")
    parser.add_argument("--label", help=f"Langfuse 프롬프트 관리의 {ROUTER_PROMPT_NAME} 에서 이 라벨의 버전으로 평가 (예: staging, production)")
    args = parser.parse_args()
    use_rule = args.rule
    if use_rule and args.label:
        print("--rule 은 프롬프트를 쓰지 않으므로 --label 과 함께 쓸 수 없습니다.")
        return
    settings = None if use_rule else load_settings()
    llm = None if use_rule else make_llm(settings)
    if llm is not None and not llm.can_reason:
        print("LLM_PROVIDER=fake 에서는 모델 평가를 할 수 없습니다. --rule 로 실행하세요.")
        return
    prompt = None
    if args.label:
        prompt = load_prompt(settings, args.label)
        if prompt is None:
            return
    prompt_name = f"{ROUTER_PROMPT_NAME} v{prompt.version}({args.label})" if prompt else "코드의 프롬프트"

    rows = [json.loads(line) for line in (ROOT / "data/eval/tool_selection.jsonl").read_text(encoding="utf-8").splitlines()]
    tool_ok = args_ok = 0
    failures = []
    for row in rows:
        if use_rule:
            choice = rule_choose(row["message"], NOW, LOCATION)
        else:
            choice = model_choose(row["message"], row["history"], NOW, LOCATION, llm, prompt=prompt)
        picked = {t["name"] for t in choice.tools}
        required, optional = set(row["expected_tools"]), set(row["optional_tools"])
        tools_right = required <= picked <= required | optional
        wrong = []
        if tools_right:
            tool_ok += 1
            got = {t["name"]: t["args"] for t in choice.tools}
            for name, expected in row["expected_args"].items():
                wrong += args_match(expected, got.get(name, {}))
            if not wrong:
                args_ok += 1
        if not tools_right or wrong:
            failures.append((row, sorted(picked), wrong, choice.confidence))

    n = len(rows)
    print(f"[{'키워드 규칙' if use_rule else '모델 ' + llm.model + ', ' + prompt_name}] 도구 정답 {tool_ok}/{n}, 도구와 인자 모두 정답 {args_ok}/{n}")
    for row, picked, wrong, conf in failures:
        detail = "; ".join(wrong) if wrong else f"정답 {row['expected_tools'] or '도구 없음'}, 결과 {picked or '도구 없음'}"
        print(f"  {row['id']} {row['message']}  →  {detail}  (확신도 {conf:.2f})")


if __name__ == "__main__":
    main()
