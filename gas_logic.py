# -*- coding: utf-8 -*-
"""
rclone_g2g_copy / gas_logic.py

Google Apps Script(GAS) 웹앱(gas/Code.gs를 배포한 것)을 통해 구글 드라이브
폴더를 서버사이드로 복사하는 **대체 백엔드**입니다. 카테고리탭 화면의
체크박스로 rclone/GAS 중 골라 쓸 수 있습니다.

설계 원칙: 실제 복사 작업은 전적으로 구글 인프라(Apps Script 시간기반
트리거) 안에서 실행됩니다. 이쪽(BookOasis 서버)은 그 웹앱에 상태를
물어보기만(polling) 하면 되고, rclone 백엔드처럼 "백그라운드 스레드가
계속 살아있어야 한다"는 부담이 없습니다.

job_state.json을 최신으로 유지하는 책임은, UI가 폴링할 때마다 호출되는
maybe_refresh_gas_job()이 그 순간 웹앱에 최신 상태를 물어보고 갱신하는
방식으로 해결합니다 - "요청마다 플러그인 모듈이 새로 로드되는" 이 프레임워크
특성과도 자연히 잘 맞습니다 (어느 모듈 인스턴스가 요청을 받아도 그 순간
바로 최신 정보를 다시 받아오면 되므로, 백그라운드 스레드가 orphan되는 걱정이
아예 없음).

!! 실제 배포된 Google Apps Script 웹앱으로 직접 테스트하지 못했습니다 !!
아래 HTTP 요청/응답 처리 로직 자체는 이 파일만 놓고 유닛테스트(모킹)했지만,
gas/Code.gs가 실제로 이 계약대로 응답하는지는 script.google.com에 배포해서
직접 확인해주셔야 합니다. 문제가 있으면 알려주시면 바로 고쳐드리겠습니다.
"""

import json
import time
import urllib.error
import urllib.request

from .logic import _process_is_alive, _read_state, _update_state, _write_state, get_folder_id

_REQUEST_TIMEOUT = 15  # 초 - Apps Script 콜드스타트가 느릴 수 있어 넉넉히 잡음
_MAX_CONSECUTIVE_FAILURES = 5  # 이 횟수만큼 연속으로 상태 조회에 실패하면 job을 error로 정리
_MAX_LOG_LINES = 30  # rclone 백엔드의 _MAX_RETURN_LINES와 동일한 정신


class GasError(Exception):
    """GAS 웹앱 설정/통신 문제 (URL 미설정, 네트워크 오류, 응답 파싱 실패 등)"""


def _gas_request(webapp_url, secret, payload):
    """webapp_url에 POST로 JSON을 보내고 JSON을 파싱해 반환한다.
    통신/파싱 실패 시 GasError를 던진다 (호출측에서 잡아서 상태에 반영)."""
    webapp_url = (webapp_url or "").strip()
    if not webapp_url:
        raise GasError("GAS_WEBAPP_URL이 설정되지 않았습니다. 설정 화면에서 먼저 저장해주세요.")

    body = dict(payload)
    body["secret"] = secret or ""

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        webapp_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = ""
        raise GasError(f"웹앱 응답 오류 (HTTP {e.code}) {detail}")
    except urllib.error.URLError as e:
        raise GasError(f"웹앱에 연결할 수 없습니다: {e.reason}")
    except Exception as e:  # noqa: BLE001
        raise GasError(f"요청 중 오류: {e}")

    try:
        parsed = json.loads(raw)
    except Exception:
        raise GasError(f"웹앱 응답을 JSON으로 해석할 수 없습니다 (앞부분: {raw[:200]!r})")

    if not isinstance(parsed, dict):
        raise GasError(f"웹앱 응답 형식이 예상과 다릅니다: {raw[:200]!r}")

    return parsed


