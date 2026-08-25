# -*- coding: utf-8 -*-
"""
rclone_g2g_copy / logic.py

원본 스크립트(g2g.py)의 rclone_server_side_copy() 로직을
"한 번 실행하고 콘솔에 print"에서 "백그라운드 스레드로 실행하고
job 상태(dict)에 라인을 누적 -> 프론트가 폴링"으로 이식한 것입니다.

핵심만 원본과 동일:
  - source_folder_url(구글 드라이브 폴더 URL 또는 ID)에서 폴더 ID를 정규식으로 추출
  - rclone_remote,root_folder_id=<source_id>: 를 source 로,
    rclone_remote:<dest_folder_name> 을 dest 로 삼아
  - `rclone copy <source> <dest> --config <config_path> --progress` 를 실행

바뀐 점:
  - RCLONE_PATH / CONFIG_PATH / RCLONE_REMOTE 는 더 이상 파일 상단 상수가 아니라
    플러그인 설정(설정 화면에서 저장된 값)에서 매번 읽어옵니다.
  - SOURCE_URL / DEST_FOLDER_NAME 은 더 이상 상수가 아니라, 카테고리탭 화면에서
    사용자가 매번 입력하는 값입니다.
  - subprocess.Popen 스트리밍 출력을 print() 하는 대신 JOBS[job_id]["lines"]에 누적합니다.
"""

import os
import re
import subprocess
import threading
import time
import uuid

# job_id -> {
#   "status": "running" | "success" | "error",
#   "lines": [str, ...],
#   "returncode": int | None,
#   "started_at": float,
#   "finished_at": float | None,
#   "source_id": str,
#   "dest_path": str,
# }
JOBS = {}
_JOBS_LOCK = threading.Lock()

# 메모리 누적 방지를 위해 job을 보관하는 최대 개수 (오래된 것부터 정리)
_MAX_KEEP_JOBS = 20

# get_dashboard_data(db_type, limit)는 job_id를 인자로 받지 않으므로(플러그인
# 표준 계약 시그니처 고정), "가장 최근 시작한 job" 하나를 전역으로 추적해서
# 풀페이지 폴링이 이 값만 보고 상태/로그를 그려주도록 한다. (동시에 여러 건
# 복사를 돌리는 시나리오는 지원하지 않음 - 필요해지면 job_id를 프론트에서
# 직접 관리하도록 확장)
LAST_JOB_ID = None


class ConfigError(Exception):
    """RCLONE_PATH / CONFIG_PATH 가 잘못 설정되었을 때"""


def get_folder_id(drive_url):
    """구글 드라이브 URL에서 폴더 ID를 추출합니다. (원본 g2g.py와 동일 로직)"""
    match = re.search(r"folders/([a-zA-Z0-9-_]+)", drive_url)
    if match:
        return match.group(1)
    drive_url = (drive_url or "").strip()
    if drive_url and "/" not in drive_url:
        return drive_url
    raise ValueError("유효한 구글 드라이브 폴더 주소가 아닙니다.")


def _validate_config(rclone_path, config_path):
    if os.path.isabs(rclone_path) or "/" in rclone_path or "\\" in rclone_path:
        if not os.path.exists(rclone_path):
            raise ConfigError(f"지정한 경로에서 rclone 실행 파일을 찾을 수 없습니다: {rclone_path}")

    if not os.path.exists(config_path):
        raise ConfigError(f"지정한 경로에서 rclone.conf 파일을 찾을 수 없습니다: {config_path}")


def _append_line(job_id, text):
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job["lines"].append(text)


