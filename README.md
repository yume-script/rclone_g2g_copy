# 폴더 복사 (rclone G2G) — rclone_g2g_copy

원본 단발성 스크립트 `g2g.py`(파일 상단 상수를 직접 고쳐 실행하던 rclone 서버사이드
복사 스크립트)를 카테고리탭 지원 BookOasis 플러그인으로 이식했습니다.

`scan_scheduler.py`/`script.js` 실제 소스를 참고해, 커스텀 Flask Blueprint 없이
**BaseMetadataProvider 표준 계약(search/apply/get_dashboard_data)만으로** 동작합니다.

## 화면 구성

- **설정(모달, settings.html)**: `RCLONE_PATH` / `CONFIG_PATH` / `RCLONE_REMOTE`
  — `config_schema`에 선언된 필드와 1:1 대응.
- **카테고리탭(사이드바 전체 화면, index.html)**: 소스 폴더(URL/ID), 목적지 경로,
  [복사 시작]/[중단] 버튼, 실시간 로그.

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
  logic.py               # rclone 실행/파일 기반 job 상태 관리/중단(PID kill) (원본 g2g.py 로직 이식)
  settings.html           # 설정 모달 - RCLONE_PATH/CONFIG_PATH/RCLONE_REMOTE
  index.html               # 카테고리탭 전체 화면 - 실행 폼 + 로그 + 중단 버튼
  style.css                 # 카테고리탭 화면 스타일
  script.js                  # 카테고리탭 화면 동작
  requirements.txt            # 빈 파일 (외부 pip 의존성 없음 - unified_book 규칙대로 패키지명만 적는 파일)
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
