// rclone_g2g_copy 플러그인 카테고리탭 풀페이지 스크립트
// scan_scheduler의 script.js와 동일하게 new Function('pluginId', 'container', ...)로
// 실행되므로 import 없이 전역 API + 인자로 받는 pluginId/container만 사용합니다.

(function () {
  const LOG_PREFIX = '[rclone_g2g_copy]';
  console.log(LOG_PREFIX, '0/2 카테고리탭 UI 로드됨.');

  // scan_scheduler와 동일하게, 이 플러그인도 특정 db_type(라이브러리 스코프)에
  // 종속되지 않는 전역 유틸리티라 'general'로 고정해서 보냅니다.
  const DB_TYPE = 'general';

  let pollTimer = null;
  let renderedLineCount = 0;
  let lastJobStatus = null; // running | success | error | cancelled | null(아직 없음)

  const banner = container.querySelector('[data-role="config-banner"]');
  const sourceInput = container.querySelector('[data-role="source-url"]');
  const destInput = container.querySelector('[data-role="dest-folder"]');
  const destPreview = container.querySelector('[data-role="dest-preview"]');
  const startBtn = container.querySelector('[data-role="start-btn"]');
  const cancelBtn = container.querySelector('[data-role="cancel-btn"]');
  const statusText = container.querySelector('[data-role="status-text"]');
  const logBox = container.querySelector('[data-role="log-box"]');
  const logDest = container.querySelector('[data-role="log-dest"]');

  let mountPrefix = '';
  let inputsPrefilled = false;

  // 폴링 주기. 이 프레임워크는 요청마다 플러그인 모듈을 새로 로드하는
  // 구조라(README 참고), 폴링이 잦을수록 서버 부하가 커진다. 그래서:
  //  - 시작 직후 잠깐만(START_BURST_MS) 빠르게(FAST_MS) 확인해서 반응성을 살리고,
  //  - 그 뒤로는 느리게(SLOW_MS)만 확인한다.
  //  - 브라우저 탭이 보이지 않을 때는(document.hidden) 폴링을 완전히 멈추고,
  //    다시 보이게 되면 즉시 한 번 확인 후 재개한다.
  const POLL_FAST_MS = 2000;
  const POLL_SLOW_MS = 8000;
  const POLL_FAST_WINDOW_MS = 20000;
  let pollStartedAt = 0;

  const STATUS_LABEL = {
    success: '완료',
    cancelled: '사용자가 중단함',
  };

  // logic.py의 to_rclone_relative_path()와 동일한 규칙: 입력이 마운트
  // 접두사로 시작하면 그 부분을 잘라내 rclone 기준 상대 경로로 바꾼다.
  // (서버에서도 동일하게 다시 한 번 변환하므로, 여기는 미리보기 전용)
  function toRcloneRelativePath(path, prefix) {
    const p = (path || '').trim();
    if (!p) return p;
    const normPath = p.replace(/\/+$/, '');
    const normPrefix = (prefix || '').trim().replace(/\/+$/, '');
    if (normPrefix && normPath.startsWith(normPrefix)) {
      let remainder = normPath.slice(normPrefix.length);
      if (!remainder.startsWith('/')) remainder = '/' + remainder;
      return remainder || '/';
    }
    return p;
  }

  function updateDestPreview() {
    const raw = (destInput.value || '').trim();
    if (!raw) {
      destPreview.textContent = '';
      destPreview.removeAttribute('data-state');
      return;
    }
    const converted = toRcloneRelativePath(raw, mountPrefix);
    if (converted !== raw) {
      destPreview.textContent = `→ rclone 기준 경로: ${converted}`;
      destPreview.setAttribute('data-state', 'converted');
    } else {
      destPreview.textContent = `rclone 기준 경로로 그대로 사용됩니다: ${raw}`;
      destPreview.removeAttribute('data-state');
    }
  }

  destInput.addEventListener('input', updateDestPreview);

  function renderConfigBanner(cfg) {
    if (!cfg) return;
    mountPrefix = cfg.mount_prefix || '';
    updateDestPreview();
    if (cfg.configured) {
      banner.setAttribute('data-state', 'ok');
      const discordNote = cfg.discord_notify_enabled ? ' · 디스코드 알림 켜짐' : '';
      banner.textContent =
        `설정 완료 · remote: ${cfg.rclone_remote} · rclone: ${cfg.rclone_path} · 마운트 접두사: ${cfg.mount_prefix}${discordNote}`;
    } else {
      banner.setAttribute('data-state', 'missing');
      banner.textContent =
        'RCLONE_PATH / CONFIG_PATH / RCLONE_REMOTE가 아직 설정되지 않았습니다. 설정 화면에서 먼저 저장해주세요.';
    }
  }

  function appendLines(lines) {
    // 이전엔 새 줄마다 logBox.textContent += line 을 반복했는데, 줄이
    // 많아지면(수백~수천 줄) 매번 전체 문자열을 새로 복사하게 되어(사실상
    // O(n^2)) 화면 전환/새로고침 직후 첫 렌더링이 눈에 띄게 느렸다.
    // 서버가 최근 최대 500줄만 내려주므로(logic.py의 _MAX_RETURN_LINES),
    // 매 폴링마다 배열을 한 번에 join해서 통째로 다시 그려도 충분히 가볍다
    // (길이만 비교해서 건너뛰면, 오래된 줄이 잘려나가고 새 줄이 추가돼
    // 총 길이가 그대로인 경우를 놓쳐 갱신이 멈춘 것처럼 보이는 버그가 있었음).
    if (!lines) return;

    const nearBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 40;
    logBox.textContent = lines.join('\n');
    renderedLineCount = lines.length;
    if (nearBottom) {
      logBox.scrollTop = logBox.scrollHeight;
    }
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function scheduleNextPoll() {
    stopPolling();
    if (document.hidden) return; // 탭이 안 보이면 예약하지 않음 - visibilitychange가 재개시킴
    const elapsed = Date.now() - pollStartedAt;
    const interval = elapsed < POLL_FAST_WINDOW_MS ? POLL_FAST_MS : POLL_SLOW_MS;
    pollTimer = setTimeout(poll, interval);
  }

  function setRunningUI(isRunning) {
    startBtn.disabled = isRunning;
    cancelBtn.hidden = !isRunning;
    cancelBtn.disabled = false;
  }

  function renderJob(job) {
    if (!job) {
      logDest.textContent = '';
      setRunningUI(false);
      return;
    }

    // 화면을 새로 열었을 때(또는 새로고침) 이미 진행 중이거나 방금 끝난 job이
    // 있으면, 사용자가 입력했던 원본 값(변환 전)을 그대로 입력창에 복원한다.
    // 딱 한 번만 채우고, 이후에는 사용자가 직접 수정한 값을 건드리지 않는다.
    if (!inputsPrefilled) {
      inputsPrefilled = true;
      if (job.source_url_input && !sourceInput.value) {
        sourceInput.value = job.source_url_input;
      }
      if (job.dest_input && !destInput.value) {
        destInput.value = job.dest_input;
      }
      updateDestPreview();
    }
    logDest.textContent = job.dest_path ? `→ ${job.dest_path}` : '';
    appendLines(job.lines);

    if (job.status === 'running') {
      setRunningUI(true);
    }

    if (job.status === lastJobStatus) return;
    lastJobStatus = job.status;

    if (job.status === 'running') {
      statusText.textContent = '복사 진행 중...';
    } else if (job.status === 'success' || job.status === 'cancelled') {
      statusText.textContent = STATUS_LABEL[job.status];
      setRunningUI(false);
      stopPolling();
    } else if (job.status === 'error') {
      statusText.textContent = `오류로 종료됨 (종료 코드: ${job.returncode})`;
      setRunningUI(false);
      stopPolling();
    }
  }

  // ==================================================================
  // 데이터 로딩 (설정 상태 + 최근 job 상태) - scan_scheduler의
  // fetchSchedules()와 동일한 엔드포인트 규약
  // ==================================================================
  function poll() {
    const params = new URLSearchParams({ type: DB_TYPE, limit: '1' });
    const url = `/api/media/dashboard/widgets/${pluginId}/data?${params.toString()}`;

    fetch(url)
      .then((res) => res.json())
      .then((data) => {
        if (!data.success) {
          statusText.textContent = `상태 조회 실패: ${data.error || '알 수 없는 오류'}`;
          console.warn(LOG_PREFIX, '데이터 조회 실패:', data.error);
          return;
        }
        renderConfigBanner(data.config);
        renderJob(data.job);

        if (data.job && data.job.status === 'running') {
          if (!pollStartedAt) pollStartedAt = Date.now();
          scheduleNextPoll();
        } else {
          pollStartedAt = 0;
          stopPolling();
        }
      })
      .catch((err) => {
        statusText.textContent = `상태 조회 실패: ${err}`;
        console.error(LOG_PREFIX, '요청 실패:', err);
        // 네트워크 오류로도 폴링이 끊기지 않도록, 진행 중이었다면 계속 재시도
        if (pollStartedAt) scheduleNextPoll();
      });
  }

  // 탭이 백그라운드로 가면 폴링을 멈추고, 다시 보이면 즉시 한 번 확인 후
  // 필요하면 재개한다 - 안 보고 있는 동안의 불필요한 부하를 없앤다.
  function onVisibilityChange() {
    if (document.hidden) {
      stopPolling();
    } else {
      poll();
    }
  }
  document.addEventListener('visibilitychange', onVisibilityChange);

  // ==================================================================
  // 액션 호출 공통부 - scan_scheduler의 saveEdit()과 동일한 호출 규약:
  // POST /api/media/books/0/apply-metadata,
  // body { type, source: pluginId, item_data }, 응답은 data.success / data.error
  // ==================================================================
  function callApply(itemData) {
    return fetch('/api/media/books/0/apply-metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: DB_TYPE, source: pluginId, item_data: itemData }),
    }).then((res) => res.json());
  }

  function startCopy() {
    const sourceUrl = (sourceInput.value || '').trim();
    const destFolder = (destInput.value || '').trim();

    if (!sourceUrl) {
      statusText.textContent = '소스 폴더 URL(또는 ID)을 입력해주세요.';
      return;
    }
    if (!destFolder) {
      statusText.textContent = '목적지 경로를 입력해주세요.';
      return;
    }

    startBtn.disabled = true;
    statusText.textContent = '요청을 보내는 중...';
    renderedLineCount = 0;
    lastJobStatus = null;
    logBox.textContent = '';
    logDest.textContent = '';

    callApply({
      action: 'start_copy',
      source_url: sourceUrl,
      dest_folder_name: destFolder,
    })
      .then((data) => {
        if (!data || !data.success) {
          statusText.textContent = (data && (data.error || data.message)) || '요청이 거부되었습니다.';
          startBtn.disabled = false;
          return;
        }
        statusText.textContent = data.message || '복사를 시작했습니다.';
        console.log(LOG_PREFIX, '복사 시작 요청 성공');
        pollStartedAt = Date.now(); // 시작 직후 잠깐은 빠르게 확인
        poll();
      })
      .catch((err) => {
        statusText.textContent = `시작 실패: ${err}`;
        startBtn.disabled = false;
        console.error(LOG_PREFIX, '요청 실패:', err);
      });
  }

  function cancelCopy() {
    if (!window.confirm('진행 중인 복사를 중단할까요? 이미 복사된 파일은 그대로 남습니다.')) {
      return;
    }
    cancelBtn.disabled = true;
    statusText.textContent = '중단 요청 중...';

    callApply({ action: 'cancel_copy' })
      .then((data) => {
        if (!data || !data.success) {
          statusText.textContent = (data && (data.error || data.message)) || '중단 요청이 거부되었습니다.';
          cancelBtn.disabled = false;
          return;
        }
        statusText.textContent = data.message || '중단을 요청했습니다.';
        console.log(LOG_PREFIX, '중단 요청 성공');
        // 중단 처리가 실제로 끝나는 걸 빨리 반영하도록 잠깐 빠른 주기로 전환
        pollStartedAt = Date.now();
        poll();
      })
      .catch((err) => {
        statusText.textContent = `중단 요청 실패: ${err}`;
        cancelBtn.disabled = false;
        console.error(LOG_PREFIX, '요청 실패:', err);
      });
  }

  startBtn.addEventListener('click', startCopy);
  cancelBtn.addEventListener('click', cancelCopy);

  // 탭이 언마운트될 때 폴링 타이머가 남지 않도록 정리 레지스트리에 등록
  // (plugin_hub 작업 때 확인된 window.__bookOasisViewerCleanups 관례)
  window.__bookOasisViewerCleanups = window.__bookOasisViewerCleanups || {};
  window.__bookOasisViewerCleanups[pluginId] = function () {
    stopPolling();
    document.removeEventListener('visibilitychange', onVisibilityChange);
  };

  poll();
  console.log(LOG_PREFIX, '1/2 초기 상태 조회 요청 시작');
})();
