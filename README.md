# 폴더 복사 (rclone G2G) — rclone_g2g_copy

원본 단발성 스크립트 `g2g.py`(파일 상단 상수를 직접 고쳐 실행하던 rclone 서버사이드
복사 스크립트)를 카테고리탭 지원 BookOasis 플러그인으로 이식했습니다.

`scan_scheduler.py`/`script.js` 실제 소스를 참고해, 커스텀 Flask Blueprint 없이
**BaseMetadataProvider 표준 계약(search/apply/get_dashboard_data)만으로** 동작합니다.

## 화면 구성

- **설정(모달, settings.html)**: `RCLONE_PATH` / `CONFIG_PATH` / `RCLONE_REMOTE` /
  `MOUNT_PREFIX`(선택) / `DISCORD_WEBHOOK_URL`(선택) — `config_schema`에 선언된
  필드와 1:1 대응.
- **카테고리탭(사이드바 전체 화면, index.html)**: 소스 폴더(URL/ID), 목적지 경로,
  [복사 시작]/[중단] 버튼, 실시간 로그.

## 새로고침 시 입력창 복원

`job_state.json`에 사용자가 실제로 타이핑했던 **원본 값**(`source_url_input`,
`dest_input` - 마운트 경로 변환 전)도 함께 저장합니다. 화면을 새로고침하면
`script.js`가 첫 폴링 응답에서 이 값을 읽어 소스/목적지 입력창을 자동으로
채워줍니다 (입력창이 비어있을 때만, 한 번만) — 그래서 새로고침해도 "무엇을
복사하던 중이었는지"가 그대로 보입니다.

## 목적지 경로 자동 변환 (마운트 경로 → rclone 기준 경로)

rclone remote가 도커/호스트에 실제로 마운트되어 있으면(예: `/mnt/zeeps_member`),
사용자는 파일탐색기/터미널에서 본 마운트 경로를 그대로 붙여넣기 쉽습니다. 그런데
rclone copy의 목적지는 `remote:상대경로` 형태라, 마운트 접두사가 그대로 들어가면
`/mnt/zeeps_member/zeepsmember/공유폴더`처럼 폴더가 중복되는 문제가 생깁니다.

그래서 목적지 경로 입력값이 설정된 `MOUNT_PREFIX`(비워두면 `/mnt/<RCLONE_REMOTE>`
자동 사용)로 시작하면, 그 접두사를 잘라내고 rclone 기준 상대 경로로 자동 변환합니다.
이미 rclone 기준 경로를 입력한 경우(접두사와 안 겹치는 경우)는 그대로 사용합니다.

- 예: `MOUNT_PREFIX = /mnt/zeeps_member`일 때
  `/mnt/zeeps_member/zeepsmember/crars님_공유_5` 입력 →
  `/zeepsmember/crars님_공유_5`로 변환되어 사용됨
- 변환 결과는 목적지 경로 입력창 **바로 아래**에 실시간으로 미리보기가 뜹니다
  (타이핑할 때마다 `script.js`가 즉시 계산 - `logic.py`의
  `to_rclone_relative_path()`와 동일한 규칙을 JS로도 복제해뒀습니다).
- 실제 변환은 서버 쪽(`_start_copy()`)에서 한 번 더 수행하므로, 프론트 미리보기
  로직이 서버 판단과 달라도 최종 동작은 항상 서버 기준입니다.
- 변환이 적용된 경우 [복사 시작] 응답 메시지에도 "...로 변환했습니다"라고 표시됩니다.

## 성능 (화면 전환/새로고침 시 로딩 속도)

두 가지를 고쳤습니다:

