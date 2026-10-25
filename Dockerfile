# 에이전트 서비스(friend-agent) 실행 환경
# 1) 시작 이미지와 Python 버전을 고정한다.
FROM python:3.13-slim-bookworm

# 로그를 버퍼에 쌓지 않고 바로 내보내고, 실행 중에 .pyc 파일을 만들지 않는다.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 2) 운영체제 프로그램 설치: 사진 글자 읽기에 쓰는 tesseract 와 한국어 데이터. 설치 뒤 패키지 목록 캐시는 지운다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 3) 라이브러리 설치. requirements.txt 만 먼저 복사하면 코드만 바뀐 빌드에서 이 단계를 다시 쓰지 않는다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4) 코드 복사. 올리지 않을 파일은 .dockerignore 에 적는다(.env 포함).
COPY . .

# 5) 관리자(root)가 아닌 사용자로 실행한다.
RUN useradd --create-home appuser
USER appuser

# 6) Cloud Run이 정해 주는 PORT 환경변수로 실행한다. 없으면 8080.
#    exec 로 uvicorn 이 셸 대신 주 프로세스가 되어, Cloud Run의 종료 신호를 직접 받고 정리하며 끝난다.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
