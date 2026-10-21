'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { view: 'chat', status: null, messages: [], memories: [], documents: [], persona: {}, sources: [], messageSources: new Map(), busy: false, settingsLoaded: false, personaLoaded: false };
let toastTimer;
let editorSubmit;
let chatRequest = null;
const viewNames = { chat: '陪你聊天', memories: '共同记忆', knowledge: '生活知识库', persona: '人物设定', settings: '连接与设置' };
const kindNames = { fact: '生活片段', preference: '偏好', identity: '个人信息', event: '近期事件', promise: '共同约定', summary: '生活摘要' };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function showToast(message, error = false) {
  const node = $('#toast');
  node.textContent = message;
  node.className = `toast${error ? ' error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add('hidden'), error ? 8000 : 4500);
}

function errorMessage(error) {
  return error?.name === 'AbortError' ? '请求超时了，请检查本机服务或模型连接后重试。' : error?.message || '请求没有完成，请稍后重试。';
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeout || (path === '/api/chat' ? 150000 : 120000));
  try {
    const response = await fetch(path, {
      method: options.method || 'GET',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal,
      ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
    });
    const contentType = response.headers.get('content-type') || '';
    const result = contentType.includes('application/json') ? await response.json() : null;
    if (!response.ok) {
      const detail = result?.detail;
      throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `请求失败（${response.status}），请检查本机服务。`);
    }
    if (result === null) throw new Error('本机服务返回了无法识别的数据，请检查服务日志。');
    return result;
  } catch (error) {
    if (error instanceof TypeError) throw new Error('无法连接本机服务。请确认启动窗口仍在运行，然后刷新页面。');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function formatDate(value, options = {}) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit', ...options });
}

function formatTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
}

function emptyState(title, description, symbol = 'leaf') {
  const node = el('div', 'empty-state');
  node.append(icon(symbol), el('h3', '', title), el('p', '', description));
  return node;
}

function renderBudget(budget = {}) {
  const spend = Number(budget.estimated_spend || 0);
  const limit = Number(budget.monthly_limit ?? 50);
  const value = $('#budget-value');
  value.replaceChildren(document.createTextNode(`¥ ${spend.toFixed(2)} `), el('em', '', `/ ${limit}`));
  $('#budget-progress').style.width = `${Math.min(100, limit > 0 ? spend / limit * 100 : 0)}%`;
}

async function loadStatus() {
  const data = await api('/api/status');
  state.status = data;
  $('#top-mode').textContent = data.mode === 'live' ? '真实模型' : '本地演示';
  $('#top-mode').classList.toggle('live', data.mode === 'live');
  $('#demo-notice').classList.toggle('hidden', data.mode === 'live');
  renderBudget(data.budget);
  const warnings = Array.isArray(data.warnings) ? data.warnings.filter(Boolean) : [];
  $('#global-warning').textContent = warnings.join('；');
  $('#global-warning').classList.toggle('hidden', !warnings.length);
}

function createMessage(message, sources = []) {
  const user = message.role === 'user';
  const node = el('article', `message-row${user ? ' user' : ''}`);
  if (message.id !== undefined) node.dataset.messageId = String(message.id);
  const name = user ? (state.persona.user_name || '我') : (state.persona.name || '小雨');
  const avatar = el('div', 'message-avatar', user ? '我' : name.slice(0, 1));
  avatar.setAttribute('aria-hidden', 'true');
  const content = el('div', 'message-content');
  const meta = el('div', 'message-meta');
  meta.append(el('span', '', name));
  if (!user && message.mode === 'demo') meta.append(el('span', 'message-demo', '演示回复'));
  if (message.created_at) {
    const time = el('time', '', formatTime(message.created_at));
    time.dateTime = message.created_at;
    meta.append(time);
  }
  const bubble = el('div', 'bubble', message.content);
  content.append(meta, bubble);
  if (sources.length) {
    const details = el('details', 'message-sources');
    details.append(el('summary', '', `参考了 ${sources.length} 篇生活笔记`));
    for (const source of sources) details.append(createSource(source));
    content.append(details);
  }
  node.append(avatar, content);
  return node;
}

function createSource(source) {
  const node = el('div', 'source-item');
  node.append(el('strong', '', source.title || '生活笔记'), el('p', '', source.snippet || displayDocumentPath(source.path)));
  return node;
}

function displayDocumentPath(path) {
  return typeof path === 'string' && path.startsWith('manual://') ? '手动导入的笔记' : path || '手动导入的笔记';
}

function renderWelcome() {
  const fragment = document.createDocumentFragment();
  fragment.append(el('div', 'intro-stamp', 'A LITTLE SPACE, JUST FOR YOU'));
  const name = state.persona.name || '小雨';
  const welcome = createMessage({ role: 'assistant', content: `嗨，我是${name}。\n\n可以和我说说今天的小事，也可以先在「人物设定」里，告诉我你喜欢怎样的陪伴。我们从一句日常开始吧。`, mode: 'demo' });
  welcome.classList.add('welcome-message');
  fragment.append(welcome);
  const suggestions = el('div', 'suggestions');
  for (const value of ['今天有一点累，想聊聊', '记住：我喜欢不加糖的拿铁', '你记得我喜欢什么吗？']) {
    const button = el('button', '', value);
    button.type = 'button';
    button.addEventListener('click', () => {
      $('#message-input').value = value;
      $('#message-input').focus();
    });
    suggestions.append(button);
  }
  fragment.append(suggestions);
  return fragment;
}

function renderMessages() {
  const container = $('#messages');
  container.replaceChildren();
  const date = el('div', 'date-divider', new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }));
  container.append(date);
  if (!state.messages.length) container.append(renderWelcome());
  for (const message of state.messages) {
    if (message.role === 'user' || message.role === 'assistant') container.append(createMessage(message, message.sources || state.messageSources.get(String(message.id)) || []));
  }
  container.scrollTop = container.scrollHeight;
}

async function loadMessages() {
  const data = await api('/api/messages');
  state.messages = data.messages || [];
  renderMessages();
}

function renderSourcePreview(sources) {
  state.sources = sources;
  $('#source-count').textContent = sources.length;
  const preview = $('#source-preview');
  preview.replaceChildren();
  if (!sources.length) preview.append(el('p', 'subtle', '本轮没有检索到相关生活笔记。'));
  for (const source of sources.slice(0, 3)) preview.append(createSource(source));
}

function renderMemoryPreview() {
  $('#memory-count').textContent = state.memories.length;
  const node = $('#memory-preview');
  node.replaceChildren();
  const memories = [...state.memories].sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned)).slice(0, 3);
  if (!memories.length) node.append(el('p', 'subtle', '还没有记忆。聊聊你的生活吧。'));
  for (const memory of memories) {
    const item = el('div', 'preview-memory');
    const text = el('div', '', memory.content);
    text.append(el('span', 'preview-kind', kindNames[memory.kind] || '共同记忆'));
    item.append(text);
    node.append(item);
  }
}

async function sendChat(event) {
  event.preventDefault();
  if (state.busy) return;
  const input = $('#message-input');
  const message = input.value.trim();
  if (!message) return;
  if (message.length > 4000) return showToast('单条消息最多 4000 个字符，可以分几次慢慢说。', true);
  if (chatRequest?.message !== message) chatRequest = { message, request_id: globalThis.crypto?.randomUUID?.() || `chat-${Date.now()}-${Math.random().toString(36).slice(2)}` };
  state.busy = true;
  $('#send-button').disabled = true;
  $('#send-button span').textContent = '回应中';
  $('#chat-form').setAttribute('aria-busy', 'true');
  $('#chat-error').classList.add('hidden');
  input.value = '';
  renderMessages();
  const pendingMessage = createMessage({ role: 'user', content: message, created_at: new Date().toISOString() });
  const loading = createMessage({ role: 'assistant', content: '' });
  loading.setAttribute('aria-label', `${state.persona.name || '小雨'}正在回应`);
  const dots = el('div', 'typing-dots');
  dots.append(el('i'), el('i'), el('i'));
  $('.bubble', loading).replaceChildren(dots);
  $('#messages').append(pendingMessage, loading);
  $('#messages').scrollTop = $('#messages').scrollHeight;
  try {
    const data = await api('/api/chat', { method: 'POST', body: chatRequest });
    if (data.warning) showToast(String(data.warning), true);
    chatRequest = null;
    if (!data.reply?.content) throw new Error('服务没有返回回复内容，请查看本机服务日志。');
    state.messageSources.set(String(data.reply.id), data.sources || []);
    loading.replaceWith(createMessage(data.reply, data.sources || []));
    renderSourcePreview(data.sources || []);
    if (Array.isArray(data.memories)) {
      const byId = new Map(state.memories.map(memory => [memory.id, memory]));
      for (const memory of data.memories) byId.set(memory.id, memory);
      state.memories = [...byId.values()];
      renderMemoryPreview();
    }
    const updates = await Promise.allSettled([loadMessages(), loadMemories(), loadStatus()]);
    for (const update of updates) if (update.status === 'rejected') showToast(`回复已收到，但刷新失败：${errorMessage(update.reason)}`, true);
  } catch (error) {
    pendingMessage.remove();
    loading.remove();
    if (!input.value) input.value = message;
    $('#chat-error').textContent = errorMessage(error);
    $('#chat-error').classList.remove('hidden');
  } finally {
    state.busy = false;
    $('#send-button').disabled = false;
    $('#send-button span').textContent = '发送';
    $('#chat-form').removeAttribute('aria-busy');
    $('#messages').scrollTop = $('#messages').scrollHeight;
    input.focus();
  }
}

function actionButton(label, action, className = '') {
  const button = el('button', className, label);
  button.type = 'button';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await action(); } catch (error) { showToast(errorMessage(error), true); } finally { button.disabled = false; }
  });
  return button;
}

function renderMemories() {
  const query = $('#memory-search').value.trim().toLocaleLowerCase();
  const filtered = state.memories.filter(memory => `${memory.content} ${kindNames[memory.kind] || memory.kind}`.toLocaleLowerCase().includes(query));
  filtered.sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned));
  $('#memory-total').textContent = query ? `找到 ${filtered.length} 条` : `${state.memories.length} 条记忆`;
  const list = $('#memories-list');
  list.replaceChildren();
  if (!filtered.length) list.append(emptyState(query ? '还没找到这一段记忆' : '我们可以从一件小事开始', query ? '换个关键词试试。' : '聊聊你的偏好，或点击「添加记忆」写下想被记住的事。'));
  for (const memory of filtered) {
    const card = el('article', `memory-card${memory.pinned ? ' pinned' : ''}`);
    const heading = el('div', 'memory-card-heading');
    heading.append(el('span', 'kind-tag', kindNames[memory.kind] || memory.kind || '生活片段'));
    if (memory.pinned) heading.append(el('span', 'pin-label', '已置顶'));
    const footer = el('div', 'memory-card-footer');
    const time = el('time', '', `${formatDate(memory.updated_at || memory.created_at, { year: 'numeric' })} · ${memory.source === 'manual' ? '手动添加' : '聊天记忆'}`);
    const actions = el('div', 'card-actions');
    actions.append(
      actionButton(memory.pinned ? '取消置顶' : '置顶', async () => { await api(`/api/memories/${encodeURIComponent(memory.id)}`, { method: 'PATCH', body: { content: memory.content, pinned: !memory.pinned } }); await loadMemories(); }),
      actionButton('编辑', () => openMemoryEditor(memory)),
      actionButton('删除', async () => { if (!confirm(`确定删除这条记忆？\n\n${memory.content.slice(0, 160)}`)) return; await api(`/api/memories/${encodeURIComponent(memory.id)}`, { method: 'DELETE' }); await loadMemories(); showToast('这条记忆已删除。'); }, 'danger'),
    );
    footer.append(time, actions);
    card.append(heading, el('p', '', memory.content), footer);
    list.append(card);
  }
}

async function loadMemories() {
  const data = await api('/api/memories');
  state.memories = data.memories || [];
  renderMemoryPreview();
  renderMemories();
}

function field(label, name, { type = 'text', value = '', rows = 4, required = false, placeholder = '', maxLength, options } = {}) {
  const wrapper = el('label', '', label);
  const control = el(options ? 'select' : type === 'textarea' ? 'textarea' : 'input');
  control.name = name;
  if (!options && type !== 'textarea') control.type = type;
  if (type === 'textarea') control.rows = rows;
  if (options) for (const [key, title] of options) { const option = el('option', '', title); option.value = key; control.append(option); }
  control.value = value;
  control.required = required;
  control.placeholder = placeholder;
  if (maxLength) control.maxLength = maxLength;
  wrapper.append(control);
  return wrapper;
}

function openEditor(title, fields, submit, submitLabel = '保存') {
  $('#editor-title').textContent = title;
  $('#editor-fields').replaceChildren(...fields);
  $('#editor-error').classList.add('hidden');
  $('#save-editor').textContent = submitLabel;
  editorSubmit = submit;
  $('#editor-dialog').showModal();
}

function openMemoryEditor(memory) {
  const fields = [field('想被记住的事', 'content', { type: 'textarea', value: memory?.content || '', rows: 5, required: true, maxLength: 2000, placeholder: '例如：我喜欢不加糖的拿铁，通常上午喝。' })];
  if (!memory) fields.push(field('记忆类型', 'kind', { value: 'fact', options: [['fact', '生活片段'], ['preference', '个人偏好'], ['identity', '个人信息'], ['event', '近期事件'], ['promise', '共同约定'], ['summary', '生活摘要']] }));
  openEditor(memory ? '编辑这段记忆' : '添加一段记忆', fields, async values => {
    const content = values.get('content').trim();
    if (!content) throw new Error('请写下一点内容。');
    await api(memory ? `/api/memories/${encodeURIComponent(memory.id)}` : '/api/memories', { method: memory ? 'PATCH' : 'POST', body: memory ? { content } : { content, kind: values.get('kind') } });
    await loadMemories();
    showToast(memory ? '记忆已更新。' : '这件小事已记住。');
  });
}

function renderDocuments() {
  $('#document-total').textContent = `${state.documents.length} 篇笔记`;
  const list = $('#documents-list');
  list.replaceChildren();
  if (!state.documents.length) list.append(emptyState('给日常留一点位置', '粘贴一篇 Markdown 笔记，或连接你的 Obsidian 文件夹。', 'book'));
  for (const document of state.documents) {
    const row = el('article', 'document-row');
    const mark = el('div', 'document-icon');
    mark.append(icon('book'));
    const details = el('div', 'document-details');
    details.append(el('h3', '', document.title || '未命名笔记'), el('p', '', displayDocumentPath(document.path)));
    const time = el('time', '', formatDate(document.updated_at, { year: 'numeric' }));
    row.append(mark, details, time, actionButton('删除', async () => {
      if (!confirm(`从索引中删除「${document.title || '这篇笔记'}」？\n原始 Obsidian 文件会保留。`)) return;
      await api(`/api/knowledge/${encodeURIComponent(document.id)}`, { method: 'DELETE' });
      await loadKnowledge();
      showToast('这篇笔记已从索引中删除。');
    }, 'text-button'));
    list.append(row);
  }
}

async function loadKnowledge() {
  const data = await api('/api/knowledge');
  state.documents = data.documents || [];
  if (document.activeElement !== $('#vault-path')) $('#vault-path').value = data.vault_path || '';
  renderDocuments();
}

function openDocumentEditor() {
  openEditor('导入一篇生活笔记', [
    field('笔记标题', 'title', { required: true, maxLength: 200, placeholder: '例如：最近的我' }),
    field('Markdown 正文', 'content', { type: 'textarea', rows: 10, required: true, maxLength: 100000, placeholder: '# 我的生活\n\n写下喜好、经历，或最近发生的事…' }),
  ], async values => {
    const title = values.get('title').trim();
    const content = values.get('content').trim();
    if (!title || !content) throw new Error('请填写标题和正文。');
    await api('/api/knowledge/import', { method: 'POST', body: { title, content } });
    await loadKnowledge();
    showToast('笔记已导入，可以试着检索了。');
  }, '导入笔记');
}

function renderKnowledgeResults(data) {
  if (data.warning) showToast(String(data.warning), true);
  const target = $('#knowledge-results');
  target.replaceChildren();
  target.classList.remove('hidden');
  const heading = el('div', 'section-line');
  const results = data.results || [];
  heading.append(el('h3', '', `找到 ${results.length} 条相关片段`), actionButton('收起', () => target.classList.add('hidden'), 'text-button'));
  target.append(heading);
  const method = data.method === 'embedding' || data.method === 'vector' ? '模型向量检索' : data.method || '本地关键词检索';
  target.append(el('p', 'field-hint', `检索方式：${method}`));
  if (!results.length) target.append(el('p', 'subtle', '暂时没有匹配的笔记。可以换个词，或先导入几篇生活笔记。'));
  for (const result of results) {
    const card = el('article', 'result-card');
    card.append(el('strong', '', result.title || '生活笔记'), el('p', '', result.snippet || ''), el('small', '', displayDocumentPath(result.path)));
    target.append(card);
  }
}

function renderPersona() {
  const persona = state.persona;
  const name = persona.name || '小雨';
  $('#chat-title').textContent = name;
  $('label[for="message-input"]').textContent = `想和${name}说的话`;
  $('.companion-avatar').firstChild.textContent = name.slice(0, 1);
  $('.relationship-label').textContent = (persona.relationship || '日常陪伴').slice(0, 14);
  for (const key of ['name', 'user_name', 'personality', 'relationship', 'style', 'boundaries', 'user_profile']) $('#persona-form').elements[key].value = persona[key] || '';
}

async function loadPersona() {
  state.persona = await api('/api/persona');
  state.personaLoaded = true;
  renderPersona();
}

function renderSettings(data) {
  const form = $('#settings-form');
  for (const key of ['mode', 'provider', 'base_url', 'model', 'summary_model', 'embedding_model', 'monthly_budget']) if (form.elements[key]) form.elements[key].value = data[key] ?? '';
  form.elements.api_key.value = '';
  form.elements.embedding_enabled.checked = !!data.embedding_enabled;
  $('#key-status').textContent = data.api_key_configured ? '已在本机配置' : '未配置';
}

async function loadSettings() {
  const data = await api('/api/settings');
  state.settingsLoaded = true;
  renderSettings(data);
}

async function loadUsage() {
  const data = await api('/api/usage');
  $('#usage-spend').textContent = `¥ ${Number(data.estimated_spend || 0).toFixed(4)}`;
  $('#usage-detail').textContent = `${data.calls || 0} 次调用 · 输入 ${Number(data.input_tokens || 0).toLocaleString()} / 输出 ${Number(data.output_tokens || 0).toLocaleString()} token`;
  renderBudget(data);
}

async function navigate(view, updateHash = true) {
  if (!viewNames[view]) view = 'chat';
  state.view = view;
  for (const node of $$('.view')) node.classList.toggle('active', node.id === `view-${view}`);
  for (const node of $$('.nav-item')) { node.classList.toggle('active', node.dataset.view === view); node.setAttribute('aria-current', node.dataset.view === view ? 'page' : 'false'); }
  $('#page-label').textContent = viewNames[view];
  if (updateHash) history.replaceState(null, '', `#${view}`);
  if (view !== 'chat') window.scrollTo({ top: 0, behavior: 'instant' });
  try {
    if (view === 'memories') await loadMemories();
    if (view === 'knowledge') await loadKnowledge();
    if (view === 'persona' && !state.personaLoaded) await loadPersona();
    if (view === 'settings') {
      if (!state.settingsLoaded) await loadSettings();
      const updates = await Promise.allSettled([loadUsage()]);
      for (const update of updates) if (update.status === 'rejected') showToast(errorMessage(update.reason), true);
    }
  } catch (error) { showToast(errorMessage(error), true); }
}

