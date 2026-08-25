# -*- coding: utf-8 -*-
"""
rclone_g2g_copy (폴더 복사 - rclone G2G)
--------------
원본 스크립트 g2g.py(파일 상단 상수를 직접 고쳐서 실행하던 rclone 서버사이드
복사 스크립트)를 카테고리탭 지원 BookOasis 플러그인으로 이식한 것입니다.

- scan_scheduler.py에서 실제로 확인된 계약을 그대로 따릅니다: 커스텀 Flask
  Blueprint/라우트를 따로 두지 않고, BaseMetadataProvider 표준 계약
  (search/apply/get_dashboard_data)만으로 동작합니다.
- 설정(RCLONE_PATH/CONFIG_PATH/RCLONE_REMOTE)은 config_schema + settings.html로
  선언하고, self.get_db_gateway(db_type).get_plugin_config(self.id)로 읽습니다.
  (ridi_book/dict_lookup 작업에서 확인된 설정 조회 패턴)
- 실행(복사 시작)은 좌측 사이드바 category_tab 풀페이지(index.html/script.js)에서
  POST /api/media/books/0/apply-metadata (book_id=0 더미, plugin_board에서 확인된
  범용 액션 채널)를 호출해 apply(db_type, book_id, item_data)로 들어옵니다.
  item_data = {"action": "start_copy", "source_url": ..., "dest_folder_name": ...}
- 진행 상황(rclone --progress 로그)은 GET /api/media/dashboard/widgets/
  rclone_g2g_copy/data 로 들어오는 get_dashboard_data(db_type, limit)를
  프론트가 주기적으로 폴링해서 가져갑니다. (풀페이지 뷰이므로 db_type/limit은
  사실상 무시하고, 가장 최근 시작한 job 하나의 상태/로그를 그대로 반환)

!! 확인 필요 (scan_scheduler.py에는 없던 부분이라 추정입니다) !!
  - `get_plugin_config` 메서드명/반환 형태: ridi_book 작업 때
    "get_db_gateway(db_type).get_plugin_config(self.id)"로 확인되었다는 메모가
    있어 그대로 따랐지만, 이번 세션에서 그 실제 소스는 보지 못했습니다.
  - apply-metadata 응답 JSON 형태(성공/실패 필드명)는 정확히 확인되지 않아
    script.js에서 `success`/`message`로 가정했습니다 - 실제 응답이 다르면
    script.js의 파싱 부분만 고치면 됩니다.
"""

from plugins.metadata.base import BaseMetadataProvider

from .logic import ConfigError, start_copy_job, get_last_job_status


class RcloneG2gCopyProvider(BaseMetadataProvider):
    id = "rclone_g2g_copy"
    name = "폴더 복사 (rclone G2G)"
    is_searchable = False

    # 설정 화면(settings.html)과 1:1로 대응되는 필드 목록.
    # (random_gallery/pixiv_ranking 작업에서 확인된 config_schema 형식)
    config_schema = [
        {
            "key": "RCLONE_PATH",
            "label": "RCLONE_PATH",
            "type": "text",
            "default": "/usr/bin/rclone",
        },
        {
            "key": "CONFIG_PATH",
            "label": "CONFIG_PATH (rclone.conf 절대경로)",
            "type": "text",
            "default": "",
        },
        {
            "key": "RCLONE_REMOTE",
            "label": "RCLONE_REMOTE (rclone.conf에 등록된 remote 이름)",
            "type": "text",
            "default": "",
        },
    ]

    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        # TODO: 실제 GitHub 저장소 생성 후 확인 필요 (yume-script/rclone_g2g_copy 가정)
        "raw_base_url": "https://raw.githubusercontent.com/yume-script/rclone_g2g_copy/refs/heads/main/",
        "files": [
            "rclone_g2g_copy.py",
            "logic.py",
            "__init__.py",
            "VERSION",
            "index.html",
            "style.css",
            "script.js",
            "settings.html",
            "requirements.txt",
        ],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }

    # 좌측 사이드바 독립 카테고리 메뉴 등록 (scan_scheduler와 동일한 계약)
    category_tab = {
        "title": "폴더 복사 (rclone G2G)",
        "icon": "fa-solid fa-clone",  # TODO: 실제 아이콘 셋 확인 필요
        "order": 96,
        "sessions": "all",
    }

    # ------------------------------------------------------------------
    # 설정 조회 헬퍼
    # ------------------------------------------------------------------
    def _get_config(self, db_type):
        try:
            gw = self.get_db_gateway(db_type)
            cfg = gw.get_plugin_config(self.id) or {}
        except Exception:
            cfg = {}
        defaults = {item["key"]: item.get("default", "") for item in self.config_schema}
        defaults.update({k: v for k, v in cfg.items() if v})
        return defaults

    # ------------------------------------------------------------------
    # 필수 계약: 이 플러그인은 도서 메타데이터 검색과 무관한 유틸리티
    # ------------------------------------------------------------------
    def search(self, db_type, query):
        return []

    def apply(self, db_type, book_id, item_data):
        """book_id=0으로 호출되는 범용 액션 채널 (plugin_board/scan_scheduler와 동일 패턴).
        item_data = {
            "action": "start_copy",
            "source_url": "https://drive.google.com/drive/folders/xxxx 또는 폴더ID",
            "dest_folder_name": "/remote/기준/목적지/경로",
        }
        """
        try:
            return self._dispatch_apply(db_type, item_data)
        except Exception as exc:  # noqa: BLE001
            return False, "예상치 못한 오류가 발생했습니다: %s" % exc

    def _dispatch_apply(self, db_type, item_data):
        if not isinstance(item_data, dict):
            return False, "유효하지 않은 요청 데이터 형식입니다."

        action = str(item_data.get("action", "")).strip()
        if action != "start_copy":
            return False, "지원하지 않는 action입니다: %s" % action

        source_url = str(item_data.get("source_url", "")).strip()
        dest_folder_name = str(item_data.get("dest_folder_name", "")).strip()

        if not source_url:
            return False, "소스 폴더 URL(또는 ID)을 입력해주세요."
        if not dest_folder_name:
            return False, "목적지 경로를 입력해주세요."

        config = self._get_config(db_type)

        try:
            start_copy_job(
                rclone_path=config.get("RCLONE_PATH"),
                config_path=config.get("CONFIG_PATH"),
                rclone_remote=config.get("RCLONE_REMOTE"),
                source_folder_url=source_url,
                dest_folder_name=dest_folder_name,
            )
        except ConfigError as e:
            return False, str(e)
        except (ValueError, RuntimeError) as e:
            return False, str(e)

        return True, "복사를 시작했습니다. 진행 상황은 화면 하단 로그에서 확인하세요."

    # ------------------------------------------------------------------
    # 풀페이지 뷰(index.html/script.js)가 주기적으로 폴링하는 데이터 소스
    # GET /api/media/dashboard/widgets/rclone_g2g_copy/data?type=<db_type>
    # ------------------------------------------------------------------
    def get_dashboard_data(self, db_type, limit=10):
        config = self._get_config(db_type)
        configured = bool(config.get("RCLONE_PATH") and config.get("CONFIG_PATH") and config.get("RCLONE_REMOTE"))

        job = get_last_job_status()

        return {
            "success": True,
            "config": {
                "configured": configured,
                "rclone_path": config.get("RCLONE_PATH"),
                "rclone_remote": config.get("RCLONE_REMOTE"),
            },
            "job": job,  # None이면 아직 시작한 job이 없다는 뜻
        }
