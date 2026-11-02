"""data/eval/ 의 평가 데이터를 Langfuse 데이터셋으로 올리는 스크립트(langfuse Python SDK 5.x).

| 파일 | 데이터셋 | input | expected_output |
| --- | --- | --- | --- |
| tool_selection.jsonl | tool-selection | message | tools, args, optional_tools (+ next_holiday) |
| memory_extraction.jsonl | memory-extraction | conversation | must_include, must_not_include |
| safety.jsonl | safety | message | expected |
| holiday_reply.jsonl | holiday-reply | message, tool_result | must_include, must_include_any, must_not_include, criteria |

- metadata 에는 원래 id 와 category 를 넣는다(holiday-reply 의 category 는 failure 또는 normal).
- 항목 id 는 '<데이터셋 이름>-<원래 id>' 로 고정한다. create_dataset_item 은 같은 id 가 있으면 고친다(upsert).
  그래서 다시 실행해도 항목이 늘지 않는다. 데이터셋 이름을 붙이는 이유는, 항목 id 가 데이터셋 안에서만
  유일한지 프로젝트 전체에서 유일해야 하는지 문서에 없어서 다른 데이터셋의 같은 id 와 겹치지 않게 하려는 것이다.
- '다음 공휴일'을 묻는 항목(T26 등)은 expected_output 에 next_holiday 를 더한다. 앱의 공휴일 도구와 같은
  출처(Nager.Date)에서, 이 스크립트를 실행한 시각의 한국 날짜 기준(오늘 포함) 다음 공휴일 날짜를 구한다.
  평가 직전에 다시 실행하면 그날 기준으로 고쳐진다.
- 키는 .env 의 LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL 에서 읽고 출력하지 않는다.
  끝나면 데이터셋별 항목 수만 출력한다.

실행:
  python scripts/upload_datasets.py            # 올리기
  python scripts/upload_datasets.py --dry-run  # 올리지 않고 읽은 항목 수와 next_holiday 만 확인
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv  # noqa: E402  (.env 를 앱과 같은 방식으로 읽는다)
from app.tools import holidays_of  # noqa: E402  (앱의 공휴일 도구와 같은 출처)

EVAL = ROOT / "data" / "eval"
KST = ZoneInfo("Asia/Seoul")


def read_jsonl(name):
    return [json.loads(line) for line in (EVAL / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def next_holiday(today):
    """오늘(포함) 이후 첫 공휴일 날짜. 올해에 없으면 내년에서 찾는다."""
    for year in (today.year, today.year + 1):
        for day, _name in sorted(holidays_of(year)):
            if day >= today:
                return day.isoformat()
    return None


def tool_selection_items(today):
    items = []
    for row in read_jsonl("tool_selection.jsonl"):
        expected = {"tools": row.get("expected_tools") or [], "args": row.get("expected_args") or {},
                    "optional_tools": row.get("optional_tools") or []}
        if "다음 공휴일" in row["message"]:
            expected["next_holiday"] = next_holiday(today)
        items.append((row["id"], {"message": row["message"]}, expected, row.get("category")))
    return items


def memory_extraction_items(today):
    return [(row["id"], {"conversation": row["conversation"]},
             {"must_include": row.get("must_include") or [], "must_not_include": row.get("must_not_include") or []},
             row.get("category"))
            for row in read_jsonl("memory_extraction.jsonl")]


def safety_items(today):
    return [(row["id"], {"message": row["message"]}, {"expected": row.get("expected")}, row.get("category"))
            for row in read_jsonl("safety.jsonl")]


def holiday_reply_items(today):
    return [(row["id"], {"message": row["message"], "tool_result": row["tool_result"]},
             {"must_include": row.get("must_include") or [], "must_include_any": row.get("must_include_any") or [],
              "must_not_include": row.get("must_not_include") or [], "criteria": row.get("criteria", "")},
             row["category"])
            for row in read_jsonl("holiday_reply.jsonl")]


DATASETS = [
    ("tool-selection", "도구 선택 평가. input.message 에 맞는 도구와 인자", tool_selection_items),
    ("memory-extraction", "기억 정리 평가. 대화에서 남겨야 할 것과 남기면 안 되는 것", memory_extraction_items),
    ("safety", "안전장치 평가. 위기 표현, 프롬프트 주입, 연애 감정 요청", safety_items),
    ("holiday-reply", "공휴일 답변 평가. 고정한 도구 결과로 답을 만들 때의 통과 조건", holiday_reply_items),
]


def make_client():
    load_dotenv(ROOT / ".env")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not (public_key and secret_key):
        sys.exit("LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 .env 에 없어 올리지 않습니다.")
    from langfuse import Langfuse

    options = {"public_key": public_key, "secret_key": secret_key}
    base_url = os.environ.get("LANGFUSE_BASE_URL", "").strip().rstrip("/")
    if base_url:
        options["base_url"] = base_url
    return Langfuse(**options)


def ensure_dataset(client, name, description):
    """데이터셋이 없을 때만 만든다. 이름은 프로젝트 안에서 유일하다."""
    try:
        client.get_dataset(name)
    except Exception:
        client.create_dataset(name=name, description=description)


def main():
    parser = argparse.ArgumentParser(description="data/eval 을 Langfuse 데이터셋으로 올리기")
    parser.add_argument("--dry-run", action="store_true", help="올리지 않고 읽은 항목 수와 next_holiday 만 출력")
    args = parser.parse_args()
    today = datetime.now(KST).date()

    if args.dry_run:
        print(f"오늘(한국) {today}, next_holiday {next_holiday(today)}")
        for name, _description, build in DATASETS:
            print(f"{name}: {len(build(today))}")
        return

    client = make_client()
    for name, description, build in DATASETS:
        ensure_dataset(client, name, description)
        for item_id, input_, expected, category in build(today):
            client.create_dataset_item(dataset_name=name, id=f"{name}-{item_id}", input=input_,
                                       expected_output=expected, metadata={"id": item_id, "category": category})
    client.flush()
    for name, _description, _build in DATASETS:
        print(f"{name}: {len(client.get_dataset(name).items)}")


if __name__ == "__main__":
    main()
