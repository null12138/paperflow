(() => {
  const progress = message => {
    const match = String(message || '').match(/\[(\d+)\s*\/\s*(\d+)\]/);
    if (!match || Number(match[2]) <= 0) return null;
    return Math.max(0, Math.min(100, Number(match[1]) / Number(match[2]) * 100));
  };
  const paintProgress = (row, job) => {
    const fill = row.querySelector('.task-progress-fill');
    if (!fill) return;
    fill.className = `task-progress-fill ${job.status}`;
    const value = progress(job.message);
    if (value !== null && job.status === 'running') {
      fill.style.width = `${value.toFixed(2)}%`;
      fill.style.animation = 'none';
    } else {
      fill.style.removeProperty('width'); fill.style.removeProperty('animation');
    }
  };
  const downloadForm = document.getElementById('download-form');
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const lines = value => value.split(/\r?\n|,/).map(item => item.trim()).filter(Boolean);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  if (!downloadForm) return;
  const input = document.getElementById('download-input');
  const tabs = document.querySelectorAll('.mode-tab');
  const status = document.getElementById('download-status');
  let mode = 'keyword';
  const setStatus = (message, state = 'running', link = '', element = status) => {
    element.className = `job-status is-visible is-${state}`;
    element.textContent = message;
    if (link) { const a = document.createElement('a'); a.href = link; a.textContent = '查看任务 →'; element.append(a); }
  };
  const toast = (message, title = '任务已创建') => {
    const box = document.getElementById('toast'); box.querySelector('strong').textContent = title;
    document.getElementById('toast-message').textContent = message; box.classList.add('is-visible');
    window.setTimeout(() => box.classList.remove('is-visible'), 4500);
  };
  const bindInput = (field, count, file) => {
    const update = () => { document.querySelector(count).textContent = `${lines(document.querySelector(field).value).length} 条内容`; };
    document.querySelector(field).addEventListener('input', update);
    document.querySelector(file).addEventListener('change', async event => { const selected = event.target.files[0]; if (!selected) return; document.querySelector(field).value = await selected.text(); update(); });
  };
  tabs.forEach(tab => tab.addEventListener('click', () => {
    mode = tab.dataset.mode; tabs.forEach(item => { const active = item === tab; item.classList.toggle('is-active', active); item.setAttribute('aria-selected', String(active)); });
    const doi = mode === 'doi'; document.getElementById('download-label').textContent = doi ? 'DOI 清单' : '关键词清单';
    document.getElementById('download-input').placeholder = doi ? '例如：\n10.1038/s41586-021-03819-2\n10.1126/science.1116681' : '例如：\nPanthera tigris\nGinkgo biloba';
    document.getElementById('download-button-label').textContent = doi ? '开始下载 PDF' : '开始检索并下载';
    document.getElementById('process-step-1').textContent = doi ? '读取 DOI' : '检索文献';
    document.getElementById('process-step-2').textContent = doi ? '匹配 DOI' : '整理文献信息';
  }));
  async function watchJob(created, statusElement) {
    let job = created;
    while (!['succeeded', 'failed', 'cancelled'].includes(job.status)) {
      await sleep(2200);
      try {
        const poll = await fetch(`/api/jobs/${created.id}`);
        if (!poll.ok) continue;
        job = await poll.json();
      } catch (_) { continue; }
    }
    if (job.status === 'succeeded') {
      setStatus(job.message || `任务 #${created.id} 已完成`, 'complete', `/jobs/${created.id}`, statusElement);
      toast(job.message || `任务 #${created.id} 已完成`, '任务已完成');
    } else setStatus(job.message || `任务 #${created.id} 未完成`, 'error', `/jobs/${created.id}`, statusElement);
  }
  async function submit(form, workflow, field, errorId, statusElement, button) {
    const items = lines(document.querySelector(field).value); const error = document.getElementById(errorId);
    if (!items.length) { error.textContent = '请粘贴或上传内容。'; return; }
    button = button || form.querySelector('button[type="submit"]');
    error.textContent = ''; button.disabled = true; statusElement.className = 'job-status is-visible is-running'; statusElement.textContent = '正在创建任务…';
    try {
      const limitField = form.querySelector('input[type="number"]');
      const limit = limitField ? Number(limitField.value || 0) : 0;
      const response = await fetch('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify({workflow, mode: workflow === 'metadata' ? 'keyword' : mode, items, limit})});
      const created = await response.json(); if (!response.ok) throw new Error(created.error || '创建任务失败');
      setStatus(`任务 #${created.id} 已创建，将在后台持续运行。`, 'running', `/jobs/${created.id}`, statusElement);
      toast(`任务 #${created.id} 已进入后台，您可以继续创建任务或关闭页面。`);
      watchJob(created, statusElement);
    } catch (exception) { setStatus(exception.message, 'error', '', statusElement); toast(exception.message, '任务未能启动'); }
    finally { button.disabled = false; }
  }
  downloadForm.addEventListener('submit', event => { event.preventDefault(); submit(downloadForm, 'download', '#download-input', 'download-error', status, event.submitter); });
  document.getElementById('metadata-form').addEventListener('submit', event => { event.preventDefault(); submit(event.currentTarget, 'metadata', '#metadata-input', 'metadata-error', document.getElementById('metadata-status'), event.submitter); });
  document.querySelector('#toast button').addEventListener('click', () => document.getElementById('toast').classList.remove('is-visible'));
  bindInput('#download-input', '#download-count', '#download-file'); bindInput('#metadata-input', '#metadata-count', '#metadata-file');
  const labels = {succeeded: '完成', running: '运行中', queued: '排队中', failed: '失败', cancelled: '已取消'};
  const jobNames = {search: '检索文献', wos_fetch: '导入 WOS', download_db: '补全 PDF', download_list: 'DOI 下载', preflight: '整理开放全文', impact_factor: '导入影响因子', export_report: '生成报告', reconcile: '核对文件', dedupe: '清理重复', full_run: '检索并下载', doctor: '服务检查'};
  const taskList = document.getElementById('recent-tasks');
  const makeTaskRow = job => {
    const row = document.createElement('a'); row.className = 'task-row'; row.href = `/jobs/${job.id}`; row.dataset.jobId = job.id;
    const id = document.createElement('span'); id.className = 'task-id'; id.textContent = `#${job.id}`;
    const main = document.createElement('span'); main.className = 'task-main';
    const title = document.createElement('strong'); title.textContent = jobNames[job.kind] || job.kind;
    const message = document.createElement('small'); message.className = 'task-message'; message.textContent = job.message || '等待执行';
    const bar = document.createElement('span'); bar.className = 'task-progress'; const fill = document.createElement('i'); bar.append(fill);
    main.append(title, message, bar);
    const state = document.createElement('span'); state.className = `task-state ${job.status}`; state.textContent = labels[job.status] || job.status;
    row.append(id, main, state);
    if (job.archive_ready || job.txt_ready) {
      const archive = document.createElement('a'); archive.className = 'button button-small primary task-archive';
      archive.href = job.txt_ready ? job.txt_url : `/downloads/${job.id}/archive`; archive.textContent = job.txt_ready ? '下载 TXT' : '下载 ZIP'; archive.addEventListener('click', event => event.stopPropagation());
      row.append(archive);
    }
    paintProgress(row, job); return row;
  };
  async function refreshRecentTasks() {
    try {
      const response = await fetch('/api/jobs'); if (!response.ok) throw new Error();
      const jobs = (await response.json()).slice(0, 8); taskList.replaceChildren();
      if (!jobs.length) { const empty = document.createElement('div'); empty.className = 'task-empty'; empty.textContent = '还没有任务。提交一个关键词或 DOI 清单后，进度会显示在这里。'; taskList.append(empty); return; }
      jobs.forEach(job => taskList.append(makeTaskRow(job)));
      if (jobs.some(job => !['succeeded', 'failed', 'cancelled'].includes(job.status))) window.setTimeout(refreshRecentTasks, 2500);
    } catch (_) { const empty = document.createElement('div'); empty.className = 'task-empty'; empty.textContent = '任务仍在后台运行，稍后将自动刷新。'; taskList.replaceChildren(empty); window.setTimeout(refreshRecentTasks, 5000); }
  }
  refreshRecentTasks();
})();

(() => {
  if (document.getElementById('download-form')) return;
  const labels = {succeeded: '完成', running: '运行中', queued: '排队中', failed: '失败', cancelled: '已取消'};
  const progress = message => {
    const match = String(message || '').match(/\[(\d+)\s*\/\s*(\d+)\]/);
    return match && Number(match[2]) > 0 ? Math.max(0, Math.min(100, Number(match[1]) / Number(match[2]) * 100)) : null;
  };
  const paintProgress = (row, job) => {
    const fill = row.querySelector('.task-progress-fill'); if (!fill) return;
    fill.className = `task-progress-fill ${job.status}`;
    const value = progress(job.message);
    if (value !== null && job.status === 'running') { fill.style.width = `${value.toFixed(2)}%`; fill.style.animation = 'none'; }
    else { fill.style.removeProperty('width'); fill.style.removeProperty('animation'); }
  };
  async function refreshTaskList() {
    let active = false;
    try { const response = await fetch('/api/jobs'); if (!response.ok) throw new Error(); const jobs = await response.json(); const byId = new Map(jobs.map(job => [String(job.id), job]));
      for (const row of document.querySelectorAll('.task-row')) { const job = byId.get(row.dataset.jobId); if (!job) continue;
        row.querySelector('.task-message').textContent = `${job.message || '等待执行'} · ${job.created_at || ''}`;
        const state = row.querySelector('.task-state'); state.textContent = labels[job.status] || job.status; state.className = `task-state ${job.status}`;
        let archive = row.querySelector('.task-archive');
        if ((job.archive_ready || job.txt_ready) && !archive && !row.querySelector('.task-txt')) {
          archive = document.createElement('a'); archive.className = 'button button-small primary task-archive';
          archive.href = job.txt_ready ? job.txt_url : `/downloads/${job.id}/archive`; archive.textContent = job.txt_ready ? '下载 TXT' : '下载 ZIP';
          archive.addEventListener('click', event => event.stopPropagation());
          (row.querySelector('.task-actions') || row).append(archive);
        } else if (!job.archive_ready && !job.txt_ready && archive) archive.remove();
        paintProgress(row, job);
        if (!['succeeded','failed','cancelled'].includes(job.status)) active = true;
      }
    } catch (_) { active = true; }
    if (active) window.setTimeout(refreshTaskList, 2200);
  }
  if (document.querySelector('.task-row')) refreshTaskList();
})();

(() => {
  const view = document.getElementById('job-view'); if (!view) return;
  const status = document.getElementById('job-status'), message = document.getElementById('job-message'), log = document.getElementById('job-log');
  const labels = {succeeded:'完成',running:'运行中',queued:'排队中',failed:'失败',cancelled:'已取消'};
  async function refresh() { try { const response = await fetch(view.dataset.apiUrl); if (!response.ok) return; const job = await response.json(); status.textContent = labels[job.status] || job.status; status.className = `badge large ${job.status}`; message.textContent = job.message || ''; log.textContent = job.log || '任务开始后会显示处理进度。'; log.scrollTop = log.scrollHeight; if (!['succeeded','failed','cancelled'].includes(job.status)) window.setTimeout(refresh, 2500); else window.location.reload(); } catch (_) { window.setTimeout(refresh, 5000); } }
  if (!['succeeded','failed','cancelled'].includes(status.textContent.trim())) window.setTimeout(refresh, 1000);
})();

(() => { const button = document.getElementById('open-all-publishers'); if (!button) return; button.addEventListener('click', () => document.querySelectorAll('.publisher-link').forEach((link, index) => window.setTimeout(() => window.open(link.href, '_blank', 'noopener,noreferrer'), index * 120))); })();
