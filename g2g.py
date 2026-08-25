import os
import re
import subprocess

# =====================================================================
# [사용자 설정 영역] 본인 환경에 맞게 변수 값을 수정하세요.
# =====================================================================

# 1. rclone 실행 파일 경로
#    - 환경변수에 등록된 경우: "rclone"
#    - 리눅스/맥 절대경로 예시: "/usr/bin/rclone"
RCLONE_PATH = "/usr/bin/rclone"

# 2. rclone.conf 파일 경로 (현재 폴더에 없으므로 실제 파일이 있는 절대/상대 경로 지정)
#    - 리눅스 예시: "/home/username/.config/rclone/rclone.conf"
CONFIG_PATH = "/root/docker/ff/db/rclone.conf"

# 3. rclone.conf에 등록된 리모트(Remote) 이름 (예: 'gdrive')
RCLONE_REMOTE = "zeeps_member"

# 4. 복사하려는 구글 드라이브 폴더 주소 (또는 폴더 ID)
SOURCE_URL = (
    "https://drive.google.com/drive/folders/1ymLEWVRxKqdFR7HSPG96qR4XvOEKSZGx"
)

# 5. 복사될 구글드라이브 폴더명 (내 구글 드라이브 기준 목적지 경로)
DEST_FOLDER_NAME = "/zeepsmember/crars님_공유_2"

# =====================================================================

def get_folder_id(drive_url):
  """구글 드라이브 URL에서 폴더 ID를 추출합니다."""
  match = re.search(r"folders/([a-zA-Z0-9-_]+)", drive_url)
  if match:
    return match.group(1)
  if "/" not in drive_url:
    return drive_url
  raise ValueError("유효한 구글 드라이브 폴더 주소가 아닙니다.")


def rclone_server_side_copy(
    rclone_path, config_path, rclone_remote, source_folder_url, dest_folder_name
):
  """Rclone을 이용해 원격 서버사이드 복사를 수행합니다."""
  if os.path.isabs(rclone_path) or "/" in rclone_path or "\\" in rclone_path:
    if not os.path.exists(rclone_path):
      print(f"[-] 오류: 지정한 경로에서 rclone 실행 파일을 찾을 수 없습니다: {rclone_path}")
      return

  if not os.path.exists(config_path):
    print(
        f"[-] 오류: 지정한 경로에서 rclone.conf 파일을 찾을 수 없습니다: {config_path}"
    )
    return

  try:
    source_id = get_folder_id(source_folder_url)
  except ValueError as e:
    print(f"[-] 오류: {e}")
    return

  source_path = f"{rclone_remote},root_folder_id={source_id}:"
  dest_path = f"{rclone_remote}:{dest_folder_name}"

  # 유효하지 않은 플래그(--drive-server-side-copy-concurrency) 제거됨
  cmd = [
      rclone_path,
      "copy",
      source_path,
      dest_path,
      "--config",
      config_path,
      "--progress",
  ]

  print("=" * 60)
  print(f"[*] Rclone 경로       : {rclone_path}")
  print(f"[*] Config 파일 경로  : {config_path}")
  print(f"[*] 소스 폴더 ID      : {source_id}")
  print((f"[*] 목적지 경로       : {dest_path}"))
  print("=" * 60)
  print("[*] 서버사이드 복사를 시작합니다...\n")

  try:
    # text=True를 제거하고 바이트 스트림(stdout)으로 받은 뒤,
    # 인코딩 에러가 나는 문자는 치환(replace)하여 깨짐을 방지합니다.
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )

    for line in process.stdout:
      try:
        # utf-8로 디코딩하되, 해독할 수 없는 바이트는 '?' 등으로 대체하여 에러 회피
        decoded_line = line.decode("utf-8", errors="replace")
        print(decoded_line, end="")
      except Exception:
        # 최후의 보루: 다른 인코딩 시도 혹은 무시
        print(line.decode("latin-1", errors="ignore"), end="")

    process.wait()

    if process.returncode == 0:
      print("\n[+] 서버사이드 복사가 성공적으로 완료되었습니다!")
    else:
      print(
          f"\n[-] 복사 중 오류가 발생했습니다. (종료 코드:"
          f" {process.returncode})"
      )

  except Exception as e:
    print(f"\n[-] 스크립트 실행 중 예외 발생: {e}")


if __name__ == "__main__":
  rclone_server_side_copy(
      rclone_path=RCLONE_PATH,
      config_path=CONFIG_PATH,
      rclone_remote=RCLONE_REMOTE,
      source_folder_url=SOURCE_URL,
      dest_folder_name=DEST_FOLDER_NAME,
  )