# Stock Lab · 모의투자 연구실

Debian 13의 Docker Compose, FastAPI, PostgreSQL로 실행하는 개인 모의투자 서비스입니다. 토스 시세를 사용하지만 실제 증권 주문을 보내는 경로는 없습니다. 전략 수익성은 검증되지 않았습니다.

## 현재 서버

- 프로젝트: `/opt/stock-lab`
- 사이트: `https://stock.eshiro.net`
- 서버 구성: `app` 1개, PostgreSQL `db` 1개. 여러 앱 워커를 같은 DB에 연결하지 않습니다.
- 사용자의 현재 실험 원금: KRW 1,000,000 + USD 1,000. 통화별로 관리하며 환전하지 않습니다.
- 새 실험 화면 기본값도 KRW 1,000,000 + USD 1,000입니다. 신규 DB를 처음 생성하는 구버전 기본값은 KRW 10,000,000 + USD 10,000이므로 필요한 원금은 새 실험 화면에서 명시합니다. 기존 DB는 재시작으로 초기화하지 않습니다.

## 분석·매매 흐름

장중 전략은 디렉터의 조사 지시 → 기업·상품 / 단기 차트 / 뉴스·공시 분석 → 반대 검토 → 디렉터의 최종 전략 순서입니다. 세 분석가는 병렬로 조사하며 한 사이클에 Gemini 요청 6회를 예약합니다. 기본 전략은 5역할·5회입니다.

서버가 가상 현금, 통화별 평가자산, 종목 한도, 최우선 호가 수량, 손절 위험을 검토해 정수 주식 수량을 계산합니다. ETF의 레버리지 배수를 가격에 다시 곱하지 않으며 차입·공매도는 지원하지 않습니다. 실제 검색 출처 근거가 부족하면 거래 제안을 제한합니다. 출처 링크가 있다는 사실만으로 기사 내용이나 게시일이 독립 검증된 것은 아닙니다.

- **시작:** 선택한 직접 승인 또는 자동 모의매매 방식으로 분석을 실행합니다.
- **중지:** 분석, 미승인 제안, 손절·익절·보유시간 감시와 청산 대기를 중지합니다. 보유 주식은 유지합니다.
- **전량 매도 후 종료:** 별도 확인 후 거래 가능한 최신 호가에서 모의청산을 시도합니다. 거래 불가·호가 부족이면 기다립니다.
- **앱 재시작:** 원금·현금·보유분·거래·실험 기록을 보존하고 중지 상태로 복구합니다. 자동으로 분석을 재개하지 않습니다.

토스에는 조회 API만 호출합니다. 정규장, 시세·호가 시각, 유효한 호가와 잔량을 검사하며 실패한 실제 시세를 시험 가격으로 대체하지 않습니다. 호가 조회 지연과 시장 급변으로 손절 가격·보유시간 준수를 보장할 수 없습니다. 거래비용은 설정한 시뮬레이션 가정값입니다.

## Gemini 설정과 무료 사용

서버 `.env`의 다음 항목을 사용합니다. 비밀값을 터미널 출력이나 채팅에 붙여넣지 않습니다.

```dotenv
MARKET_MODE=toss
TOSS_CLIENT_ID=발급받은_ID
TOSS_CLIENT_SECRET=발급받은_비밀값
GEMINI_API_KEY=발급받은_키
GEMINI_MODEL=gemini-3.8-flash
AI_DAILY_CALL_LIMIT=30
```

키 이름과 모델명만 바꾸는 OpenAI 호환 방식이 아닙니다. 현재 코드는 Gemini `generateContent`, JSON 출력, Google Search grounding 응답을 직접 처리합니다. 기업·뉴스 역할은 검색을 사용합니다. Gemini 2.x의 검색 요청에는 JSON 스키마를 프롬프트로 전달하고 반환값을 서버에서 검증하며, 다른 요청에는 구조화 출력 스키마를 사용합니다. 미완료·차단·잘못된 응답은 매매 제안으로 사용하지 않습니다.

2026-09-28 공식 문서 확인 결과:

- `gemini-3.8-flash`는 실제 모델 ID이며 일반 텍스트 무료 등급을 제공합니다. **무료 등급에서는 Google Search grounding을 사용할 수 없습니다.** 현재 기업·뉴스 분석 방식 전체가 무료로 작동한다고 볼 수 없습니다.
- `gemini-2.5-flash`는 무료 등급의 검색을 지원하지만 계정의 기존 사용 이력과 실제 할당량에 따른 접근 확인이 필요합니다. 모델 목록에 보인다는 사실만으로 생성·검색 성공을 보장하지 않습니다.
- 키가 속한 Google AI Studio 프로젝트의 Free/Paid Tier를 확인해야 합니다. 앱은 결제 등급이나 금액 상한을 조회·보장하지 않습니다. 유료 등급의 검색 무료분은 텍스트 토큰 무료를 뜻하지 않습니다.
- 앱의 30회 한도는 UTC 일자 기준이며 실패한 생성 요청도 포함합니다. 장중 전략은 시작 시 잔여 6회가 필요합니다. 공급자 측 한도·초기화 시각과 별개입니다. 앱은 자동 재시도하지 않습니다.

공식 자료: [3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash), [가격표](https://ai.google.dev/gemini-api/docs/pricing), [모델 수명주기](https://ai.google.dev/gemini-api/docs/deprecations), [구조화 출력](https://ai.google.dev/gemini-api/docs/structured-output), [Google 검색](https://ai.google.dev/gemini-api/docs/google-search), [결제](https://ai.google.dev/gemini-api/docs/billing).

## 운영과 백업

```bash
cd /opt/stock-lab
docker compose ps
bash backup.sh
```

수정 전 `.env`와 코드를 별도 비공개 아카이브로 저장하고 DB를 `pg_dump -Fc`로 백업합니다. `backup.sh`는 DB만 백업하므로 코드·설정 백업을 대신하지 않습니다. DB 백업에는 가상 계좌·거래·이전 실험이 포함됩니다.

검증된 코드 변경을 적용할 때:

```bash
docker compose up -d --build --no-deps --wait --wait-timeout 180 app
```

이 명령은 앱을 다시 만들며 분석은 중지 상태가 됩니다. `docker compose down -v`는 DB 볼륨을 삭제하므로 사용하지 않습니다. `.env`, DB 덤프와 예전 `backups/`의 설정 사본은 비공개로 관리합니다. `setup.sh`는 비밀번호를 출력하므로 기존 서버 점검에는 사용하지 않습니다.

## 검증

실제 API 키와 운영 DB를 연결하지 않은 별도 환경에서 실행합니다.

```bash
python -m pip install -r requirements.txt pytest
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
```

기존 서버 테스트는 매개변수 포함 30개였습니다. 인계문의 194개 테스트 묶음은 이 서버에서 찾지 못했습니다. 추가 회귀 테스트는 Gemini 요청·응답·출처·시작 검사, 검색 표시 격리, 장중 위험 수량·직접/자동 모의체결·청산·기록 보존을 다룹니다. 합성 시세와 모의 Gemini 응답의 테스트 성공은 실제 Gemini 연결 성공이나 수익성 검증을 뜻하지 않습니다.

## 🔌 서버 재부팅 후 자동 복구

- `deploy/stocklab.service` + `deploy/stocklab-up`: 부팅 시 LAN 주소(`APP_HOST`, DHCP)가 생길 때까지 기다린 뒤 `docker compose up -d`를 실행합니다. Docker가 DHCP보다 먼저 떠서 포트 바인딩이 실패하던 문제를 막습니다.
  설치: `install -m755 deploy/stocklab-up /usr/local/bin/ && install -m644 deploy/stocklab.service /etc/systemd/system/ && systemctl enable --now stocklab.service`
- 시작 버튼을 눌러 둔 상태(`resume`)는 서버가 재시작·재부팅·비정상 종료돼도 이어집니다. 중지·전량 매도를 누르면 재시작 후에도 멈춰 있습니다. 잔고·체결 기록은 DB에 그대로 남고, 진행 중이던 분석 1건만 다시 합니다.