1. **서버**: `_read_log_lines()`가 예전엔 로그 파일 전체를 메모리에 읽은 뒤
   파이썬에서 뒤쪽만 잘라냈습니다. 복사가 오래 걸려 로그가 수천~수만 줄로
   커지면 매 폴링(1초 간격)마다 파일 전체를 읽는 게 느려지는 원인이었습니다.
   지금은 파일 끝에서부터 청크 단위로 거꾸로 읽어 필요한 최근 줄만 확보하므로,
   로그가 아무리 커져도 매번 읽는 양이 일정합니다. 반환 상한도
   `_MAX_RETURN_LINES`를 3000 → **30줄**로 낮춰 전송량 자체를 줄였습니다
   (전체 로그는 `job.log` 파일에 그대로 남아있습니다).
2. **프론트**: `appendLines()`가 예전엔 새 줄마다
   `logBox.textContent += line`을 반복해서, 줄이 많을 때 매번 전체 문자열을
   다시 복사하는 꼴(사실상 O(n²))이라 화면 전환 직후 렌더링이 느렸습니다.
   지금은 배열을 한 번에 `join`해서 한 번만 대입합니다. 서버가 최대 30줄만
   내려주므로 매 폴링마다 통째로 다시 그려도 가볍습니다. (스크롤을 위로
   올려서 지난 로그를 보고 있을 때는 자동 스크롤이 방해하지 않도록, 맨 아래
   근처에 있을 때만 자동으로 따라 내려가게 했습니다.)

## 폴링 부하 줄이기

이 프레임워크는 요청마다 플러그인 모듈을 새로 로드하는 구조라(위 항목 참고),
폴링이 잦을수록 서버 부하가 커집니다. 예전엔 진행 중일 때 무조건 1초 간격으로
계속 확인했는데, 지금은:

- **시작 직후 20초간**만 2초 간격으로 빠르게 확인하고, 그 뒤로는 **8초 간격**으로
  느리게 확인합니다 (`POLL_FAST_MS`/`POLL_SLOW_MS`/`POLL_FAST_WINDOW_MS`, `script.js`).
- **브라우저 탭이 보이지 않을 때는 폴링을 완전히 멈춥니다** (`document.hidden`
  감지). 다시 탭을 보면 즉시 한 번 확인하고 필요하면 재개합니다.
- 중단 버튼을 눌렀을 때는 처리 결과를 빨리 반영하도록 그 순간만 다시 빠른
  주기로 돌아갑니다.
- 로그는 `setInterval` 대신 `setTimeout`을 매번 다시 예약하는 방식으로 바꿔서,
  탭이 숨겨진 동안 타이머가 계속 쌓이지 않게 했습니다. 탭을 여러 번 열고 닫아도
  `visibilitychange` 리스너가 중복 등록되지 않도록 언마운트 시 제거합니다.



## 디스코드 완료 알림

설정 화면에 `DISCORD_WEBHOOK_URL`(선택)을 추가했습니다. 디스코드 채널의
연동 > 웹후크에서 발급한 URL을 입력해두면, 복사가 **끝날 때마다**(성공/실패/
사용자 중단 모두) 그 웹훅으로 알림을 보냅니다:

- ✅ 성공: `[BookOasis] 폴더 복사 완료` + 목적지 경로
- ❌ 실패: `[BookOasis] 폴더 복사 실패` + 종료 코드 + 목적지 경로
- ⏹️ 중단: `[BookOasis] 폴더 복사 중단됨` + 목적지 경로

비워두면 알림을 아예 보내지 않습니다. 전송은 표준 라이브러리(`urllib.request`)
만으로 구현했고(추가 pip 의존성 없음), 웹훅 전송이 실패해도 job 자체의 성공/
실패 처리에는 영향을 주지 않도록 예외를 삼키고 로그에만 남깁니다 (실행 로그
하단에 `[!] 디스코드 알림 전송 실패: ...`로 표시됨).

## RCLONE_REMOTE 풀다운 (rclone.conf 자동 인식)

