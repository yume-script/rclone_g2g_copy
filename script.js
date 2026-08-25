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
  const startBtn = container.querySelector('[data-role="start-btn"]');
  const cancelBtn = container.querySelector('[data-role="cancel-btn"]');
  const statusText = container.querySelector('[data-role="status-text"]');
  const logBox = container.querySelector('[data-role="log-box"]');
  const logDest = container.querySelector('[data-role="log-dest"]');

  const STATUS_LABEL = {
    success: '완료',
    cancelled: '사용자가 중단함',
  };

  function renderConfigBanner(cfg) {
    if (!cfg) return;
    if (cfg.configured) {
      banner.setAttribute('data-state', 'ok');
      banner.textContent =
        `설정 완료 · remote: ${cfg.rclone_remote} · rclone: ${cfg.rclone_path}`;
    } else {
      banner.setAttribute('data-state', 'missing');
      banner.textContent =
        'RCLONE_PATH / CONFIG_PATH / RCLONE_REMOTE가 아직 설정되지 않았습니다. 설정 화면에서 먼저 저장해주세요.';
    }
  }

  function appendLines(lines) {
    if (!lines || lines.length === 0) return;
    const toAppend = lines.slice(renderedLineCount);
    if (toAppend.length === 0) return;
    if (renderedLineCount === 0) {
      logBox.textContent = '';
    }
    toAppend.forEach((line) => {
      logBox.textContent += `${line}\n`;
    });
    renderedLineCount = lines.length;
    logBox.scrollTop = logBox.scrollHeight;
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
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
        if (data.job && data.job.status === 'running' && !pollTimer) {
          pollTimer = setInterval(poll, 1000);
        }
      })
      .catch((err) => {
        statusText.textContent = `상태 조회 실패: ${err}`;
        console.error(LOG_PREFIX, '요청 실패:', err);
      });
  }

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
        stopPolling();
        pollTimer = setInterval(poll, 1000);
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
        // 상태 갱신은 poll()이 계속 돌면서 반영 (곧 status가 cancelled로 바뀜)
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
  };

  poll();
  console.log(LOG_PREFIX, '1/2 초기 상태 조회 요청 시작');
})();
