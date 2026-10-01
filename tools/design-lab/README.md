# 🧪 design-lab · 화면 검수 도구

화면을 고칠 때 **실제 브라우저에서** 확인하기 위한 개발용 도구입니다. 운영 서버·DB·토스·AI와 완전히 분리된 **데모 서버(합성 데이터)** 를 띄우고, 헤드리스 브라우저(Playwright)로 스크린샷·동작·글자 대비를 점검합니다. 앱 자체에는 필요 없고 이미지에도 들어가지 않습니다.

| 파일 | 역할 |
|---|---|
| `seed.py` | 앱의 실제 엔진으로 분석 사이클을 돌려 체결·보고서·판단 채점(30/60분과 1·5·21거래일)·규칙 신호 줄·추적 손절 보유 종목·그림자 주문이 있는 **1개월 스윙** 데모 장부를 만듭니다 |
| `demo_app.py` | 그 장부 위에서 실제 앱을 실행하고 시장 순위·수급 데이터를 채웁니다 |
| `shot.py` | 스크린샷. `/api/state` 응답을 가로채 **실행 중 · 승인 대기 · 손실 정지 · 빈 상태** 등을 재현합니다 |
| `interact.py` | 테마 유지, 탭 이동, 30/60분 토글, 대화상자, **2초 갱신 뒤에도 요소가 유지되는지**, 키보드 포커스, 움직임 줄이기 (보안 정책을 켠 채) |
| `focus_check.py` | **오늘의 집중 종목 카드**: 종목·점수·출처·제외 이유·보유 종목 교체 표시, 실험 설정의 종목 구성 방식, 내비게이션, 폰 폭 가로 넘침 (스크린샷 없이 DOM으로 확인) |
| `month_check.py` | **1개월 스윙 화면**: 전략 이름·규칙 신호 줄·추적 손절 표시·채점 탭(30분~21거래일)·새 실험 폼(일↔분 전환, 범위 검사, 전송 값)·폰 폭 가로 넘침 (스크린샷 없이 DOM으로 확인) |
| `seed_day.py` · `day_check.py` | **(제거된) 당일 단타 실험**: 변동폭이 다양한 합성 일봉으로 단타용 집중 종목 목록을 만들고, 화면에서 변동폭·제외 이유(움직임 작음·너무 거침·급락)·문구·새 실험 폼 안내를 DOM으로 확인합니다. 데모 서버를 띄울 때 `seed.py` 대신 `seed_day.py`를 실행하세요 |
| `seed_verify.py` · `verify_check.py` | **검증 계획 패널**: 1개월 스윙 시드에 검증 3주 차·규칙대로 비교·지수 비교를 더해, 검증 칸(진행률·판정 항목·거래 성적표·지수 비교·AI vs 규칙대로), 1개월 스윙만 남은 새 실험 양식, 실제 요율 비용 안내, 폰 폭·다크 테마를 DOM으로 확인합니다 (`sh -c "python /lab/seed_verify.py && exec uvicorn demo_app:application --host 0.0.0.0 --port 8080"`) |
| `seed_watch.py` · `demo_app_watch.py` · `watch_check.py` | **조건부 자동 진입 카드**: `seed_day.py` 장부에 대기 중 계획 2개·종료된 계획 3개·조건 진입 체결·디렉터 계획 보고서를 더해 화면에서 카드 구성(종류·기준 가격·현재 호가·조건까지 부호 있는 %·무효 가격·남은 시간·연속 확인)·종료 목록·체결 표시·폰 폭·다크 테마를 DOM으로 확인합니다. 앱이 시작하면 대기 계획을 취소하므로 `demo_app_watch:application`으로 띄우세요 (`sh -c "python /lab/seed_watch.py && exec uvicorn demo_app_watch:application --host 0.0.0.0 --port 8080"`) |
| `contrast.py` | 글자를 투명하게 만든 배경을 찍어 글자마다 **실제 대비**를 측정하고 WCAG AA 미달만 보고합니다 |
| `img/Dockerfile` | 브라우저 이미지 (Playwright + 한글 폰트 + Inter, 약 3.7GB) |