설정 화면에서 `RCLONE_REMOTE`를 더 이상 직접 타이핑하지 않아도 됩니다.
`CONFIG_PATH`(rclone.conf 경로)를 입력해두면, `settings.js`가 그 파일을 서버에서
파싱해서 등록된 remote 이름들을 풀다운(select)으로 보여줍니다.

- rclone.conf는 INI 형식이라 각 remote가 `[remote_name]` 섹션으로 구분되는데,
  `logic.py`의 `list_rclone_remotes()`가 `configparser`로 이 섹션 이름들만
  뽑아냅니다 (토큰 값에 `%`가 섞여 있어도 안전하도록 `interpolation=None`으로
  읽음 — 실제 유사 형식으로 테스트 완료).
- CONFIG_PATH 입력창에서 포커스를 벗어나면(저장 전이라도) 자동으로 새로고침되고,
  RCLONE_REMOTE 라벨 옆 **↻ 새로고침** 버튼으로 언제든 다시 불러올 수 있습니다.
- rclone.conf를 못 찾거나 remote가 하나도 없으면 자동으로 직접 입력 텍스트
  필드로 돌아갑니다 — 항상 어떤 값이든 저장할 수 있게 폴백을 보장합니다.
- 이미 저장돼 있던 값이 새로 불러온 목록에 없으면(예: rclone.conf에서 이미
  지운 remote), 그 값을 몰래 다른 걸로 바꿔버리지 않도록 목록에 그대로
  끼워 넣어서 보여줍니다.
- 구현은 select/text 두 입력 중 **화면에 보이는 쪽에만** `name="RCLONE_REMOTE"`를
  붙이는 방식입니다 (jsdom으로 실제 DOM 시뮬레이션해서 저장 시 숨겨진 입력의
  값이 실수로 덮어쓰지 않는지 확인함). 텍스트 입력 쪽은 애초에
  `name="RCLONE_REMOTE"`를 갖고 있어서, `settings.js`가 어떤 이유로든 실행에
  실패해도 예전처럼 수동 입력은 항상 동작합니다.
- 목록 조회는 새 Flask 라우트를 만들지 않고, 기존에 확인된 `apply()` 액션
  채널(`item_data.action = "list_remotes"`)을 재사용했습니다. `apply()`가
  `(bool, message)` 문자열만 돌려줄 수 있는 제약 때문에, remote 목록은
  `message`에 JSON으로 실어 보내고 `settings.js`에서 `JSON.parse`합니다.

## 속도가 느릴 때 (동시성 옵션)

서버사이드 복사인데도 느리다면(로그의 `MiB/s` 표시가 낮음), 대부분 대역폭이
아니라 **동시 처리 개수 부족**이 원인입니다. 구글 드라이브 서버사이드 복사는
파일마다 독립적인 API 호출이라, 동시에 더 많이 보낼수록 API 왕복 지연을 훨씬
잘 가려서 체감 속도가 크게 빨라집니다. 예전엔 이 값들을 아예 지정하지 않아서
rclone 기본값(`--transfers=4`, `--checkers=8`)으로 돌아가고 있었습니다.

설정 화면에 세 필드를 추가했습니다:

- **RCLONE_TRANSFERS** (기본 8) — `--transfers`
- **RCLONE_CHECKERS** (기본 16) — `--checkers`
- **RCLONE_FAST_LIST** (기본 켜짐) — `--fast-list`, 목록 조회 API 호출 수 자체를
  줄여줌 (파일/폴더가 많을 때 특히 효과적)

너무 높이면 구글 API 레이트리밋(오류 403)에 걸려 재시도가 늘어나 오히려
느려질 수 있으니, 16~32 정도까지만 천천히 올려보는 걸 권장합니다. 값을
비워두거나 숫자가 아닌 값을 넣으면 기본값(8/16)으로 자동 대체됩니다.

## "Failed to save config ... device or resource busy" 에러

