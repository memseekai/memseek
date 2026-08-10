(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var currentStep = 0;
  var selectedLayer = 'l0';
  var playTimer = null;
  var liveTimeline = [];
  var selectedLiveCollection = 'messages';

  var messages = [
    {
      role: 'alice',
      time: '09:00',
      text: 'New Node services use Fastify. Billing stays on Express until mobile drops the old response fields.'
    },
    {
      role: 'alice',
      time: '09:02',
      text: 'Never deploy billing changes without my explicit approval.'
    },
    {
      role: 'alice',
      time: '09:05',
      text: 'Before removing compatibility, show me telemetry proving the old fields have zero traffic.'
    }
  ];

  var layers = {
    l0: {
      threshold: 1,
      count: 3,
      label: 'L0 · EXACT MESSAGE',
      title: 'Keep the original words',
      text: 'The conversation is immutable. Later claims can always be checked against what Alice actually said.',
      note: 'Use it to verify wording, time, and source.',
      color: 'var(--am-l0)'
    },
    l1: {
      threshold: 2,
      count: 3,
      label: 'L1 · USEFUL MEMORY',
      title: 'Extract one claim at a time',
      text: 'Billing stays on Express. Deploys need explicit approval. Compatibility removal needs zero-traffic telemetry.',
      note: 'Use it to recall precise facts and rules.',
      color: 'var(--am-l1)'
    },
    l2: {
      threshold: 3,
      count: 1,
      label: 'L2 · WORKING SCENE',
      title: 'Maintain the billing context',
      text: 'One scene keeps the migration plan, mobile dependency, approval gate, and evidence requirement together.',
      note: 'Use it to restore a complete working context.',
      color: 'var(--am-l2)'
    },
    l3: {
      threshold: 4,
      count: 1,
      label: 'L3 · DURABLE PERSONA',
      title: 'Learn how Alice works',
      text: 'Alice expects explicit approval and observable evidence before risky production changes.',
      note: 'Use it to adapt consistently across projects.',
      color: 'var(--am-l3)'
    }
  };

  var steps = [
    { status: 'Ready', title: 'Start with a conversation', copy: 'No memory exists before the first message arrives.', layer: 'l0' },
    { status: 'L0 stored', title: 'Keep the exact messages', copy: 'Three source messages are stored without rewriting them.', layer: 'l0' },
    { status: 'L1 extracted', title: 'Pull out useful claims', copy: 'The worker creates small, cited facts and rules.', layer: 'l1' },
    { status: 'L2 updated', title: 'Build one working scene', copy: 'Related claims become a maintained billing context.', layer: 'l2' },
    { status: 'L3 ready', title: 'Distil a stable pattern', copy: 'The agent can now adapt to Alice without guessing.', layer: 'l3' }
  ];

  function byId(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char];
    });
  }

  function renderMessages() {
    if (currentStep < 1) {
      byId('demo-messages').innerHTML = '<p class="am-empty">Press play to add the conversation.</p>';
      return;
    }
    byId('demo-messages').innerHTML = messages.map(function (message, index) {
      return '<article class="am-message" style="animation-delay:' + (index * 70) + 'ms"><span>A</span><div><b>' +
        escapeHtml(message.role) + '<time>' + escapeHtml(message.time) + '</time></b><p>' +
        escapeHtml(message.text) + '</p></div></article>';
    }).join('');
  }

  function renderLayerDetail() {
    var layer = layers[selectedLayer];
    var reached = currentStep >= layer.threshold;
    var detail = byId('layer-detail');
    detail.style.setProperty('--detail-color', layer.color);
    if (!reached) {
      detail.innerHTML = '<span>' + escapeHtml(layer.label) + '</span><h3>Not built yet</h3><p>Move forward to let the worker create this layer.</p>';
      return;
    }
    detail.innerHTML = '<span>' + escapeHtml(layer.label) + '</span><h3>' + escapeHtml(layer.title) +
      '</h3><p>' + escapeHtml(layer.text) + '</p><small>' + escapeHtml(layer.note) + '</small>';
  }

  function renderStep(nextStep) {
    currentStep = Math.max(0, Math.min(steps.length - 1, nextStep));
    var step = steps[currentStep];
    selectedLayer = step.layer;
    byId('demo-status').textContent = step.status;
    byId('step-title').textContent = step.title;
    byId('step-copy').textContent = step.copy;

    Object.keys(layers).forEach(function (key) {
      var layer = layers[key];
      var reached = currentStep >= layer.threshold;
      var button = document.querySelector('[data-layer="' + key + '"]');
      button.classList.toggle('is-reached', reached);
      button.classList.toggle('is-selected', selectedLayer === key);
      byId('count-' + key).textContent = reached ? String(layer.count) : '0';
    });
    document.querySelectorAll('[data-step]').forEach(function (button) {
      button.classList.toggle('is-active', Number(button.dataset.step) === currentStep);
    });
    renderMessages();
    renderLayerDetail();
  }

  function setPlaying(playing) {
    var button = byId('demo-play');
    button.textContent = playing ? 'Ⅱ' : '▶';
    button.setAttribute('aria-pressed', String(playing));
  }

  function stopPlayback() {
    window.clearInterval(playTimer);
    playTimer = null;
    setPlaying(false);
  }

  function startPlayback() {
    if (playTimer) {
      stopPlayback();
      return;
    }
    if (currentStep >= steps.length - 1) renderStep(0);
    setPlaying(true);
    var advance = function () {
      if (currentStep >= steps.length - 1) {
        stopPlayback();
        return;
      }
      renderStep(currentStep + 1);
    };
    advance();
    playTimer = window.setInterval(advance, reduceMotion ? 700 : 1350);
  }

  function useMemory() {
    var answer = byId('recall-answer');
    answer.classList.add('is-ready');
    answer.querySelector('p').textContent = 'Do not deploy tonight. Keep billing compatible, prepare a rollback plan, and get Alice’s explicit approval first.';
    byId('recall-button').textContent = 'Memory applied ✓';
  }

  function setApiStatus(state, text) {
    var status = byId('api-status');
    status.classList.remove('is-connected', 'is-error');
    if (state) status.classList.add('is-' + state);
    status.innerHTML = '<i></i> ' + escapeHtml(text);
  }

  function renderLiveCollection(collection) {
    selectedLiveCollection = collection;
    document.querySelectorAll('[data-live-layer]').forEach(function (button) {
      button.classList.toggle('is-selected', button.dataset.liveLayer === collection);
    });
    var rows = liveTimeline.filter(function (record) { return record.collection === collection; });
    if (!rows.length) {
      byId('api-live-list').innerHTML = '<p class="am-live-empty">No ' + escapeHtml(collection) + ' yet.</p>';
      return;
    }
    byId('api-live-list').innerHTML = rows.slice(0, 10).map(function (record) {
      var stamp = record.occurred_at || record.created_at || '';
      var time = stamp ? new Date(stamp).toLocaleDateString([], { month: 'short', day: 'numeric' }) : '—';
      return '<article class="am-live-row"><span>' + escapeHtml(time) + '</span><p>' +
        escapeHtml(record.text || '(no text projection)') + '<small>' +
        escapeHtml((record.type || 'record') + ' · seq ' + (record.seq || '—')) + '</small></p></article>';
    }).join('');
  }

  async function fetchJson(url, apiKey) {
    var response = await window.fetch(url, {
      headers: apiKey ? { Authorization: 'Bearer ' + apiKey } : {},
      cache: 'no-store'
    });
    var body = null;
    try { body = await response.json(); } catch (error) { body = null; }
    if (!response.ok) {
      var detail = body && (body.detail || body.error && body.error.message);
      var apiError = new Error(detail || 'Memseek returned HTTP ' + response.status);
      apiError.status = response.status;
      throw apiError;
    }
    return body;
  }

  async function connectApi(event) {
    event.preventDefault();
    var message = byId('api-message');
    var button = byId('api-connect-button');
    var baseUrl;
    try {
      baseUrl = new URL(byId('api-url').value.trim());
    } catch (error) {
      message.textContent = 'Enter a complete API URL.';
      message.classList.add('is-error');
      setApiStatus('error', 'Invalid URL');
      return;
    }

    var apiKey = byId('api-key').value.trim();
    var runId = byId('api-run').value.trim();
    if (!apiKey || !/^[A-Za-z0-9_-]{1,32}$/.test(runId)) {
      message.textContent = 'Add the workspace key and the run ID used by the example.';
      message.classList.add('is-error');
      setApiStatus('error', 'Details required');
      return;
    }

    baseUrl.pathname = baseUrl.pathname.replace(/\/$/, '');
    baseUrl.search = '';
    baseUrl.hash = '';
    button.disabled = true;
    button.textContent = 'Connecting…';
    message.classList.remove('is-error');
    message.textContent = 'Loading agent.alice-' + runId + '…';

    try {
      var healthUrl = new URL(baseUrl.toString());
      healthUrl.pathname += '/health';
      var health = await fetchJson(healthUrl.toString(), '');
      if (!health || health.ok !== true) throw new Error('The API is not ready.');

      var timelineUrl = new URL(baseUrl.toString());
      timelineUrl.pathname += '/timeline';
      timelineUrl.searchParams.set('entity', 'agent.alice-' + runId);
      timelineUrl.searchParams.set('status', 'all');
      timelineUrl.searchParams.set('limit', '100');
      var payload = await fetchJson(timelineUrl.toString(), apiKey);
      liveTimeline = payload && payload.records || [];

      ['messages', 'memories', 'scenes', 'persona'].forEach(function (collection, index) {
        byId('live-l' + index).textContent = String(liveTimeline.filter(function (record) {
          return record.collection === collection;
        }).length);
      });
      byId('api-results').hidden = false;
      renderLiveCollection(selectedLiveCollection);
      message.textContent = liveTimeline.length
        ? 'Loaded ' + liveTimeline.length + ' records.'
        : 'Connected. No records match this run ID yet.';
      setApiStatus('connected', 'Connected');
    } catch (error) {
      byId('api-results').hidden = true;
      message.classList.add('is-error');
      if (error && error.status === 401) message.textContent = 'That workspace key was not accepted.';
      else if (error instanceof TypeError) message.textContent = 'Could not reach the API. Check the URL and CORS origin.';
      else message.textContent = error && error.message ? error.message : 'Connection failed.';
      setApiStatus('error', 'Connection failed');
    } finally {
      button.disabled = false;
      button.textContent = 'Connect';
    }
  }

  byId('hero-start').addEventListener('click', function () {
    byId('demo').scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
    window.setTimeout(startPlayback, reduceMotion ? 0 : 450);
  });
  byId('demo-play').addEventListener('click', startPlayback);
  byId('demo-back').addEventListener('click', function () { stopPlayback(); renderStep(currentStep - 1); });
  byId('demo-next').addEventListener('click', function () { stopPlayback(); renderStep(currentStep + 1); });
  document.querySelectorAll('[data-step]').forEach(function (button) {
    button.addEventListener('click', function () { stopPlayback(); renderStep(Number(button.dataset.step)); });
  });
  document.querySelectorAll('[data-layer]').forEach(function (button) {
    button.addEventListener('click', function () {
      selectedLayer = button.dataset.layer;
      document.querySelectorAll('[data-layer]').forEach(function (item) {
        item.classList.toggle('is-selected', item.dataset.layer === selectedLayer);
      });
      renderLayerDetail();
    });
  });
  byId('recall-button').addEventListener('click', useMemory);
  byId('api-connect-form').addEventListener('submit', connectApi);
  document.querySelectorAll('[data-live-layer]').forEach(function (button) {
    button.addEventListener('click', function () { renderLiveCollection(button.dataset.liveLayer); });
  });
  document.addEventListener('visibilitychange', function () { if (document.hidden) stopPlayback(); });

  renderStep(0);
})();