async function submitWithButton(form, action) {
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  const previous = button.textContent;
  button.textContent = '保存中…';
  try { await action(); } catch (error) { showToast(errorMessage(error), true); } finally { button.disabled = false; button.textContent = previous; }
}

function setupEvents() {
  for (const button of $$('[data-view]')) button.addEventListener('click', () => navigate(button.dataset.view));
  $('.brand').addEventListener('click', event => { event.preventDefault(); navigate('chat'); });
  window.addEventListener('hashchange', () => navigate(location.hash.slice(1), false));
  $('#chat-form').addEventListener('submit', sendChat);
  $('#message-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('#chat-form').requestSubmit(); } });
  $('#reset-session').addEventListener('click', async () => {
    if (state.busy) return showToast('请等这次回复完成后再开启新会话。');
    if (!confirm('开启新会话会清空当前上下文摘要，保留聊天历史、长期记忆与生活笔记。继续吗？')) return;
    const button = $('#reset-session');
    button.disabled = true;
    try { await api('/api/session/reset', { method: 'POST', body: { delete_messages: false } }); renderSourcePreview([]); showToast('新会话已开启，历史与长期记忆都保留。'); } catch (error) { showToast(errorMessage(error), true); } finally { button.disabled = false; }
  });
  $('#add-memory').addEventListener('click', () => openMemoryEditor());
  $('#memory-search').addEventListener('input', renderMemories);
  $('#add-document').addEventListener('click', openDocumentEditor);
  $('#close-editor').addEventListener('click', () => $('#editor-dialog').close());
  $('#cancel-editor').addEventListener('click', () => $('#editor-dialog').close());
  $('#editor-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('#save-editor');
    if (button.disabled) return;
    button.disabled = true;
    $('#editor-error').classList.add('hidden');
    try { await editorSubmit(new FormData(event.target)); $('#editor-dialog').close(); } catch (error) { $('#editor-error').textContent = errorMessage(error); $('#editor-error').classList.remove('hidden'); } finally { button.disabled = false; }
  });
  $('#editor-dialog').addEventListener('cancel', event => { if ($('#save-editor').disabled) event.preventDefault(); });
  $('#vault-form').addEventListener('submit', async event => {
    event.preventDefault();
    const path = $('#vault-path').value.trim();
    if (!path) return showToast('请填写 Obsidian 笔记库的完整路径。', true);
    const button = $('#sync-vault');
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = '同步中…';
    try { const data = await api('/api/knowledge/sync', { method: 'POST', body: { vault_path: path } }); await loadKnowledge(); showToast(`同步完成：索引 ${data.indexed || 0} 篇，移除 ${data.deleted || 0} 篇，跳过 ${data.skipped || 0} 篇。`); } catch (error) { showToast(errorMessage(error), true); } finally { button.disabled = false; button.replaceChildren(icon('refresh'), document.createTextNode('同步笔记')); }
  });
  $('#knowledge-search-form').addEventListener('submit', async event => {
    event.preventDefault();
    const query = $('#knowledge-query').value.trim();
    if (!query) return showToast('先写下想寻找的事情。');
    const button = $('button', event.target);
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = '检索中';
    try { renderKnowledgeResults(await api('/api/knowledge/search', { method: 'POST', body: { query } })); } catch (error) { showToast(errorMessage(error), true); } finally { button.disabled = false; button.textContent = '检索'; }
  });
  $('#seed-demo').addEventListener('click', async () => {
    const button = $('#seed-demo');
    button.disabled = true;
    try { await api('/api/demo/seed', { method: 'POST', body: {} }); await loadKnowledge(); showToast('虚构示例笔记已准备好。它们不代表你的真实生活。'); } catch (error) { showToast(errorMessage(error), true); } finally { button.disabled = false; }
  });
  $('#persona-form').addEventListener('submit', async event => {
    event.preventDefault();
    await submitWithButton(event.target, async () => {
      const values = Object.fromEntries(new FormData(event.target));
      if (!values.name.trim()) throw new Error('请给人物起一个名字。');
      await api('/api/persona', { method: 'PUT', body: values });
      await loadPersona();
      renderMessages();
      showToast('人物设定已保存，下一句就会用上。');
    });
  });
  $('#settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    await submitWithButton(event.target, async () => {
      const form = event.target;
      const values = Object.fromEntries(new FormData(form));
      values.monthly_budget = Number(values.monthly_budget);
      values.embedding_enabled = form.elements.embedding_enabled.checked;
      if (!values.api_key.trim()) delete values.api_key;
      await api('/api/settings', { method: 'PUT', body: values });
      form.elements.api_key.value = '';
      await loadSettings();
      await loadStatus();
      showToast('连接设置已保存。');
    });
  });

}

function refineSettingsFields() {
  const form = $('#settings-form');
  form.elements.provider.setAttribute('list', 'provider-options');
  form.elements.base_url.placeholder = '从模型平台控制台复制兼容 OpenAI 的 API 地址';

}

async function initialize() {
  refineSettingsFields();
  setupEvents();
  const requestedView = location.hash.slice(1);
  const results = await Promise.allSettled([loadStatus(), loadPersona(), loadMessages(), loadMemories(), loadKnowledge(), loadSettings()]);
  const failures = results.filter(result => result.status === 'rejected');
  if (failures.length) {
    showToast(errorMessage(failures[0].reason), true);
    if (results[2].status === 'rejected') $('#messages').replaceChildren(emptyState('暂时连不上本机服务', '确认启动窗口仍在运行，然后刷新页面。', 'chat'));
    if (results[0].status === 'rejected') $('#top-mode').textContent = '服务未连接';
  } else {
    renderMessages();
  }
  await navigate(viewNames[requestedView] ? requestedView : 'chat', false);
}

initialize().catch(error => showToast(errorMessage(error), true));