def start_gas_job(webapp_url, secret, source_folder_url, dest_folder_url,
                   source_url_input=None, dest_input=None):
    """GAS 웹앱에 복사 시작을 요청하고, 성공하면 job_state.json을 새로 씁니다.

    rclone 백엔드와 달리 목적지도 (마운트 경로가 아니라) 구글 드라이브 폴더
    URL/ID입니다 - 소스와 동일한 get_folder_id()로 추출합니다.
    """
    source_folder_url = (source_folder_url or "").strip()
    dest_folder_url = (dest_folder_url or "").strip()
    if not source_folder_url:
        raise ValueError("소스 폴더 URL(또는 ID)을 입력해주세요.")
    if not dest_folder_url:
        raise ValueError("목적지 폴더 URL(또는 ID)을 입력해주세요.")

    source_folder_id = get_folder_id(source_folder_url)
    dest_folder_id = get_folder_id(dest_folder_url)

    # 동시 1건 제한 - 웹앱 쪽도 확인하지만, rclone 백엔드와 일관된 UX를 위해
    # 여기서도 먼저 확인한다. 다만 로컬 상태가 "running"이라고 해서 무조건
    # 막지는 않는다 - 실제로는 이미 끝났는데 로컬에 아직 반영이 안 됐을 수
    # 있어서(예: 화면을 안 열어봐서 폴링이 한 번도 안 일어난 경우), 막기 전에
    # 백엔드별로 "진짜 살아있는지" 한 번 더 확인한다 (rclone의 pid-alive
    # 체크와 같은 취지).
    existing = _read_state()
    if existing and existing.get("status") == "running":
        if existing.get("backend") == "gas":
            # 이 job이 실제로 아직 GAS에서 돌고 있는지 웹앱에 다시 물어본다 -
            # 이미 끝났다면(success/error/cancelled) 여기서 로컬 상태가 정리되어
            # 아래 최종 판정에서 막히지 않는다.
            existing = refresh_gas_status(existing, webapp_url, secret)
        else:
            # rclone job이었던 경우 - 실제 프로세스가 죽어있으면(비정상 종료 등)
            # 막지 않는다.
            if not _process_is_alive(existing.get("pid")):
                existing = None

    if existing and existing.get("status") == "running":
        raise RuntimeError("이미 실행 중인 복사 작업이 있습니다. 완료 또는 중단 후 다시 시도해주세요.")

    result = _gas_request(webapp_url, secret, {
        "action": "start",
        "source_folder_id": source_folder_id,
        "dest_folder_id": dest_folder_id,
    })

    if not result.get("success"):
        raise RuntimeError(result.get("error") or "GAS 웹앱이 시작을 거부했습니다.")

    gas_job_id = result.get("job_id")
    if not gas_job_id:
        raise RuntimeError("GAS 웹앱이 job_id를 돌려주지 않았습니다 (gas/Code.gs 응답 형식을 확인해주세요).")

    _write_state({
        "job_id": gas_job_id,  # rclone 백엔드와 필드명을 통일 (프론트가 백엔드 구분 없이 다룰 수 있게)
        "backend": "gas",
        "status": "running",
        "pid": None,  # GAS는 로컬 프로세스가 없음
        "cancel_requested": False,
        "returncode": None,
        "started_at": time.time(),
        "finished_at": None,
        "source_id": source_folder_id,
        "dest_path": f"Google Drive 폴더 ID: {dest_folder_id}",
        "source_url_input": (source_url_input or source_folder_url).strip(),
        "dest_input": (dest_input or dest_folder_url).strip(),
        "progress": {},
        "gas_job_id": gas_job_id,
        "gas_log_lines": [],
        "gas_refresh_failures": 0,
    })

    return gas_job_id


