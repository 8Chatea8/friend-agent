"""Langfuse 데이터셋 tool-selection 으로 도구 선택 프롬프트(tool-router)를 라벨별로 실험하는 스크립트(langfuse SDK 5.x).

라벨마다 tool-router 프롬프트를 가져와, 데이터셋 항목마다 앱의 model_choose 로 도구를 고르고 정답과 비교한다.
- 실행 이름: tool-router-v<버전>-<라벨>-<회차> (예: tool-router-v1-production-1)
- 항목 점수(BOOLEAN): tool-correct(도구가 맞음), args-correct(도구와 정답 인자가 모두 맞음)
- 실행 점수(NUMERIC): tool-accuracy, args-accuracy
- 비교 방법과 기준 시각(2026-10-12 14:00 한국 시간)은 scripts/eval_tools.py 와 같다.
- 항목마다 choose-tools generation 에 가져온 프롬프트를 연결해, 트레이스에서 버전이 보인다.

주의: .env 의 LLM 을 항목마다 한 번씩 부른다(라벨 2개 × 2회 × 41개 = 164번, 비용 발생).
키는 .env 에서 읽고 출력하지 않는다.

실행:
  python scripts/run_experiments.py                                  # production, staging 을 2회씩
  python scripts/run_experiments.py --labels staging --rounds 1
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import load_settings  # noqa: E402
from app.llm import make_llm  # noqa: E402
from app.observe import make_tracer  # noqa: E402
from app.router import ROUTER_PROMPT_NAME, ROUTER_TEMPLATE, model_choose  # noqa: E402
from eval_tools import LOCATION, NOW, args_match  # noqa: E402

DATASET = "tool-selection"


def make_task(langfuse, llm, prompt):
    def task(*, item, **kwargs):
        message, history = item.input["message"], item.input.get("history") or []
        usage = {}
        with langfuse.start_as_current_observation(as_type="generation", name="choose-tools", model=llm.model,
                                                   prompt=prompt, input={"message": message, "history": history}) as gen:
            choice = model_choose(message, history, NOW, LOCATION, llm, usage=usage, prompt=prompt)
            output = {"tools": choice.tools, "confidence": choice.confidence, "reason": choice.reason}
            gen.update(output=output, usage_details=usage or None)
        return output
    return task


def check(output, expected):
    """eval_tools.py 와 같은 기준. (도구가 맞는지, 도구와 인자가 모두 맞는지, 틀린 내용)"""
    picked = {t["name"] for t in output["tools"]}
    required, optional = set(expected.get("tools") or []), set(expected.get("optional_tools") or [])
    tools_right = required <= picked <= required | optional
    wrong = []
    if tools_right:
        got = {t["name"]: t["args"] for t in output["tools"]}
        for name, want in (expected.get("args") or {}).items():
            wrong += args_match(want, got.get(name, {}))
    else:
        wrong.append(f"도구: 정답 {sorted(required) or '없음'}, 결과 {sorted(picked) or '없음'}")
    return tools_right, tools_right and not wrong, "; ".join(wrong)


def evaluate(*, input, output, expected_output, metadata=None, **kwargs):
    from langfuse import Evaluation

    tools_right, all_right, wrong = check(output, expected_output)
    return [Evaluation(name="tool-correct", value=1.0 if tools_right else 0.0, data_type="BOOLEAN"),
            Evaluation(name="args-correct", value=1.0 if all_right else 0.0, data_type="BOOLEAN",
                       comment=wrong or None)]


def accuracy(*, item_results, **kwargs):
    from langfuse import Evaluation

    def rate(name):
        values = [e.value for r in item_results for e in r.evaluations if e.name == name]
        return round(sum(values) / len(values), 3) if values else 0.0

    return [Evaluation(name="tool-accuracy", value=rate("tool-correct")),
            Evaluation(name="args-accuracy", value=rate("args-correct"))]


def main():
    parser = argparse.ArgumentParser(description="tool-router 프롬프트를 라벨별로 실험")
    parser.add_argument("--labels", nargs="+", default=["production", "staging"], help="실험할 라벨, 기본 production staging")
    parser.add_argument("--rounds", type=int, default=2, help="라벨마다 몇 번 돌릴지, 기본 2")
    parser.add_argument("--concurrency", type=int, default=5, help="동시에 처리할 항목 수, 기본 5")
    args = parser.parse_args()

    settings = load_settings()
    llm = make_llm(settings)
    if not llm.can_reason:
        sys.exit("LLM_PROVIDER=fake 에서는 실험할 수 없습니다.")
    tracer = make_tracer(settings)
    if not tracer.enabled:
        sys.exit("LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 없어 실험할 수 없습니다.")
    langfuse = tracer.client
    dataset = langfuse.get_dataset(DATASET)

    for label in args.labels:
        tracer.prompt_label = label
        prompt = tracer.get_prompt(ROUTER_PROMPT_NAME, fallback=ROUTER_TEMPLATE)
        if prompt is None:
            print(f"{ROUTER_PROMPT_NAME}({label}) 프롬프트를 가져오지 못해 건너뜁니다.")
            continue
        for round_no in range(1, args.rounds + 1):
            run_name = f"{ROUTER_PROMPT_NAME}-v{prompt.version}-{label}-{round_no}"
            result = dataset.run_experiment(
                name=ROUTER_PROMPT_NAME, run_name=run_name,
                description=f"{ROUTER_PROMPT_NAME} v{prompt.version} ({label} 라벨) {round_no}회차, 모델 {llm.model}",
                task=make_task(langfuse, llm, prompt), evaluators=[evaluate], run_evaluators=[accuracy],
                max_concurrency=args.concurrency,
                metadata={"prompt": ROUTER_PROMPT_NAME, "prompt_version": prompt.version, "label": label,
                          "round": round_no, "model": llm.model})
            scores = {e.name: e.value for e in result.run_evaluations}
            failed = sum(1 for r in result.item_results if r.output is None)
            print(f"{run_name}: 도구 정답률 {scores.get('tool-accuracy')}, 도구+인자 정답률 {scores.get('args-accuracy')}, "
                  f"항목 {len(result.item_results)}개, 실행 실패 {failed}개")
    langfuse.flush()


if __name__ == "__main__":
    main()
