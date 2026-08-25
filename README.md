# 폴더 복사 (rclone G2G) — rclone_g2g_copy

원본 단발성 스크립트 `g2g.py`(파일 상단 상수를 직접 고쳐 실행하던 rclone 서버사이드
복사 스크립트)를 카테고리탭 지원 BookOasis 플러그인으로 이식했습니다.

`scan_scheduler.py`/`script.js` 실제 소스를 참고해, 커스텀 Flask Blueprint 없이
**BaseMetadataProvider 표준 계약(search/apply/get_dashboard_data)만으로** 동작합니다.

## 화면 구성 (요청하신 대로 분리)

- **설정(모달, settings.html)**: `RCLONE_PATH` / `CONFIG_PATH` / `RCLONE_REMOTE`
  — `config_schema`에 선언된 필드와 1:1 대응.
- **카테고리탭(사이드바 전체 화면, index.html)**: 소스 폴더(URL/ID), 목적지 경로,
  [복사 시작] 버튼, 실시간 로그 — 실행할 때마다 바뀌는 값이라 여기서 매번 입력합니다.

## 동작 방식 (scan_scheduler로 실측 확인된 계약)

- **설정 조회**: `self.get_db_gateway(db_type).get_plugin_config(self.id)`
  (ridi_book 작업에서 확인된 패턴 — 이 부분만 아직 실측 전, 아래 "확인 필요" 참고)
- **복사 시작**: index.html [복사 시작] 버튼 →
  `POST /api/media/books/0/apply-metadata`
  body: `{ type: "general", source: "rclone_g2g_copy", item_data: { action: "start_copy", source_url, dest_folder_name } }`
  → `apply(db_type, book_id=0, item_data)` → 백그라운드 스레드로
  `rclone copy <remote>,root_folder_id=<소스ID>: <remote>:<목적지경로> --config <conf> --progress` 실행.
  응답은 `data.success` (bool) + 실패 시 `data.error`/`data.message`.
- **진행 상황 조회**: index.html이 1초 간격으로
  `GET /api/media/dashboard/widgets/rclone_g2g_copy/data?type=general&limit=1` 폴링
  (scan_scheduler와 동일하게 `type`은 고정값 - 이 플러그인은 스코프 구분이 없음) →
  응답이 `{data: {...}}`로 감싸지지 않고 `get_dashboard_data()`의 반환값이 최상위
  JSON으로 바로 옴 (`data.success`, `data.config`, `data.job`).
  `--progress`의 캐리지리턴(`\r`) 갱신 라인은 서버 쪽에서 조각내 별도 라인으로 남깁니다.
- 작업 상태/로그는 프로세스 메모리(logic.py의 JOBS dict)에만 보관됩니다 — 재시작하면
  사라짐, 동시에 1건만 실행 가능(이미 실행 중이면 새 요청은 거부), 다중 워커(gunicorn)
  환경이면 워커별로 분리되니 1워커 전제입니다.

## 파일 구조

```
rclone_g2g_copy/
  __init__.py          # provider 노출
  rclone_g2g_copy.py    # BaseMetadataProvider 계약 (search/apply/get_dashboard_data) + category_tab
  logic.py               # rclone 실행/스트리밍/작업(job) 상태 관리 (원본 g2g.py 로직 이식)
  settings.html           # 설정 모달 - RCLONE_PATH/CONFIG_PATH/RCLONE_REMOTE
  index.html               # 카테고리탭 전체 화면 - 실행 폼 + 로그
  style.css                 # 카테고리탭 화면 스타일
  script.js                  # 카테고리탭 화면 동작 (scan_scheduler 규약 그대로 재사용)
  VERSION
  README.md
```

## !! 확인 필요한 부분 (실제 서버에서 검증 필요) !!

1. `get_db_gateway(db_type).get_plugin_config(self.id)` 메서드명/반환 형태
   — ridi_book 작업 때 확인됐다는 기록만 있고, 이번 세션엔 실제 소스가 없어
   그대로 가정했습니다. 이미 설정값을 이런 식으로 읽는 플러그인(예: dict_lookup.py)의
   실제 코드를 보여주시면 확실히 맞추겠습니다.
2. `category_tab.icon` 아이콘 클래스 값 — scan_scheduler의 `fa-solid fa-table-cells`를
   참고해 Font Awesome 클래스로 가정했습니다 (`fa-solid fa-clone`)
3. `update_manifest.raw_base_url`은 아직 만들지 않은 저장소(`yume-script/rclone_g2g_copy`)
   가정입니다 — GitHub 저장소를 만드신 뒤 실제 경로로 맞춰주세요.

엔드포인트/응답 형태(apply-metadata, dashboard data)는 이번에 실제 `scan_scheduler.py`/
`script.js`로 확인해서 반영했습니다.