def refresh_gas_status(state, webapp_url, secret):
    """실행 중인 GAS job의 최신 상태를 웹앱에 물어보고 job_state.json에 반영한다.
    통신 실패는 즉시 error로 처리하지 않고(일시적 문제일 수 있음)
    _MAX_CONSECUTIVE_FAILURES번 연속 실패해야 error로 정리한다."""
    gas_job_id = state.get("gas_job_id")
    if not gas_job_id:
        return state

    try:
        result = _gas_request(webapp_url, secret, {"action": "status", "job_id": gas_job_id})
    except GasError as e:
        failures = int(state.get("gas_refresh_failures", 0)) + 1
        log_lines = list(state.get("gas_log_lines", []))
        log_lines.append(f"[!] 상태 조회 실패 ({failures}/{_MAX_CONSECUTIVE_FAILURES}): {e}")
        changes = {
            "gas_refresh_failures": failures,
            "gas_log_lines": log_lines[-_MAX_LOG_LINES:],
        }
        if failures >= _MAX_CONSECUTIVE_FAILURES:
            changes["status"] = "error"
            changes["finished_at"] = time.time()
        return _update_state(**changes)

    if not result.get("success"):
        # job_id를 못 찾는 경우 등 (예: 웹앱이 재배포되어 이전 job 기록이 사라짐)
        log_lines = list(state.get("gas_log_lines", []))
        log_lines.append(f"[!] {result.get('error') or '알 수 없는 오류'}")
        return _update_state(
            status="error",
            finished_at=time.time(),
            gas_log_lines=log_lines[-_MAX_LOG_LINES:],
        )

    changes = {
        "gas_refresh_failures": 0,
        "gas_log_lines": (result.get("log_lines") or [])[-_MAX_LOG_LINES:],
    }

    files_done = result.get("files_done")
    files_total = result.get("files_total")
    progress = dict(state.get("progress") or {})
    if files_done is not None:
        progress["files_done"] = files_done
    if files_total:
        progress["files_total"] = files_total
        progress["files_percent"] = int(files_done * 100 / files_total) if files_done is not None else 0
        # GAS 쪽은 바이트 단위 진행률을 따로 안 주므로(전체 목록을 미리
        # 세지 않아 총 용량을 모름), 파일개수 기준을 그대로 "전체 진행률"로 쓴다.
        progress["percent"] = progress["files_percent"]
    if progress:
        changes["progress"] = progress

    gas_status = result.get("status")
    if gas_status == "success":
        changes["status"] = "success"
        changes["finished_at"] = time.time()
        final_progress = dict(changes.get("progress") or progress)
        final_progress["percent"] = 100
        if final_progress.get("files_total"):
            final_progress["files_done"] = final_progress["files_total"]
            final_progress["files_percent"] = 100
        changes["progress"] = final_progress
    elif gas_status == "error":
        changes["status"] = "error"
        changes["finished_at"] = time.time()
    elif gas_status == "cancelled":
        changes["status"] = "cancelled"
        changes["finished_at"] = time.time()
    # gas_status == "running"이면 status는 그대로 두면 됨(이미 running)

    return _update_state(**changes)


def maybe_refresh_gas_job(webapp_url, secret):
    """현재 job이 GAS 백엔드이고 아직 실행 중이면, 웹앱에 최신 상태를 물어봐서
    job_state.json을 갱신한다. rclone job이거나 job이 없거나 이미 끝났으면
    아무 것도 하지 않는다. (get_dashboard_data()가 매 폴링마다 호출)"""
    state = _read_state()
    if not state or state.get("backend") != "gas" or state.get("status") != "running":
        return
    refresh_gas_status(state, webapp_url, secret)


def cancel_gas_job(state, webapp_url, secret):
    """웹앱에 중단을 요청한다. GAS 쪽 트리거가 다음 체크포인트(최대 1분 이내)에서
    스스로 멈추므로, 즉시 종료되지는 않는다."""
    gas_job_id = state.get("gas_job_id")
    if not gas_job_id:
        return False, "GAS job 정보를 찾을 수 없습니다."

    try:
        result = _gas_request(webapp_url, secret, {"action": "cancel", "job_id": gas_job_id})
    except GasError as e:
        return False, f"중단 요청 실패: {e}"

    if not result.get("success"):
        return False, result.get("error") or "중단 요청이 거부되었습니다."

    _update_state(cancel_requested=True)
    return True, "중단을 요청했습니다. 다음 체크포인트에서 멈춥니다 (최대 1분 정도 걸릴 수 있습니다)."
