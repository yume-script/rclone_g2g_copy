# -*- coding: utf-8 -*-
"""
rclone_g2g_copy / logic.py

원본 스크립트(g2g.py)의 rclone_server_side_copy() 로직을
"한 번 실행하고 콘솔에 print"에서 "백그라운드 스레드로 실행하고 job 상태를
파일에 기록 -> 프론트가 폴링"으로 이식한 것입니다.

!! 중요: 왜 메모리(dict)가 아니라 파일에 저장하는가 !!
처음엔 모듈 전역 dict(JOBS)에 job 상태를 들고 있었는데, 화면을 새로고침하면
방금까지 보이던 진행 상황이 사라지는 문제가 있었습니다. 원인은 ridi_book
작업 때도 확인됐던 것과 동일합니다: 이 프레임워크는 요청마다 플러그인
모듈/인스턴스를 새로 만드는 것으로 보이고, 그러면 새 요청은 완전히 새
모듈 전역(빈 JOBS dict)을 보게 됩니다. 반면 이미 시작된 백그라운드 스레드는
자신이 캡처한 "예전" 모듈 전역에 계속 값을 쓰고 있어서, 실제 rclone 프로세스는
잘 돌고 있는데 새로고침한 화면에서는 안 보이는 상황이 발생합니다.

그래서 job 상태(job_state.json)와 로그(job.log)를 **파일**에 저장합니다.
파일 경로는 어떤 모듈 인스턴스에서 봐도 항상 같으므로, 새로고침이 어느
인스턴스로 요청을 보내든 항상 같은 최신 상태를 읽습니다.

저장 위치는 요청하신 대로 앱 작업 디렉터리(cwd) 기준
./plugins/data/rclone_g2g_copy/ 를 사용합니다 (plugins/metadata/rclone_g2g_copy/
= 코드, 업데이트 시 통째로 교체됨; ./plugins/data/rclone_g2g_copy/ = 데이터,
업데이트해도 보존됨 - google_links 플러그인에서 확인된 것과 동일한 관례).

중단(취소) 기능도 같은 이유로, 파이썬 객체(Popen 인스턴스) 참조가 아니라
OS가 보장하는 값인 PID를 파일에 저장해두고 os.kill(pid, SIGTERM)으로
직접 종료합니다 - 요청을 처리하는 모듈 인스턴스가 job을 시작했던 그
인스턴스와 달라도 항상 동작합니다.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.request
import uuid

PLUGIN_ID = "rclone_g2g_copy"
# 앱 실행 작업 디렉터리(cwd) 기준 상대 경로. __file__ 기준 상위 폴더를
# 거슬러 올라가는 대신, 요청하신 대로 "./plugins/data/<플러그인id>"를
# 그대로 사용한다 (google_links 플러그인에서 확인된 것과 동일한 상대 경로
# 표기 관례).
DATA_DIR = os.path.join(".", "plugins", "data", PLUGIN_ID)  # ./plugins/data/rclone_g2g_copy

STATE_FILE = os.path.join(DATA_DIR, "job_state.json")
LOG_FILE = os.path.join(DATA_DIR, "job.log")

# 폴링 응답으로 돌려주는 최대 라인 수. 화면 전환/새로고침 직후 첫 폴링에서
# 이 값만큼을 통째로 내려받아 렌더링하므로, 너무 크면 전송량과 렌더링 둘 다
# 느려진다. 최근 상황만 보이면 충분하다는 전제로 낮춰뒀다 - 전체 로그는
# 여전히 job.log 파일에 다 남아있으니 필요하면 서버에서 직접 확인 가능.
_MAX_RETURN_LINES = 30

# 같은 프로세스 안에서의 파일 read-modify-write 경합만 막는 용도(여러 워커/
# 프로세스 간 완전한 동시성 보장은 아님 - 1워커 전제와 동일한 수준의 안전성)
_STATE_LOCK = threading.Lock()


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


def to_rclone_relative_path(path, mount_prefix):
    """
    사용자가 도커/호스트 마운트 기준 경로(예: /mnt/zeeps_member/zeepsmember/공유폴더)를
    입력해도, rclone remote 기준 상대 경로(예: /zeepsmember/공유폴더)로 자동 변환한다.

    rclone remote가 실제로는 호스트에 /mnt/<remote명> 같은 경로로 마운트되어 있는
    경우, 사용자는 파일탐색기/터미널에서 본 마운트 경로를 그대로 붙여넣기 쉬운데,
    rclone copy의 목적지는 "remote:상대경로" 형태라 마운트 접두사가 중복으로
    들어가면 안 된다. 입력이 mount_prefix로 시작하면 그 접두사를 잘라내고,
    아니면 이미 rclone 기준 경로라고 보고 그대로 반환한다.
    """
    path = (path or "").strip()
    if not path:
        return path

    normalized_path = path.rstrip("/")
    normalized_prefix = (mount_prefix or "").strip().rstrip("/")

    if normalized_prefix and normalized_path.startswith(normalized_prefix):
        remainder = normalized_path[len(normalized_prefix):]
        if not remainder.startswith("/"):
            remainder = "/" + remainder
        return remainder or "/"

    return path


def resolve_mount_prefix(mount_prefix, rclone_remote):
    """설정에 MOUNT_PREFIX가 비어있으면 관례적인 기본값(/mnt/<remote명>)을 사용한다."""
    mount_prefix = (mount_prefix or "").strip()
    if mount_prefix:
        return mount_prefix
    rclone_remote = (rclone_remote or "").strip()
    return f"/mnt/{rclone_remote}" if rclone_remote else ""


def _validate_config(rclone_path, config_path):
    if os.path.isabs(rclone_path) or "/" in rclone_path or "\\" in rclone_path:
        if not os.path.exists(rclone_path):
            raise ConfigError(f"지정한 경로에서 rclone 실행 파일을 찾을 수 없습니다: {rclone_path}")

    if not os.path.exists(config_path):
        raise ConfigError(f"지정한 경로에서 rclone.conf 파일을 찾을 수 없습니다: {config_path}")


# ---------------------------------------------------------------------------
# 파일 기반 상태 저장
# ---------------------------------------------------------------------------

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_state(state):
    """임시 파일에 쓴 뒤 원자적으로 교체 - 폴링 중인 다른 요청이 쓰다 만
    JSON을 읽는 상황을 피한다."""
    _ensure_data_dir()
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp_path, STATE_FILE)


def _update_state(**changes):
    with _STATE_LOCK:
        state = _read_state() or {}
        state.update(changes)
        _write_state(state)
        return state


def _reset_log():
    _ensure_data_dir()
    with open(LOG_FILE, "w", encoding="utf-8"):
        pass


def _append_log_line(text):
    _ensure_data_dir()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def _read_log_lines():
    """로그 파일의 마지막 _MAX_RETURN_LINES 줄만 읽는다.

    이전에는 파일 전체를 읽은 뒤 파이썬에서 뒤쪽만 잘라냈는데, 복사가 오래
    걸려 로그가 커지면(수천~수만 줄) 매 폴링(1초 간격)마다 파일 전체를 메모리에
    올리는 게 화면 전환/새로고침 직후 느려지는 원인 중 하나였다. 파일 끝에서부터
    청크 단위로 거꾸로 읽어 필요한 줄 수만 확보하면 로그가 아무리 커져도 매번
    읽는 양이 일정하다.
    """
    try:
        file_size = os.path.getsize(LOG_FILE)
    except FileNotFoundError:
        return []

    chunk_size = 8192
    blocks = []
    lines_found = 0
    remaining = file_size

    with open(LOG_FILE, "rb") as f:
        while remaining > 0 and lines_found <= _MAX_RETURN_LINES:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            f.seek(remaining)
            block = f.read(read_size)
            blocks.append(block)
            lines_found += block.count(b"\n")

    content = b"".join(reversed(blocks)).decode("utf-8", errors="replace")
    lines = content.splitlines()

    truncated = len(lines) > _MAX_RETURN_LINES or remaining > 0
    lines = lines[-_MAX_RETURN_LINES:]
    if truncated:
        lines = ["... (앞부분 생략) ..."] + lines
    return lines


def _process_is_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, TypeError):
        return False
    except OSError:
        return False
    return True


def _notify_discord(webhook_url, content):
    """복사 완료/실패/중단 시 디스코드 웹훅으로 알림을 보낸다.
    표준 라이브러리(urllib)만 쓰고, 실패해도 job 진행/결과 자체에는 영향을
    주지 않도록 예외를 삼킨다 (알림 실패로 job이 죽으면 안 되므로)."""
    webhook_url = (webhook_url or "").strip()
    if not webhook_url:
        return
    try:
        body = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001 - 알림 실패는 조용히 로그만
        _append_log_line(f"[!] 디스코드 알림 전송 실패: {e}")


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def _run_job(job_id, rclone_path, config_path, rclone_remote, source_id, dest_folder_name, discord_webhook_url=None):
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

    _append_log_line("=" * 60)
    _append_log_line(f"[*] Rclone 경로       : {rclone_path}")
    _append_log_line(f"[*] Config 파일 경로  : {config_path}")
    _append_log_line(f"[*] 소스 폴더 ID      : {source_id}")
    _append_log_line(f"[*] 목적지 경로       : {dest_path}")
    _append_log_line("=" * 60)
    _append_log_line("[*] 서버사이드 복사를 시작합니다...\n")

    returncode = None
    process = None
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _update_state(pid=process.pid)

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
                    _append_log_line(piece)

        process.wait()
        returncode = process.returncode

    except Exception as e:
        _append_log_line(f"\n[-] 스크립트 실행 중 예외 발생: {e}")

    state = _read_state() or {}
    cancelled = bool(state.get("cancel_requested"))

    if cancelled:
        status = "cancelled"
        _append_log_line("\n[-] 사용자 요청으로 복사가 중단되었습니다.")
        notify_text = f"⏹️ **[BookOasis] 폴더 복사 중단됨**\n목적지: `{dest_path}`"
    elif returncode == 0:
        status = "success"
        _append_log_line("\n[+] 서버사이드 복사가 성공적으로 완료되었습니다!")
        notify_text = f"✅ **[BookOasis] 폴더 복사 완료**\n목적지: `{dest_path}`"
    else:
        status = "error"
        _append_log_line(f"\n[-] 복사 중 오류가 발생했습니다. (종료 코드: {returncode})")
        notify_text = f"❌ **[BookOasis] 폴더 복사 실패** (종료 코드: {returncode})\n목적지: `{dest_path}`"

    _update_state(status=status, returncode=returncode, finished_at=time.time(), pid=None)
    _notify_discord(discord_webhook_url, notify_text)


def start_copy_job(rclone_path, config_path, rclone_remote, source_folder_url, dest_folder_name,
                    source_url_input=None, dest_input=None, discord_webhook_url=None):
    """
    유효성 검사 후 백그라운드 스레드로 rclone copy를 시작합니다.
    이미 실행 중인(그리고 실제로 살아있는) job이 있으면 거부합니다.

    source_url_input / dest_input: 변환 전, 사용자가 화면에 실제로 타이핑한 원본
    값(소스는 URL 그대로, 목적지는 마운트 경로일 수도 있는 원본). 새로고침 시
    입력창을 그대로 복원해주기 위해 job 상태에 함께 저장한다.

    discord_webhook_url: 설정된 경우, 복사가 끝났을 때(성공/실패/중단 모두)
    디스코드로 알림을 보낸다. 비어있으면 알림을 보내지 않는다.
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

    existing = _read_state()
    if existing and existing.get("status") == "running" and _process_is_alive(existing.get("pid")):
        raise RuntimeError("이미 실행 중인 복사 작업이 있습니다. 완료 또는 중단 후 다시 시도해주세요.")

    job_id = uuid.uuid4().hex[:12]
    _reset_log()
    _write_state({
        "job_id": job_id,
        "status": "running",
        "pid": None,
        "cancel_requested": False,
        "returncode": None,
        "started_at": time.time(),
        "finished_at": None,
        "source_id": source_id,
        "dest_path": f"{rclone_remote}:{dest_folder_name}",
        # 새로고침 시 입력창 복원용 원본 값
        "source_url_input": (source_url_input or source_folder_url or "").strip(),
        "dest_input": (dest_input or dest_folder_name or "").strip(),
    })

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, rclone_path, config_path, rclone_remote, source_id, dest_folder_name, discord_webhook_url),
        daemon=True,
    )
    thread.start()

    return job_id