이건 저희 플러그인 코드 문제가 아니라 **rclone + Docker의 잘 알려진 이슈**입니다
([rclone/rclone#6656](https://github.com/rclone/rclone/issues/6656)).

**원인**: 구글 OAuth 토큰이 만료되면 rclone이 자동으로 갱신하고, 그 갱신된
토큰을 `rclone.conf`에 다시 저장하려고 시도합니다. 이때 rclone은 원자적 저장을
위해 `rclone.conf` → `rclone.conf.old`로 **rename**하는 방식을 쓰는데,
`rclone.conf` 파일을 도커에 **파일 하나만** 바인드 마운트해두면 그 파일 자체가
마운트 지점이라 rename이 불가능해서 "device or resource busy"가 납니다.

**해결책 (도커 볼륨 설정)**: `rclone.conf` 파일 하나만 마운트하지 말고, 그
파일이 들어있는 **디렉터리 전체**를 마운트하세요.
```yaml
# 문제 있는 방식 (파일 하나만 마운트)
- /root/docker/ff/db/rclone.conf:/app/config/rclone.conf

# 해결 방식 (디렉터리 전체 마운트)
- /root/docker/ff/db:/app/config
```

이건 BookOasis 컨테이너 자체의 볼륨 마운트 설정 문제라 플러그인 코드로는 근본
해결이 안 되지만, `logic.py`의 `_maybe_explain_config_save_error()`가 실행
로그에서 이 에러 패턴을 감지하면 원인/해결책 설명을 바로 뒤에 자동으로
덧붙여줍니다 (같은 job 안에서 반복 출력되지 않도록 한 번만 표시 — 실제 반복
발생 시나리오로 테스트 완료). 대개 이 오류가 나도 그 순간의 복사 자체는 계속
진행되지만, 갱신된 토큰이 저장되지 않아 매번 다시 나타날 수 있습니다.

## 진행률 표시

`rclone --progress` 출력에는 이미 퍼센트/속도/ETA가 찍히고 있었지만 로그 텍스트
안에 파묻혀 있었습니다. 이제 그 줄을 파싱해서 [복사 시작] 버튼 아래에
**진행률 바**로 보여줍니다.

- rclone은 `--progress` 상태 블록에 "Transferred:" 줄을 두 개 찍습니다 —
  하나는 바이트 기준(용량/속도/ETA 포함), 하나는 파일 개수 기준. 둘 다 정규식으로
  파싱해서(`logic.py`의 `_parse_progress_line()`) 하나의 `progress` 상태로
  합칩니다: `{percent, transferred, total, speed, eta, files_done, files_total}`
  (실제 rclone 출력과 유사한 샘플로 파싱/병합 로직을 테스트 완료).
- 바이트 기준 줄이 갱신될 때마다(기본 1초 간격) `job_state.json`에 함께
  저장하고, 카테고리탭 폴링이 이 값을 읽어 진행률 바 너비와 상세 텍스트
  (`42% · 760.0 MiB / 1.818 GiB · 5.2 MiB/s · ETA 3m10s · 120 / 8053 파일`)를
  갱신합니다.
- 복사가 성공적으로 끝나면 rclone의 마지막 갱신이 100%를 안 찍고 끝나는
  경우를 대비해 서버에서 강제로 100%로 채웁니다.
- 아직 첫 통계가 안 나온 시점(막 시작 직후)엔 "진행률 계산 중..."을 보여주고,
  job이 아예 없으면 진행률 바 자체를 숨깁니다 — jsdom으로 세 가지 상태
  (진행 중/통계 대기/완료) 모두 실제 DOM 렌더링까지 확인했습니다.

**전체 진행률 하나만 크게 표시**: 처음엔 세부 정보(퍼센트/속도/ETA/파일개수)를
한 줄에 다 넣었는데, "전체 복사 진행율"이 한눈에 안 들어온다는 피드백을 받아
바꿨습니다. 이제 진행률 바 위에 **큰 숫자로 전체 퍼센트 하나만** 보여주고,
나머지(속도/ETA/전송량/파일개수)는 그 아래 작은 글씨로 보조 정보로 뺐습니다.

바이트 기준 퍼센트(`percent`, 전체 데이터량 대비)와 파일개수 기준 퍼센트
(`files_percent`, 전체 파일 개수 대비)는 파일 크기가 제각각이면 서로 다르게
움직입니다. 서버사이드 복사에서는 파일개수 줄이 바이트 줄보다 먼저/자주
갱신되는 경우가 있어서, 바이트 줄만 기다리면 막 시작했을 때 진행률 바가
전혀 안 움직이는 것처럼 보일 수 있었습니다. 그래서:

- `logic.py`는 **둘 중 어느 줄이 갱신되든 즉시** 상태 파일에 반영합니다
  (예전엔 바이트 줄이 나올 때만 반영했음).
- `script.js`는 "전체 진행률"로 바이트 기준 퍼센트를 우선 쓰고, 아직 그 값이
  없으면(막 시작 직후) 파일개수 기준 퍼센트로 대체해서 보여줍니다 — 파일개수
  줄만 먼저 온 상황에서도 진행률 바가 먼저 움직이는 걸 실제 DOM 렌더링으로
  확인했습니다.

## job 상태를 왜 파일에 저장하는가 (중요)

처음 버전은 job 상태/로그를 파이썬 모듈 전역 dict(메모리)에만 들고 있었는데,
**화면을 새로고침하면 방금까지 보이던 진행 상황이 사라지는 버그**가 있었습니다.

원인: 이 프레임워크는 요청마다 플러그인 모듈/인스턴스를 새로 만드는 것으로
보입니다 (ridi_book 작업 때 `self._last_search` 인스턴스 캐시가 요청 간에
유지되지 않아 파일 캐시로 바꿨던 것과 동일한 원인). 새로고침 요청은 완전히
새로운 모듈 전역(빈 dict)을 보게 되는 반면, 이미 시작된 백그라운드 스레드는
자신이 처음 캡처했던 "예전" 모듈 전역에 계속 값을 쓰고 있어서, 실제 rclone
프로세스는 잘 돌고 있는데도 새로고침한 화면에는 안 보이는 상황이 발생했습니다.

**해결**: job 상태(`job_state.json`)와 로그(`job.log`)를 메모리가 아니라
**파일**에 저장하도록 바꿨습니다. 파일 경로는 어떤 모듈 인스턴스에서 봐도
항상 같으므로, 새로고침이 어느 인스턴스로 요청을 보내든 항상 같은 최신
상태를 읽습니다. 저장 위치는 `google_links` 플러그인에서 확인된 관례를 따라
`plugins/metadata/rclone_g2g_copy/`(코드, 업데이트 시 통째로 교체됨) 바깥의
`./plugins/data/rclone_g2g_copy/`(데이터, 업데이트해도 보존됨 · 앱 작업 디렉터리 기준 상대 경로)를 사용합니다.

## 중단(취소) 버튼

같은 이유로, 파이썬 객체(Popen 인스턴스) 참조가 아니라 **OS가 보장하는 값인
PID**를 상태 파일에 저장해두고, 중단 요청이 오면 `os.kill(pid, SIGTERM)`으로
직접 종료합니다 (5초 뒤에도 살아있으면 `SIGKILL`). 요청을 처리하는 모듈
인스턴스가 job을 시작했던 인스턴스와 달라도 항상 동작합니다.

- `[중단]` → `POST /api/media/books/0/apply-metadata`,
  `item_data: {"action": "cancel_copy"}` → `job_state.json`에
  `cancel_requested: true` 기록 + PID로 SIGTERM
- 이미 복사된 파일은 그대로 남고, 중단 시점까지의 로그도 그대로 보존됩니다.
- 서버(컨테이너)가 재시작돼서 프로세스 자체가 완전히 사라진 경우엔
  `get_last_job_status()`가 PID 생존 여부를 확인해 "추적 불가" 상태로
  자동 정리합니다 (좀비 "진행 중" 상태로 영원히 남는 것 방지).

## 동작 흐름 정리

1. `[복사 시작]` → `apply(action="start_copy")` → 기존에 실행 중(그리고 실제
   살아있는) job이 있으면 거부, 아니면 `job_state.json`/`job.log` 초기화 후
   백그라운드 스레드에서 `rclone copy ... --progress` 실행, PID를 상태 파일에 기록.
2. index.html이 1초 간격으로 `GET /api/media/dashboard/widgets/rclone_g2g_copy/data`
   폴링 → `get_dashboard_data()`가 상태 파일 + 로그 파일을 읽어 그대로 반환
   (`data.success`, `data.config`, `data.job`— `{data:...}`로 안 감싸짐, scan_scheduler와 동일).
3. `[중단]` → `apply(action="cancel_copy")` → PID에 SIGTERM.
4. 로그 파일은 `--progress`의 캐리지리턴(`\r`) 갱신 라인을 서버 쪽에서 조각내
   별도 라인으로 저장하고, 반환 시 최근 3000줄까지만 돌려줍니다(그 이상은
   앞부분 생략 표시).

## 파일 구조

```
rclone_g2g_copy/
  __init__.py          # provider 노출
  rclone_g2g_copy.py    # BaseMetadataProvider 계약 (search/apply/get_dashboard_data) + category_tab
  logic.py               # rclone 실행/파일 기반 job 상태 관리/중단(PID kill)/rclone.conf 파싱/디스코드 알림
  settings.html            # 설정 모달 - RCLONE_PATH/CONFIG_PATH/RCLONE_REMOTE(풀다운)/MOUNT_PREFIX/DISCORD_WEBHOOK_URL
  settings.css              # 설정 모달 스타일
  settings.js                # RCLONE_REMOTE 풀다운 채우기 (rclone.conf 자동 조회)
  index.html                  # 카테고리탭 전체 화면 - 실행 폼 + 로그 + 중단 버튼
  style.css                    # 카테고리탭 화면 스타일
  script.js                     # 카테고리탭 화면 동작
  requirements.txt               # 빈 파일 (외부 pip 의존성 없음 - unified_book 규칙대로 패키지명만 적는 파일)
  VERSION
  README.md
```

실행 중 생성되는 데이터 (코드와 별도 경로, 업데이트해도 보존):
```
./plugins/data/rclone_g2g_copy/
  job_state.json   # {job_id, status, pid, cancel_requested, returncode, started_at, finished_at, source_id, dest_path}
  job.log           # rclone --progress 출력 (한 줄씩)
```

## !! 확인 필요한 부분 (실제 서버에서 검증 필요) !!

1. `get_db_gateway(db_type).get_plugin_config(self.id)` 메서드명/반환 형태
   — ridi_book 작업 때 확인됐다는 기록만 있고, 이번 세션엔 실제 소스가 없어
   그대로 가정했습니다.
2. `category_tab.icon` 아이콘 클래스 값 — scan_scheduler의 `fa-solid fa-table-cells`를
   참고해 Font Awesome 클래스로 가정했습니다 (`fa-solid fa-clone`)
3. `update_manifest.raw_base_url`은 아직 만들지 않은 저장소(`yume-script/rclone_g2g_copy`)
   가정입니다.
4. `./plugins/data/<plugin_id>/`는 앱의 현재 작업 디렉터리(cwd) 기준 상대 경로입니다.
   앱이 어디서 실행되든 항상 리포지토리/컨테이너 루트가 cwd라는 전제인데, 실제로
   다른 위치에서 기동된다면 엉뚱한 곳에 파일이 생길 수 있습니다. 그 경로에 쓰기
   권한이 있는지도 함께 확인 부탁드립니다 (안 되면 로그가 전혀 안 쌓일 수 있음).