def _run_job(job_id, rclone_path, config_path, rclone_remote, source_id, dest_folder_name):
    source_path = f"{rclone_remote},root_folder_id={source_id}:"
    dest_path = f"{rclone_remote}:{dest_folder_name}"

    cmd = [
        rclone_path,
        "copy",
        source_path,
        dest_path,
        "--config",
        config_path,
        "--progress",
    ]

    _append_line(job_id, "=" * 60)
    _append_line(job_id, f"[*] Rclone 경로       : {rclone_path}")
    _append_line(job_id, f"[*] Config 파일 경로  : {config_path}")
    _append_line(job_id, f"[*] 소스 폴더 ID      : {source_id}")
    _append_line(job_id, f"[*] 목적지 경로       : {dest_path}")
    _append_line(job_id, "=" * 60)
    _append_line(job_id, "[*] 서버사이드 복사를 시작합니다...\n")

    returncode = None
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        for raw_line in process.stdout:
            try:
                decoded = raw_line.decode("utf-8", errors="replace")
            except Exception:
                decoded = raw_line.decode("latin-1", errors="ignore")
            # rclone --progress 는 캐리지리턴(\r)으로 같은 줄을 갱신하므로
            # 줄 단위 로그 뷰에서는 \r 기준으로 쪼개 마지막 조각만 남긴다.
            decoded = decoded.rstrip("\n")
            for piece in decoded.split("\r"):
                if piece:
                    _append_line(job_id, piece)

        process.wait()
        returncode = process.returncode

        if returncode == 0:
            _append_line(job_id, "\n[+] 서버사이드 복사가 성공적으로 완료되었습니다!")
        else:
            _append_line(job_id, f"\n[-] 복사 중 오류가 발생했습니다. (종료 코드: {returncode})")

    except Exception as e:
        _append_line(job_id, f"\n[-] 스크립트 실행 중 예외 발생: {e}")

    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job["status"] = "success" if returncode == 0 else "error"
            job["returncode"] = returncode
            job["finished_at"] = time.time()


def _cleanup_old_jobs():
    with _JOBS_LOCK:
        if len(JOBS) <= _MAX_KEEP_JOBS:
            return
        finished = [
            (jid, j["finished_at"])
            for jid, j in JOBS.items()
            if j["status"] != "running" and j["finished_at"] is not None
        ]
        finished.sort(key=lambda x: x[1])
        for jid, _ in finished[: len(JOBS) - _MAX_KEEP_JOBS]:
            JOBS.pop(jid, None)


def start_copy_job(rclone_path, config_path, rclone_remote, source_folder_url, dest_folder_name):
    """
    유효성 검사 후 백그라운드 스레드로 rclone copy를 시작하고 job_id를 반환합니다.
    (프레임워크 라우트 핸들러에서 호출)
    """
    rclone_path = (rclone_path or "").strip()
    config_path = (config_path or "").strip()
    rclone_remote = (rclone_remote or "").strip()
    dest_folder_name = (dest_folder_name or "").strip()

    if not rclone_path or not config_path or not rclone_remote:
        raise ConfigError("RCLONE_PATH / CONFIG_PATH / RCLONE_REMOTE가 설정되지 않았습니다. 설정 화면에서 먼저 저장해주세요.")
    if not dest_folder_name:
        raise ValueError("목적지 폴더 경로를 입력해주세요.")

    _validate_config(rclone_path, config_path)
    source_id = get_folder_id(source_folder_url)

    # 이미 실행 중인 job이 있으면 중복 실행 막기 (동시 1건 전제)
    with _JOBS_LOCK:
        for j in JOBS.values():
            if j["status"] == "running":
                raise RuntimeError("이미 실행 중인 복사 작업이 있습니다. 완료 후 다시 시도해주세요.")

    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "lines": [],
            "returncode": None,
            "started_at": time.time(),
            "finished_at": None,
            "source_id": source_id,
            "dest_path": f"{rclone_remote}:{dest_folder_name}",
        }
        global LAST_JOB_ID
        LAST_JOB_ID = job_id

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, rclone_path, config_path, rclone_remote, source_id, dest_folder_name),
        daemon=True,
    )
    thread.start()

    _cleanup_old_jobs()
    return job_id


def get_job_status(job_id):
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return None
        # 얕은 복사로 반환 (호출측에서 lines를 직렬화만 함)
        return dict(job)


def get_last_job_status():
    """get_dashboard_data()가 폴링용으로 쓰는, 가장 최근 job의 상태."""
    with _JOBS_LOCK:
        if LAST_JOB_ID is None:
            return None
        job = JOBS.get(LAST_JOB_ID)
        if job is None:
            return None
        return dict(job)
