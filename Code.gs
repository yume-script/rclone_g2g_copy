/**
 * rclone_g2g_copy 플러그인용 Google Apps Script 백엔드
 * ------------------------------------------------------
 * 구글 드라이브 폴더 A의 내용을 폴더 B로 서버사이드 복사합니다
 * (rclone의 대안 - 별도 서버/프로세스 없이 구글 인프라 안에서 실행됨).
 *
 * !! 중요 !!
 * 이 스크립트는 실제 Google Apps Script / Drive API 환경에서 직접 실행해
 * 검증하지 못했습니다 (이 대화에서는 그런 실행 환경 자체가 없음). rclone
 * 부분(logic.py)은 전부 실제 유사 시나리오로 유닛테스트를 거쳤지만, 이
 * 파일은 문서/공식 API 시그니처를 근거로 신중하게 작성한 "최선의 구현"입니다.
 * script.google.com에 붙여넣고 실행/배포하신 뒤, 작은 테스트 폴더로 먼저
 * 검증해보시고 문제가 있으면 알려주세요 - 바로 고쳐드리겠습니다.
 *
 * ===================== 배포 방법 =====================
 * 1. https://script.google.com 에서 새 프로젝트 생성
 * 2. 이 파일 내용을 Code.gs에 그대로 붙여넣기
 * 3. 상단 "서비스"(+) 에서 "Drive API"(Advanced Drive Service)를 추가할 필요는
 *    없습니다 - 기본 내장 DriveApp만 사용합니다.
 * 4. SHARED_SECRET 상수를 아무 임의의 긴 문자열로 바꿔주세요 (BookOasis
 *    설정 화면의 GAS_SHARED_SECRET에 동일한 값을 입력해야 함 - 이게 없으면
 *    누구나 이 웹앱 URL을 알면 내 드라이브 파일을 복사시킬 수 있습니다).
 * 5. 배포 > 새 배포 > 유형: 웹 앱
 *      - 실행 계정: 나
 *      - 액세스 권한이 있는 사용자: 아무나(익명 접근 필요 - BookOasis 서버가
 *        구글 계정으로 로그인할 방법이 없으므로). 대신 SHARED_SECRET으로
 *        보호합니다.
 * 6. 배포된 웹 앱 URL(.../exec 로 끝남)을 BookOasis 설정의
 *    GAS_WEBAPP_URL에 붙여넣기
 * 7. 최초 배포 시 "권한 검토" 절차에서 내 드라이브 접근 권한을 승인해야 합니다.
 * =======================================================
 */

// ↓↓↓ 반드시 아무 임의의 긴 문자열로 바꾸세요 (BookOasis 설정과 동일해야 함) ↓↓↓
const SHARED_SECRET = 'CHANGE_ME_TO_A_LONG_RANDOM_STRING';

// 한 번의 트리거 실행(최대 6분)에서 이 시간(ms)이 지나면 하던 일을 멈추고
// 다음 트리거로 넘긴다. 6분 한도에 안전 마진을 넉넉히 둔다.
const TIME_BUDGET_MS = 4.5 * 60 * 1000;

// 트리거가 다시 깨어나는 간격(분). 너무 촘촘하면 트리거 자체의 오버헤드가 커지고,
// 너무 길면 사용자 체감 진행이 느려 보인다.
const TRIGGER_INTERVAL_MINUTES = 1;

// 진행 로그는 job_state.json과 같은 정신으로 최근 N줄만 보존한다 (용량 제한 회피)
const MAX_LOG_LINES = 30;

const PROP = PropertiesService.getScriptProperties();

// ---------------------------------------------------------------------------
// 진입점
// ---------------------------------------------------------------------------

function doPost(e) {
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return jsonResponse({ success: false, error: 'INVALID_JSON_BODY' });
  }

  if (body.secret !== SHARED_SECRET) {
    return jsonResponse({ success: false, error: 'UNAUTHORIZED' });
  }

  try {
    if (body.action === 'start') {
      return jsonResponse(handleStart(body));
    }
    if (body.action === 'status') {
      return jsonResponse(handleStatus(body));
    }
    if (body.action === 'cancel') {
      return jsonResponse(handleCancel(body));
    }
    return jsonResponse({ success: false, error: 'UNKNOWN_ACTION: ' + body.action });
  } catch (err) {
    return jsonResponse({ success: false, error: 'EXCEPTION: ' + err });
  }
}

