"""평가 데이터의 메시지를 내 컴퓨터에서 띄운 서버의 /api/chat 에 하나씩 보내는 스크립트.

data/eval/tool_selection.jsonl, data/eval/safety.jsonl, data/eval/memory_extraction.jsonl 순서로 보낸다.
- tool_selection, safety: 줄마다 message 를 보낸다.
- memory_extraction: conversation 에서 user 차례의 말만 보낸다. assistant 차례는 예시 답이라 보내지 않는다(서버가 답을 만든다).
응답 스트림을 끝까지 읽은 뒤 다음 메시지를 보낸다. 보낸 개수와 실패한 개수만 출력하고, 답 내용은 출력하지 않는다.
tool_selection.jsonl 의 history 는 보내지 않는다. /api/chat 은 서버가 저장한 대화 기록을 쓴다.

주의: 서버 설정에 따라 실제 AI를 불러 비용이 들고, 대화가 서버 저장소에 쌓이며,
Langfuse 키가 있으면 트레이스로도 전송된다. 처음에는 --limit 를 작게 두고 돌린다.

실행:
  python scripts/send_eval_messages.py               # 기본 5개, http://localhost:8080
  python scripts/send_eval_messages.py --limit 20
  python scripts/send_eval_messages.py --url http://localhost:8000
"""
import argparse
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "data" / "eval"
FILES = [EVAL / "tool_selection.jsonl", EVAL / "safety.jsonl", EVAL / "memory_extraction.jsonl"]


def messages_in(row):
    """평가 데이터 한 줄에서 보낼 사용자 말을 꺼낸다."""
    if row.get("message"):
        return [row["message"]]
    return [t["content"] for t in row.get("conversation") or [] if t.get("role") == "user" and t.get("content")]


def load_messages():
    messages = []
    for path in FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                messages.extend(messages_in(json.loads(line)))
    return messages


def send(client, url, message):
    """메시지 하나를 보내고 스트림을 끝까지 읽는다. done 이벤트를 받으면 성공."""
    done = False
    with client.stream("POST", url, json={"message": message}) as r:
        if r.status_code != 200:
            r.read()
            return False
        for line in r.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "error":
                return False
            if event.get("type") == "done":
                done = True
    return done


def main():
    parser = argparse.ArgumentParser(description="평가 메시지를 /api/chat 에 하나씩 보내기")
    parser.add_argument("--limit", type=int, default=5, help="보낼 메시지 개수(세 파일 합계), 기본 5")
    parser.add_argument("--url", default="http://localhost:8080", help="서버 주소, 기본 http://localhost:8080")
    args = parser.parse_args()
    url = args.url.rstrip("/") + "/api/chat"
    messages = load_messages()[:max(args.limit, 0)]

    sent, failed = 0, 0
    with httpx.Client(timeout=httpx.Timeout(10.0, read=120.0)) as client:
        for message in messages:
            sent += 1
            try:
                if not send(client, url, message):
                    failed += 1
            except (httpx.HTTPError, json.JSONDecodeError):
                failed += 1
    print(f"보낸 개수 {sent}, 실패한 개수 {failed}")


if __name__ == "__main__":
    main()
