// rclone_g2g_copy / settings.js
//
// plugin_hub 작업에서 확인된 계약: 설정 화면 JS는
// new Function('window','pluginId','root','config', js)로 실행된다.
// - root: 이 플러그인 설정 UI(settings.html 내용)가 삽입된 컨테이너
// - config: 현재 저장되어 있는 설정 값 객체 (RCLONE_PATH, CONFIG_PATH, ...)
//
// 저장 방식은 그대로: name 속성이 있는 입력 요소를 코어가 모아서 저장한다.
// RCLONE_REMOTE는 두 입력(select/text) 중 "지금 화면에 보이는 쪽"에만
// name="RCLONE_REMOTE"를 붙여서, 저장 시 숨겨진 쪽 값에 덮어써지지 않게 한다.
// 텍스트 입력 쪽은 처음부터 name="RCLONE_REMOTE"를 갖고 있어서(정적 HTML),
// 이 스크립트가 어떤 이유로든 실행에 실패해도 기존처럼 수동 입력은 항상 동작한다.

(function (window, pluginId, root, config) {
  const selectEl = root.querySelector('[data-role="remote-select"]');
  const textEl = root.querySelector('[data-role="remote-text"]');
  const hintEl = root.querySelector('[data-role="remote-hint"]');
  const refreshBtn = root.querySelector('[data-role="refresh-remotes-btn"]');
  const configPathInput = root.querySelector('[data-role="config-path-input"]');

  if (!selectEl || !textEl) return; // settings.html 구조가 다르면 조용히 포기 (기존 텍스트 입력은 그대로 동작)

  function setHint(text, state) {
    hintEl.textContent = text;
    if (state) {
      hintEl.setAttribute('data-state', state);
    } else {
      hintEl.removeAttribute('data-state');
    }
  }

  function showTextMode(message, state) {
    selectEl.hidden = true;
    selectEl.removeAttribute('name');
    textEl.hidden = false;
    textEl.setAttribute('name', 'RCLONE_REMOTE');
    setHint(message || 'rclone.conf에 등록된 리모트 이름입니다. 직접 입력해주세요.', state);
  }

  function showSelectMode(remotes, currentValue) {
    selectEl.innerHTML = '';

    // 지금 선택/저장돼있던 값이 remotes 목록에 없으면(예: rclone.conf에서
    // 이미 지워진 remote), 설정을 조용히 바꿔버리지 않도록 옵션에 그대로
    // 끼워넣어준다.
    const options = remotes.slice();
    if (currentValue && !options.includes(currentValue)) {
      options.unshift(currentValue);
    }

    options.forEach((name) => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      selectEl.appendChild(opt);
    });

    if (currentValue && options.includes(currentValue)) {
      selectEl.value = currentValue;
    }

    textEl.hidden = true;
    textEl.removeAttribute('name');
    selectEl.hidden = false;
    selectEl.setAttribute('name', 'RCLONE_REMOTE');

    setHint(`rclone.conf에서 ${remotes.length}개의 remote를 찾았습니다. 목록에서 선택하세요.`, 'ok');
  }

  function fetchRemotes(configPath) {
    refreshBtn.disabled = true;
    setHint('rclone.conf에서 remote 목록을 불러오는 중...', 'loading');

    return fetch('/api/media/books/0/apply-metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: 'general',
        source: pluginId,
        item_data: { action: 'list_remotes', config_path: configPath },
      }),
    })
      .then((res) => res.json())
      .then((data) => {
        if (!data || !data.success) {
          showTextMode(
            (data && (data.error || data.message)) || 'remote 목록을 불러오지 못했습니다. 직접 입력해주세요.',
            'error'
          );
          return;
        }
        let remotes = [];
        try {
          const parsed = JSON.parse(data.message || '{}');
          remotes = Array.isArray(parsed.remotes) ? parsed.remotes : [];
        } catch (e) {
          remotes = [];
        }

        const currentValue = (selectEl.hidden ? textEl.value : selectEl.value || textEl.value) || '';

        if (remotes.length === 0) {
          showTextMode(
            configPath
              ? 'rclone.conf에서 remote를 찾지 못했습니다. 경로를 확인하거나 직접 입력해주세요.'
              : 'CONFIG_PATH를 먼저 입력하고 저장(또는 새로고침)하면 remote 목록을 불러옵니다.',
            'empty'
          );
        } else {
          showSelectMode(remotes, currentValue);
        }
      })
      .catch((err) => {
        showTextMode(`remote 목록 조회 실패: ${err}`, 'error');
      })
      .finally(() => {
        refreshBtn.disabled = false;
      });
  }

  refreshBtn.addEventListener('click', () => {
    const path = (configPathInput && configPathInput.value) || (config && config.CONFIG_PATH) || '';
    fetchRemotes(path);
  });

  // CONFIG_PATH를 바꾸고 다른 곳을 클릭하면(저장 전이라도) 자동으로 다시 조회
  if (configPathInput) {
    configPathInput.addEventListener('change', () => {
      fetchRemotes(configPathInput.value);
    });
  }

  // 최초 진입 시: 저장되어 있던 CONFIG_PATH 기준으로 한 번 시도
  const initialConfigPath = (config && config.CONFIG_PATH) || (configPathInput && configPathInput.value) || '';
  if (initialConfigPath) {
    fetchRemotes(initialConfigPath);
  } else {
    showTextMode('CONFIG_PATH를 먼저 입력하고 저장(또는 새로고침)하면 remote 목록을 불러옵니다.', 'empty');
  }
})(window, pluginId, root, config);