## 사용법 (저장소 루트에서)

```bash
# 0) 앱 이미지 (한 번) — 이미 있으면 생략
docker build -t stock-lab-app .

# 1) 데모 서버: http://127.0.0.1:8081  (비밀번호 demo-password-12345)
docker run -d --name stocklab-demo -p 127.0.0.1:8081:8080 \
  -e PYTHONPATH=/app:/lab -e APP_HOST=127.0.0.1 \
  -v "$PWD/app":/app/app:ro -v "$PWD/tools/design-lab":/lab:ro \
  --tmpfs /tmp:exec,mode=1777 stock-lab-app \
  sh -c "python /lab/seed.py && exec uvicorn demo_app:application --host 0.0.0.0 --port 8080"

# 2) 브라우저 이미지 (한 번)
docker build -t stocklab-shots tools/design-lab/img

# 3) 스크린샷 → ./shots
docker run --rm --network host --ipc=host \
  -v "$PWD/tools/design-lab":/lab:ro -v "$PWD/shots":/out \
  stocklab-shots python /lab/shot.py /lab/shots.example.json

# 4) 동작 점검 / 글자 대비 측정
docker run --rm --network host --ipc=host -v "$PWD/tools/design-lab":/lab:ro -v "$PWD/shots":/out \
  stocklab-shots python /lab/interact.py
docker run --rm --network host --ipc=host -v "$PWD/tools/design-lab":/lab:ro -w /lab \
  stocklab-shots python /lab/contrast.py

# 정리
docker rm -f stocklab-demo
```

`app/` 폴더를 읽기 전용으로 연결하므로 CSS·JS를 고치면 **다시 빌드하지 않고** 바로 반영됩니다.

## 스크린샷 사양 (`shots.example.json`)

| 키 | 의미 |
|---|---|
| `name` | 출력 파일 이름 |
| `width` · `height` · `scale` | 화면 크기, 배율 (모바일은 `390×844`, `scale: 2`) |
| `theme` | `light` / `dark` |
| `states` | 적용할 상태 변환: `base`(운영처럼 보이게) · `running` · `pending` · `halted` · `empty` · `flat` · `raw`(변환 없음) |
| `scroll` | 이 선택자의 구역이 맨 위에 오도록 스크롤 후 화면 캡처 |
| `selector` | 이 요소만 캡처 (상단 바는 고정 해제) |
| `full` · `clip` | 전체 페이지 / 영역 잘라내기 |
| `jpeg` | JPEG로 저장 (작은 파일) |
| `csp` | `true`이면 보안 정책을 그대로 적용해 위반을 콘솔 오류로 확인 |

## 다른 서버를 대상으로 (예: 운영 화면 확인)

`shot.py`는 `LAB_BASE`와 `LAB_PASSWORD` 환경변수로 대상을 바꿉니다. 비밀번호는 명령어에 직접 쓰지 말고 환경변수로 넘기고, 상태 변환은 `raw`만 쓰세요.

```bash
export LAB_PASSWORD="…"   # 셸에서 설정 (기록에 남기지 않기)
docker run --rm --network host -e LAB_PASSWORD -e LAB_BASE=http://192.168.1.117:8080 \
  -v "$PWD/tools/design-lab":/lab:ro -v "$PWD/shots":/out stocklab-shots python /lab/shot.py /lab/prod.json
```

> ⚠️ 토스 Open API는 **클라이언트당 유효한 토큰이 1개**이고 재발급하면 이전 토큰이 즉시 무효화됩니다. 운영 서버를 점검할 때는 앱 밖에서 토스 API를 직접 호출하지 말고, 이 도구처럼 앱의 화면·상태 API만 읽으세요.
