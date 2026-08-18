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
      role: 'user',
      speaker: 'Alice',
      time: '09:00',
      text: 'New Node services use Fastify. Billing stays on Express until mobile drops the old response fields.'
    },
    {
      role: 'user',
      speaker: 'Alice',
      time: '09:02',
      text: 'Never deploy billing changes without my explicit approval. Writing the code is fine; shipping it is not.'
    },
    {
      role: 'user',
      speaker: 'Alice',
      time: '09:05',
      text: 'Before removing compatibility, show me telemetry proving the old fields have zero traffic.'
    }
  ];

  var layers = {
    l0: {
      threshold: 1,
      count: 3,
      label: 'L0 · SOURCE EVIDENCE',
      title: 'Capture what Alice actually said',
      text: 'Each message is stored unchanged, with its speaker, conversation, time, and order. This is evidence, not interpretation.',
      note: 'Use L0 to verify exact wording, time, and source.',
      color: 'var(--am-l0)',
      collection: 'messages',
      recordType: 'message',
      storage: 'event · append only',
      input: 'Alice’s three example messages',
      computation: 'No model rewrites the content. Memseek validates the fields, stores the exact message, and adds an embedding so it can be found later.',
      pipeline: ['validate', 'store exact text', 'embed for search'],
      records: [
        {
          label: 'message · ordinal 1',
          text: 'Never deploy billing changes without my explicit approval. Writing the code is fine; shipping it is not.',
          fields: 'role user · session platform-review · 09:02',
          citation: 'Source record · no citation needed'
        }
      ],
      result: '3 immutable message records in messages.'
    },
    l1: {
      threshold: 2,
      count: 4,
      label: 'L1 · ATOMIC MEMORY',
      title: 'Turn the conversation into separate claims',
      text: 'The worker extracts one reusable claim per record. It then searches existing memory and decides whether each claim should be stored, merged with an older claim, or skipped as a repeat.',
      note: 'Use L1 to recall one precise fact or rule without replaying the conversation.',
      color: 'var(--am-l1)',
      collection: 'memories',
      recordType: 'memory',
      storage: 'event · append only',
      input: 'New messages + any memories already stored',
      computation: 'Extract atomic claims, classify each one, search for overlap, then make an auditable store / merge / skip decision.',
      pipeline: ['extract claims', 'search existing', 'store · merge · skip'],
      records: [
        {
          label: 'persona · priority 80 · store',
          text: 'New Node services use Fastify.',
          fields: 'scene Billing migration',
          citation: 'cites message · 09:00'
        },
        {
          label: 'instruction · priority 100 · store',
          text: 'Billing changes require Alice’s explicit approval before production deployment.',
          fields: 'scene Billing migration',
          citation: 'cites message · 09:02'
        },
        {
          label: 'episodic · priority 85 · store',
          text: 'Billing stays on Express while mobile depends on the old response fields.',
          fields: 'scene Billing migration',
          citation: 'cites message · 09:00'
        },
        {
          label: 'instruction · priority 95 · store',
          text: 'Remove compatibility only after telemetry shows zero traffic.',
          fields: 'scene Billing migration',
          citation: 'cites message · 09:05'
        }
      ],
      result: '4 active atomic records in memories. A repeated rule would write nothing; a changed fact would supersede its earlier record.'
    },
    l2: {
      threshold: 3,
      count: 1,
      label: 'L2 · WORKING SCENE',
      title: 'Maintain one readable billing context',
      text: 'The worker combines related L1 claims with the current billing scene. It updates that one named document instead of producing another loose summary.',
      note: 'Use L2 to restore a complete working context for a project or situation.',
      color: 'var(--am-l2)',
      collection: 'scenes',
      recordType: 'scene_block',
      storage: 'keyed · current version + history',
      input: 'New and standing L1 claims + the current billing scene',
      computation: 'Choose update, create, merge, or retract. Then write one structured Markdown block and cite every L1 claim it keeps.',
      pipeline: ['group by context', 'update scene', 'retain citations'],
      records: [
        {
          label: 'key · billing-api-compatibility',
          text: '## Work Context\nBilling remains on Express while mobile uses legacy fields.\n\n## Decision Logic\nKeep compatibility until zero-traffic telemetry is available. Get Alice’s approval before production deployment.',
          fields: 'heat 1 · current head',
          citation: 'cites 4 memories'
        }
      ],
      result: '1 current Markdown document in scenes; later updates keep the same key and preserve earlier versions.'
    },
    l3: {
      threshold: 4,
      count: 1,
      label: 'L3 · DURABLE PERSONA',
      title: 'Keep only the pattern that should travel',
      text: 'The worker compares changed scenes with the existing profile. Project dates and billing details stay in L2; only a stable interaction pattern is promoted.',
      note: 'Use L3 to adapt consistently without mistaking a temporary project fact for personality.',
      color: 'var(--am-l3)',
      collection: 'persona',
      recordType: 'trait',
      storage: 'keyed · current version + history',
      input: 'Changed scene blocks + current scenes + current persona',
      computation: 'Look for a stable cross-context pattern and update only the named trait that changed. A scene update can legitimately produce no L3 record.',
      pipeline: ['compare scenes', 'filter temporary facts', 'update changed trait'],
      records: [
        {
          label: 'key · interaction_protocol',
          text: 'Treat writing code and shipping it as separate approvals; ask Alice before any production deployment.',
          fields: 'current head · one of five possible trait keys',
          citation: 'cites scene · billing-api-compatibility'
        }
      ],
      result: '1 illustrative trait in persona. A real run may update zero to five trait keys, depending on the evidence.'
    }
  };

  var steps = [
    { status: 'Ready', title: 'Start with a conversation', copy: 'No memory exists before the first message arrives.', layer: 'l0' },
    { status: 'L0 stored', title: 'Validate, store, and index the source', copy: 'Three messages land unchanged in the messages collection.', layer: 'l0' },
    { status: 'L1 extracted', title: 'Extract, search, and decide', copy: 'Four separate claims land in memories, each citing a source message.', layer: 'l1' },
    { status: 'L2 updated', title: 'Update one named context', copy: 'The claims become the current billing scene while its history remains readable.', layer: 'l2' },
    { status: 'L3 ready', title: 'Promote only the durable pattern', copy: 'One interaction trait changes; temporary billing facts stay in the scene.', layer: 'l3' }
  ];

  function byId(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char];
    });
  }

  function clipText(value, length) {
    var text = String(value || '');
    return text.length > length ? text.slice(0, length - 1).trimEnd() + '…' : text;
  }

  function renderMessages() {
    if (currentStep < 1) {
      byId('demo-messages').innerHTML = '<p class="am-empty">Press play to add the conversation.</p>';
      return;
    }
    byId('demo-messages').innerHTML = messages.map(function (message, index) {
      return '<article class="am-message" style="animation-delay:' + (index * 70) + 'ms"><span>A</span><div><b>' +
        escapeHtml(message.speaker || message.role) + '<time>' + escapeHtml(message.time) + '</time></b><p>' +
        escapeHtml(message.text) + '</p></div></article>';
    }).join('');
  }

  function renderLayerDetail() {
    var layer = layers[selectedLayer];
    var reached = currentStep >= layer.threshold;
    var detail = byId('layer-detail');
    detail.style.setProperty('--detail-color', layer.color);
    var pipeline = layer.pipeline.map(function (item, index) {
      return '<span>' + escapeHtml(item) + '</span>' + (index < layer.pipeline.length - 1 ? '<i aria-hidden="true">→</i>' : '');
    }).join('');
    var records = layer.records.map(function (record) {
      return '<article class="am-example-record"><div class="am-example-label">' + escapeHtml(record.label) +
        '</div><p>' + escapeHtml(record.text) + '</p><div class="am-example-meta"><span>' +
        escapeHtml(record.fields) + '</span><span>' + escapeHtml(record.citation) + '</span></div></article>';
    }).join('');
    detail.innerHTML = '<div class="am-detail-heading"><span>' + escapeHtml(layer.label) +
      '</span><span class="am-detail-state' + (reached ? ' is-ready' : '') + '">' +
      (reached ? 'built in this step' : 'example preview') + '</span></div><h3>' +
      escapeHtml(layer.title) + '</h3><p>' + escapeHtml(layer.text) +
      '</p><div class="am-compute"><div><small>INPUT</small><b>' + escapeHtml(layer.input) +
      '</b></div><div><small>WHAT RUNS</small><p>' + escapeHtml(layer.computation) +
      '</p><div class="am-pipeline">' + pipeline + '</div></div></div><section class="am-record-preview" aria-label="Example records in ' +
      escapeHtml(layer.collection) + '"><header><div><small>ENDS UP IN</small><b>' +
      escapeHtml(layer.collection) + '</b></div><span>' + escapeHtml(layer.storage) +
      '</span></header><div class="am-record-type">collection <b>' + escapeHtml(layer.collection) +
      '</b> · type <b>' + escapeHtml(layer.recordType) + '</b></div><div class="am-example-list">' +
      records + '</div><footer>' + escapeHtml(layer.result) + '</footer></section><small class="am-layer-use">' +
      escapeHtml(layer.note) + '</small>';
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
      button.setAttribute('aria-pressed', String(selectedLayer === key));
      byId('count-' + key).textContent = reached ? String(layer.count) : '0';
    });
    document.querySelectorAll('[data-step]').forEach(function (button) {
      var active = Number(button.dataset.step) === currentStep;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    renderMessages();
    renderLayerDetail();
  }

  function setPlaying(playing) {
    var button = byId('demo-play');
    button.textContent = playing ? 'Ⅱ' : '▶';
    button.setAttribute('aria-pressed', String(playing));
    button.setAttribute('aria-label', playing ? 'Pause memory build' : 'Play memory build');
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
    playTimer = window.setInterval(advance, reduceMotion ? 1800 : 2600);
  }

  function useMemory() {
    var answer = byId('recall-answer');
    answer.classList.add('is-ready');
    answer.querySelector('p').textContent = 'Do not rewrite or deploy billing tonight. Keep the old response fields until telemetry shows zero traffic, then get Alice’s explicit approval before production deployment.';
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
      var content = record.content && typeof record.content === 'object' ? record.content : {};
      var value = record.text || content.text || '(no text projection)';
      var meta = [record.type || 'record'];
      if (record.key) meta.push('key ' + record.key);
      if (collection === 'messages') {
        if (content.role) meta.push('role ' + content.role);
        if (content.session_id) meta.push('session ' + content.session_id);
        if (content.ordinal !== undefined) meta.push('ordinal ' + content.ordinal);
      } else if (collection === 'memories') {
        if (content.memory_kind) meta.push(content.memory_kind);
        if (content.priority !== undefined) meta.push('p' + content.priority);
        if (content.decision) meta.push(content.decision);
      } else if (collection === 'scenes' && content.heat !== undefined) {
        meta.push('heat ' + content.heat);
      }
      meta.push('seq ' + (record.seq || '—'));
      return '<article class="am-live-row"><span>' + escapeHtml(time) + '</span><p>' +
        escapeHtml(clipText(value, 320)) + '<small>' + escapeHtml(meta.join(' · ')) + '</small></p></article>';
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
        item.setAttribute('aria-pressed', String(item.dataset.layer === selectedLayer));
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