// GET도 동일하게 지원 (일부 환경에서 POST 바디 전달이 까다로울 수 있어 대비용).
// BookOasis 쪽(gas_logic.py)은 기본적으로 POST를 사용한다.
function doGet(e) {
  var body = e.parameter || {};
  if (body.secret !== SHARED_SECRET) {
    return jsonResponse({ success: false, error: 'UNAUTHORIZED' });
  }
  try {
    if (body.action === 'status') {
      return jsonResponse(handleStatus(body));
    }
    return jsonResponse({ success: false, error: 'GET는 status 액션만 지원합니다.' });
  } catch (err) {
    return jsonResponse({ success: false, error: 'EXCEPTION: ' + err });
  }
}

function jsonResponse(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON
  );
}

// ---------------------------------------------------------------------------
// action: start
// ---------------------------------------------------------------------------

function handleStart(body) {
  var sourceFolderId = body.source_folder_id;
  var destFolderId = body.dest_folder_id;

  if (!sourceFolderId || !destFolderId) {
    return { success: false, error: 'source_folder_id / dest_folder_id가 필요합니다.' };
  }

  // 폴더 존재/접근 가능 여부를 미리 확인해서, 시작하자마자 바로 에러를 알려준다.
  try {
    DriveApp.getFolderById(sourceFolderId);
    DriveApp.getFolderById(destFolderId);
  } catch (err) {
    return { success: false, error: '폴더를 찾을 수 없거나 접근 권한이 없습니다: ' + err };
  }

  // 동시에 하나만 - 이미 실행 중인 job이 있으면 거부 (rclone 쪽과 동일한 정책)
  var current = readJob();
  if (current && current.status === 'running') {
    return { success: false, error: '이미 실행 중인 복사 작업이 있습니다.' };
  }

  var jobId = Utilities.getUuid();
  var job = {
    job_id: jobId,
    status: 'running',
    cancel_requested: false,
    source_folder_id: sourceFolderId,
    dest_folder_id: destFolderId,
    // 아직 순회를 시작 안 한 상태 - 폴더 스택에 [소스, 목적지] 한 쌍만 넣어둔다.
    // 스택 기반 DFS로 순회해야 재개(체크포인트)가 쉽다 (재귀 호출은 트리거
    // 재시작 시 이어갈 수 없음).
    folder_stack: [{ src: sourceFolderId, dest: destFolderId }],
    files_done: 0,
    files_total: null, // 전체 개수는 모르므로 null (미리 전체 목록을 세지 않음 - 대용량 폴더에서 리스트업 자체가 오래 걸릴 수 있어 생략)
    log: [],
    started_at: new Date().getTime(),
    finished_at: null,
    error: null,
  };

  appendLog(job, '복사를 시작합니다. (source=' + sourceFolderId + ', dest=' + destFolderId + ')');
  writeJob(job);

  // 트리거 재개용 - continueJob이 주기적으로 실행되도록 등록
  ensureTrigger();

  // 첫 청크는 바로 지금 처리해서, 사용자가 곧바로 진행 상황을 볼 수 있게 한다.
  continueJob();

  return { success: true, job_id: jobId };
}

// ---------------------------------------------------------------------------
// action: status
// ---------------------------------------------------------------------------

function handleStatus(body) {
  var job = readJob();
  if (!job || job.job_id !== body.job_id) {
    return { success: false, error: '해당 job을 찾을 수 없습니다.' };
  }
  return {
    success: true,
    status: job.status,
    files_done: job.files_done,
    files_total: job.files_total,
    log_lines: job.log,
    error: job.error,
  };
}

// ---------------------------------------------------------------------------
// action: cancel
// ---------------------------------------------------------------------------

function handleCancel(body) {
  var job = readJob();
  if (!job || job.job_id !== body.job_id) {
    return { success: false, error: '해당 job을 찾을 수 없습니다.' };
  }
  if (job.status !== 'running') {
    return { success: true }; // 이미 끝난 job - 그냥 성공 처리
  }
  job.cancel_requested = true;
  appendLog(job, '중단이 요청되었습니다. 다음 체크포인트에서 멈춥니다.');
  writeJob(job);
  return { success: true };
}

// ---------------------------------------------------------------------------
// 실제 복사 작업 (시간기반 트리거가 주기적으로 호출)
// ---------------------------------------------------------------------------