def cancel_current_job():
    """실행 중인 job을 중단합니다. PID에 SIGTERM을 보내고, 응답이 없으면
    잠시 후 SIGKILL로 강제 종료합니다."""
    state = _read_state()
    if not state or state.get("status") != "running":
        return False, "진행 중인 복사 작업이 없습니다."

    pid = state.get("pid")
    _update_state(cancel_requested=True)

    if not pid:
        # 아직 rclone 프로세스가 실제로 뜨기 전(아주 짧은 순간)일 수 있음 -
        # cancel_requested만 세워두면 프로세스가 뜬 직후 상태를 봐서
        # 알아서 정리되도록 하는 편이 안전하지만, 여기서는 바로 응답한다.
        return True, "중단을 요청했습니다."

    if not _process_is_alive(pid):
        # 이미 끝난 프로세스 - _run_job이 곧 상태를 정리할 것
        return True, "이미 종료된 작업입니다."

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception as e:
        return False, f"중단 요청 중 오류: {e}"

    def _force_kill_if_still_alive():
        time.sleep(5)
        if _process_is_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

    threading.Thread(target=_force_kill_if_still_alive, daemon=True).start()

    return True, "중단을 요청했습니다. 잠시 후 종료됩니다."


def get_last_job_status():
    """get_dashboard_data()가 폴링용으로 쓰는, 가장 최근 job의 상태 + 로그."""
    state = _read_state()
    if state is None:
        return None

    # status가 "running"인데 실제 프로세스가 죽어있으면(예: 컨테이너
    # 재시작으로 스레드 자체가 사라진 경우) 좀비 상태로 영원히 "진행 중"으로
    # 보이는 것을 막기 위해 여기서 정리한다. 단, job을 막 시작해서 아직
    # pid가 기록되기 전(Popen 호출 직전)일 수 있으므로 시작 직후 몇 초간은
    # 봐준다.
    just_started = (time.time() - (state.get("started_at") or 0)) < 5
    if state.get("status") == "running" and not state.get("pid") and just_started:
        pass  # 아직 pid 기록 전 - 정상, 다음 폴링 때 다시 확인
    elif state.get("status") == "running" and not _process_is_alive(state.get("pid")):
        state = _update_state(
            status="error",
            returncode=None,
            finished_at=time.time(),
        )
        _append_log_line("\n[-] 서버가 재시작되어 진행 상황을 더 이상 추적할 수 없습니다. (복사가 이미 끝났을 수도 있습니다 - rclone remote에서 직접 확인해주세요)")

    result = dict(state)
    result["lines"] = _read_log_lines()
    return result