function continueJob() {
  var job = readJob();
  if (!job || job.status !== 'running') {
    removeTrigger();
    return;
  }

  if (job.cancel_requested) {
    job.status = 'cancelled';
    job.finished_at = new Date().getTime();
    appendLog(job, '사용자 요청으로 복사가 중단되었습니다.');
    writeJob(job);
    removeTrigger();
    return;
  }

  var startTime = new Date().getTime();

  try {
    while (job.folder_stack.length > 0) {
      if (new Date().getTime() - startTime > TIME_BUDGET_MS) {
        // 시간 다 됨 - 지금까지 진행 상황을 저장하고 다음 트리거로 넘김
        writeJob(job);
        return;
      }

      var top = job.folder_stack[job.folder_stack.length - 1];
      var srcFolder = DriveApp.getFolderById(top.src);
      var destFolder = DriveApp.getFolderById(top.dest);

      // -- 파일 복사 (이 폴더 레벨의 파일들) --
      var files = srcFolder.getFiles();
      var processedAnyFile = false;
      while (files.hasNext()) {
        if (new Date().getTime() - startTime > TIME_BUDGET_MS) {
          writeJob(job);
          return;
        }
        var file = files.next();
        file.makeCopy(file.getName(), destFolder); // 서버사이드 복사 (다운로드/업로드 없음)
        job.files_done += 1;
        processedAnyFile = true;
      }
      if (processedAnyFile) {
        appendLog(job, top.src + ' 폴더의 파일 복사 완료 (누적 ' + job.files_done + '개)');
        writeJob(job); // 파일 몇 개 처리할 때마다는 아니고, 한 폴더 끝날 때마다 저장(과도한 쓰기 방지)
      }

      // -- 하위 폴더 순회 (DFS - 스택에 쌓기) --
      var subfolders = srcFolder.getFolders();
      var pushedAny = false;
      while (subfolders.hasNext()) {
        var subfolder = subfolders.next();
        var newDest = destFolder.createFolder(subfolder.getName());
        job.folder_stack.push({ src: subfolder.getId(), dest: newDest.getId() });
        pushedAny = true;
      }

      if (!pushedAny) {
        // 이 폴더는 완전히 끝남 - 스택에서 제거하고 다음(부모 레벨)으로
        job.folder_stack.pop();
      }
      // pushedAny가 true면 방금 push한 하위 폴더부터 다음 루프 반복에서 처리됨
    }

    // 스택이 비었으면 전체 완료
    job.status = 'success';
    job.finished_at = new Date().getTime();
    job.files_total = job.files_done;
    appendLog(job, '복사가 완료되었습니다. (총 ' + job.files_done + '개 파일)');
    writeJob(job);
    removeTrigger();
  } catch (err) {
    job.status = 'error';
    job.error = String(err);
    job.finished_at = new Date().getTime();
    appendLog(job, '오류 발생: ' + err);
    writeJob(job);
    removeTrigger();
  }
}

// ---------------------------------------------------------------------------
// job 상태 저장/조회
// ---------------------------------------------------------------------------
// PropertiesService는 값 하나당 9KB, 전체 500KB 제한이 있다. folder_stack이
// 매우 깊거나 log가 길면 넘칠 수 있으니 log는 MAX_LOG_LINES로 자르고,
// folder_stack은 "지금 처리 중인 경로"만 담기 때문에(전체 파일 목록이 아님)
// 보통은 충분히 작다.

function readJob() {
  var raw = PROP.getProperty('job');
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (err) {
    return null;
  }
}

function writeJob(job) {
  PROP.setProperty('job', JSON.stringify(job));
}

function appendLog(job, text) {
  job.log.push(text);
  if (job.log.length > MAX_LOG_LINES) {
    job.log = job.log.slice(job.log.length - MAX_LOG_LINES);
  }
}

// ---------------------------------------------------------------------------
// 트리거 관리
// ---------------------------------------------------------------------------

function ensureTrigger() {
  removeTrigger(); // 혹시 남아있던 이전 트리거 정리 (중복 실행 방지)
  ScriptApp.newTrigger('continueJob')
    .timeBased()
    .everyMinutes(TRIGGER_INTERVAL_MINUTES)
    .create();
}

function removeTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'continueJob') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
}
