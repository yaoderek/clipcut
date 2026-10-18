/* ClipCut pipeline canvas. Plain JS, no build step.
   Add a step from the left palette, the + Add step menu, or a right-click.
   Try approaches fans out real cutter recipes; Render branches queues cutter v2. */
(function () {
  const $ = (s) => document.querySelector(s);
  const IDEA = "try a fast punchy cut, a calm storytelling cut, and one with big captions for silent viewing";
  const LS = "clipcut.canvas.pipeline.v1";
  const TOUR_KEY = "clipcut.canvas.tour.v1";
  const PLAIN = { video: "a source video", transcript: "a transcript", moments: "hook moments", clips: "short clips", approaches: "edit approaches", scores: "persona scores", tests: "a channel test", winner: "a picked winner" };
  const CHANNELS = [
    ["tiktok", "TikTok"], ["reels", "Reels"], ["shorts", "Shorts"], ["x", "X"], ["linkedin", "LinkedIn"],
    ["youtube", "YouTube"], ["ig_feed", "IG_Feed"], ["ig_stories", "IG_Stories"], ["fb_reels", "FB_Reels"], ["fb_feed", "FB_Feed"]
  ];

  const TYPES = {
    ingest: { label: "Ingest", desc: "Raw long video in.", color: "#22d3ee", order: 0, emits: "video", accepts: [],
      tip: "Where the long video comes from. Replay a saved run, pick a sample, or upload a file. Branch renders need the actual mp4.",
      idle: "Choose a saved run, a sample, or a file.",
      defaults: { source: "run", run: "v2_niosh_multi", sample: "", n: 12, fileName: "" } },
    transcribe: { label: "Transcribe", desc: "Words with timestamps.", color: "#38bdf8", order: 1, emits: "transcript", accepts: ["video"],
      tip: "Cutter v2 uses faster-whisper base, or a human SRT when the sample has one. This step shows the transcript the run used. It does not re-transcribe by itself.",
      idle: "Connect a source video.", defaults: { engine: "auto" } },
    branch: { label: "Try approaches", desc: "Fan out several real cuts from one sentence.", color: "#facc15", order: 1.5, emits: "approaches", accepts: ["video", "transcript"],
      tip: "Type an edit idea. ClipCut splits it into branches (Grok if a key is set, otherwise keyword templates) and builds a small pipeline for each. Render branches runs cutter v2, one job at a time.",
      idle: "Describe a few cuts, then build branches.",
      defaults: { idea: IDEA } },
    hook_pick: { label: "Hook pick", desc: "Which opening line the cut starts on.", color: "#a78bfa", order: 2, emits: "moments", accepts: ["transcript", "approaches"],
      tip: "strongest uses the best-scored sentence, second the runner-up, earliest the first sentence. Cutter v2 already ranks hooks this way.",
      idle: "Connect a transcript or a Try approaches step.",
      defaults: { n: 5, minLen: 15, maxLen: 45, hookStrategy: "strongest" } },
    clip: { label: "Clip / length", desc: "Target length and pacing.", color: "#7c5cff", order: 3, emits: "clips", accepts: ["moments", "clips"],
      tip: "15, 30, or 45 seconds. tight punch is jump cuts plus zoom; raw punch keeps the pauses. A new render uses these settings. Filtering a saved run does not re-cut.",
      idle: "Connect a hook.", defaults: { lengths: [15, 30, 45], pacing: "any", n: 12, ending: "loop" } },
    subtitle: { label: "Subtitle style", desc: "Burned-in captions.", color: "#f472b6", order: 4, emits: "clips", accepts: ["clips"],
      tip: "word pop is big on-screen text (readable on mute). sentence is a lower-third line. Hook title is a 3 second overlay.",
      idle: "Connect a clip.", defaults: { style: "any", hookTitle: "any" } },
    crop: { label: "Crop / format", desc: "Aspect ratio and frame.", color: "#fb923c", order: 5, emits: "clips", accepts: ["clips"],
      tip: "9:16 for TikTok, Reels, and Shorts. 1:1 for X. 4:5 for LinkedIn. 16:9 for YouTube. The cutter face-crops when it renders.",
      idle: "Connect a clip.", defaults: { aspects: ["9:16", "1:1", "4:5", "16:9"], channel: "" } },
    persona_score: { label: "Persona score", desc: "Four viewers, one aggregate.", color: "#facc15", order: 6, emits: "scores", accepts: ["clips", "scores"],
      tip: "Calls POST /api/persona/score. Aggregate is a 0–1 audience mix, a different scale from the legacy channel score. If the route is missing, a simulated stub is shown.",
      idle: "Connect a clip.", defaults: { mode: "auto" } },
    hook_rewrite: { label: "Hook rewrite", desc: "Three opening lines per persona.", color: "#e879f9", order: 6.5, emits: "scores", accepts: ["scores", "clips"],
      tip: "Calls POST /api/persona/hooks. Source heuristic means template lines. Source llm means Grok and needs XAI_API_KEY or GROK_API_KEY. predicted_delta is a heuristic estimate of the persona-score change, not a measured lift. If the route is missing, a simulated stub is shown.",
      idle: "Connect a persona score or a clip.", defaults: { mode: "auto", channels: [] } },
    channel_test: { label: "Channel test", desc: "Simulated A/B by platform.", color: "#34d399", order: 7, emits: "tests", accepts: ["scores", "clips"],
      tip: "GET /api/channels/{run} is simulated traffic, not live posts. Persona channel fit is shown separately and is not the same number.",
      idle: "Connect persona scores.", defaults: { channels: ["tiktok", "reels", "shorts", "x", "linkedin"] } },
    pick_winner: { label: "Pick winner", desc: "Choose which rule ships.", color: "#fbbf24", order: 8, emits: "winner", accepts: ["scores", "tests", "winner"],
      tip: "Persona winner uses aggregate. Channel winner is the simulated A/B pick. Legacy winner is the saved run's caption tie-break. These are different rules. Don't compare the numbers as one scale.",
      idle: "Connect scores or a channel test.", defaults: { prefer: "persona", channel: "reels" } },
    publish: { label: "Publish", desc: "Copy the winner file locally.", color: "#c084fc", order: 9, emits: null, accepts: ["winner", "scores", "tests"],
      tip: "POST /api/deploy copies the run's legacy winner mp4. It does not post to TikTok, Reels, Shorts, X, or LinkedIn. It does not re-rank with the persona aggregate.",
      idle: "Connect a winner.", defaults: { action: "local" } }
  };
  const TYPE_ORDER = ["branch", "ingest", "transcribe", "hook_pick", "clip", "subtitle", "crop", "persona_score", "hook_rewrite", "channel_test", "pick_winner", "publish"];

  let pipeline = null, selectedId = null, selectedEdge = null, T = { x: 40, y: 30, k: 0.9 };
  let catalog = { runs: [], samples: [], channels: [], pipelines: [] };
  const pendingFiles = {};
  let saveTimer = null, runToken = 0, running = false, connectState = null, menuAt = null;

  const vp = $("#vp"), world = $("#world"), svg = $("#wires"), insp = $("#insp");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }
  function uid(prefix) { return prefix + "_" + Math.random().toString(36).slice(2, 7); }
  function nodeById(id) { return pipeline.nodes.find((n) => n.id === id); }
  function fmtPersona(x) { const n = Number(x); return Number.isFinite(n) ? n.toFixed(4) : "–"; }
  function sourceChip(source) {
    const s = String(source || "unspecified");
    if (s === "stub") return { cls: "sim", text: "Simulated stub" };
    if (s === "heuristic") return { cls: "sim", text: "Heuristic · not trained" };
    if (s === "llm") return { cls: "sim", text: "LLM-simulated" };
    if (s === "template") return { cls: "sim", text: "Template · not a model" };
    if (s === "mixed") return { cls: "sim", text: "Mixed · not trained" };
    if (s === "model") return { cls: "model", text: "Model" };
    return { cls: "sim", text: s };
  }
  function chipHTML(source) { const c = sourceChip(source); return `<span class="chip ${c.cls}">${esc(c.text)}</span>`; }
  function hookSourceChip(source) {
    const s = String(source || "");
    if (s === "stub") return { cls: "sim", text: "Simulated stub" };
    if (s === "llm") return { cls: "sim", text: "LLM · Grok" };
    if (s === "heuristic" || s === "template") return { cls: "sim", text: "Template" };
    if (s === "mixed") return { cls: "sim", text: "Mixed · LLM + template" };
    return { cls: "sim", text: s || "unspecified" };
  }
  function hookChipHTML(source) { const c = hookSourceChip(source); return `<span class="chip ${c.cls}">${esc(c.text)}</span>`; }
  function estimateCell(s) {
    const n = Number(s && s.predicted_delta);
    if (!Number.isFinite(n)) return "—";
    const sign = n > 0 ? "+" : "";
    const kind = s.predicted_delta_label === "heuristic_estimate"
      ? "heuristic estimate, not a measured lift"
      : "placeholder estimate, not a persona rescore";
    return `${sign}${n.toFixed(4)}<div class="mut">${esc(kind)}</div>`;
  }

  function builtin() {
    const col = [
      ["n_ingest", "ingest", 36, 24, { source: "run", run: "v2_niosh_multi", sample: "", n: 12, fileName: "" }],
      ["n_transcribe", "transcribe", 36, 196, { engine: "auto" }],
      ["n_hook", "hook_pick", 36, 368, { n: 5, minLen: 15, maxLen: 45, hookStrategy: "strongest" }],
      ["n_clip", "clip", 36, 540, { lengths: [15, 30, 45], pacing: "any", n: 12 }],
      ["n_sub", "subtitle", 36, 712, { style: "any", hookTitle: "any" }],
      ["n_crop", "crop", 400, 24, { aspects: ["9:16", "1:1", "4:5", "16:9"] }],
      ["n_persona", "persona_score", 400, 196, { mode: "auto" }],
      ["n_channel", "channel_test", 400, 368, { channels: ["tiktok", "reels", "shorts", "x", "linkedin"] }],
      ["n_win", "pick_winner", 400, 540, { prefer: "persona", channel: "reels" }],
      ["n_pub", "publish", 400, 712, { action: "local" }]
    ];
    return {
      name: "Default ClipCut pipeline", version: 1,
      nodes: col.map(([id, type, x, y, config]) => ({ id, type, x, y, config })),
      edges: [["e1", "n_ingest", "n_transcribe"], ["e2", "n_transcribe", "n_hook"], ["e3", "n_hook", "n_clip"], ["e4", "n_clip", "n_sub"], ["e5", "n_sub", "n_crop"], ["e6", "n_crop", "n_persona"], ["e7", "n_persona", "n_channel"], ["e8", "n_channel", "n_win"], ["e9", "n_win", "n_pub"]]
        .map(([id, from, to]) => ({ id, from, to }))
    };
  }

  function normalize(p) {
    if (!p || !Array.isArray(p.nodes)) return builtin();
    const nodes = [];
    p.nodes.forEach((n) => {
      if (!n || !TYPES[n.type]) return;
      nodes.push({
        id: String(n.id || uid(n.type)), type: n.type,
        x: Number(n.x) || 40, y: Number(n.y) || 40,
        config: Object.assign(structuredClone(TYPES[n.type].defaults), n.config || {}),
        output: n.output || null, phase: n.phase || "idle",
        nick: n.nick || "", approach: n.approach || "", spawnedBy: n.spawnedBy || "", branchKey: n.branchKey || "",
        winBranch: !!n.winBranch
      });
    });
    const ids = new Set(nodes.map((n) => n.id));
    const edges = (p.edges || []).filter((e) => e && ids.has(e.from) && ids.has(e.to) && e.from !== e.to)
      .map((e) => ({ id: String(e.id || uid("e")), from: e.from, to: e.to }));
    return { name: p.name || "Pipeline", version: 1, nodes, edges };
  }

  function snapshot(keepOutput) {
    return {
      name: pipeline.name, version: 1,
      nodes: pipeline.nodes.map((n) => {
        const o = { id: n.id, type: n.type, x: Math.round(n.x), y: Math.round(n.y), config: n.config, nick: n.nick, approach: n.approach, spawnedBy: n.spawnedBy, branchKey: n.branchKey };
        if (keepOutput) { o.output = n.output; o.phase = n.phase; o.winBranch = n.winBranch; }
        return o;
      }),
      edges: pipeline.edges.map((e) => ({ id: e.id, from: e.from, to: e.to }))
    };
  }
  function saveLocalSoon() { clearTimeout(saveTimer); saveTimer = setTimeout(() => { try { localStorage.setItem(LS, JSON.stringify(snapshot(true))); } catch (e) {} }, 200); }
  function loadLocal() { try { const t = localStorage.getItem(LS); return t ? normalize(JSON.parse(t)) : null; } catch (e) { return null; } }

  async function j(url, opt) {
    const r = await fetch(url, opt);
    const text = await r.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (e) { body = { detail: text }; }
    if (!r.ok) {
      const d = body && (body.detail || body.error);
      throw new Error(typeof d === "string" ? d : (r.status + " " + r.statusText));
    }
    return body;
  }
  async function fetchJsonOptional(url) {
    try { const r = await fetch(url); if (!r.ok) return null; return await r.json(); } catch (e) { return null; }
  }

  function reaches(start, goal) {
    const seen = new Set(); const stack = [start];
    while (stack.length) {
      const c = stack.pop();
      if (c === goal) return true;
      pipeline.edges.forEach((e) => { if (e.from === c && !seen.has(e.to)) { seen.add(e.to); stack.push(e.to); } });
    }
    return false;
  }
  function ancestors(id) {
    const out = new Set(); const seen = new Set([id]); const stack = [id];
    while (stack.length) {
      const cur = stack.pop();
      pipeline.edges.forEach((e) => {
        if (e.to === cur && !seen.has(e.from)) { seen.add(e.from); out.add(e.from); stack.push(e.from); }
      });
    }
    return out;
  }
  function edgeProblem(e) {
    const a = nodeById(e.from), b = nodeById(e.to);
    if (!a || !b) return "Those steps are missing.";
    const msgs = [];
    if (!TYPES[b.type].accepts.includes(TYPES[a.type].emits)) {
      msgs.push(TYPES[a.type].label + " outputs " + PLAIN[TYPES[a.type].emits] + ", and " + TYPES[b.type].label + " expects " + TYPES[b.type].accepts.map((t) => PLAIN[t]).join(" or ") + ".");
    }
    if (TYPES[a.type].order > TYPES[b.type].order) {
      msgs.push(TYPES[a.type].label + " comes after " + TYPES[b.type].label + ". Wiring a later step into an earlier one (publish before clip, for example) is marked invalid.");
    }
    if (reaches(e.to, e.from)) msgs.push("This wire loops.");
    return msgs.join(" ");
  }
  function nodeIssues(n) {
    const c = n.config || {}, msgs = [];
    if (n.type === "ingest") {
      if (c.source === "run" && !c.run) msgs.push("Choose a saved run, or switch the source.");
      if (c.source === "sample" && !c.sample) msgs.push("Choose a sample video. This checkout may not include mp4 files.");
      if (c.source === "upload" && !c.fileName && !pendingFiles[n.id]) msgs.push("Choose a video file. Browsers forget it after a refresh.");
    }
    if (n.type === "clip" && (!c.lengths || !c.lengths.length)) msgs.push("Select at least one length (15, 30, or 45 seconds).");
    if (n.type === "crop" && (!c.aspects || !c.aspects.length)) msgs.push("Select at least one aspect ratio.");
    if (n.type === "channel_test" && (!c.channels || !c.channels.length)) msgs.push("Select at least one channel.");
    if (n.type === "branch" && !String(c.idea || "").trim()) msgs.push("Describe the approaches you want to try.");
    if ((n.type === "persona_score" || n.type === "hook_rewrite") && ["heuristic", "llm", "auto"].indexOf(c.mode || "auto") < 0) msgs.push("Mode must be auto, heuristic, or llm.");
    const incoming = pipeline.edges.some((e) => e.to === n.id);
    if (incoming && (n.type === "publish" || n.type === "persona_score")) {
      const ans = ancestors(n.id);
      const hasClip = [...ans].some((id) => { const x = nodeById(id); return x && x.type === "clip"; });
      if (!hasClip && n.type === "publish") msgs.push("Publish is connected, but no Clip step is upstream. The file copy waits until a clip sits upstream.");
      if (!hasClip && n.type === "persona_score") msgs.push("Persona score is connected, but no Clip step is upstream, so there is nothing to score.");
    }
    return msgs;
  }
  function allIssues() {
    const list = [];
    pipeline.edges.forEach((e) => { const m = edgeProblem(e); if (m) list.push({ edgeId: e.id, message: m }); });
    pipeline.nodes.forEach((n) => nodeIssues(n).forEach((m) => list.push({ nodeId: n.id, message: m })));
    return list;
  }
  function shortEdge(msg) {
    if (!msg) return "";
    if (msg.indexOf("loop") >= 0) return "loop";
    if (msg.indexOf("comes after") >= 0) return "out of order";
    return "mismatched";
  }

  function qNode(id) { return world.querySelector('.node[data-id="' + id + '"]'); }
  function status(text) { $("#status").textContent = text || ""; }

  function summaryHTML(n) {
    const t = TYPES[n.type], o = n.output;
    if (n.phase === "run") return `<div>Working… ${esc((o && o.note) || "")}</div>`;
    if (!o) return `<div class="mut">${esc(t.idle)}</div>`;
    if (o.type === "error") return `<div class="issue">${esc(o.message)}</div>`;
    if (o.type === "empty") return `<div class="mut">${esc(o.message)}</div>`;
    if (o.type === "branch") {
      return `${chipHTML(o.source)}${o.progress ? `<div>${esc(o.progress)}</div>` : ""}<div>${esc((o.names || []).join(" · ") || "No branches yet")}</div>` +
        (o.winner ? `<div>Winner <b>${esc(o.winner.name)}</b> · ${fmtPersona(o.winner.aggregate)}</div>` : "") +
        `<div class="mut clamp">${esc(o.disclaimer || "")}</div>`;
    }
    if (o.type === "persona" && o.response) {
      const r = o.response, v = (r.variants || []).find((x) => x.id === r.winner) || (r.variants || [])[0];
      return `${chipHTML(r.source)}<div>Winner <b>${esc(r.winner || "–")}</b> · ${fmtPersona(v && v.aggregate)}</div><div class="mut">Persona aggregate, 0–1. Not the legacy channel score.</div><div class="disclaimer clamp">${esc(r.disclaimer || "")}</div>`;
    }
    if (o.type === "hooks" && o.response) {
      const r = o.response;
      const count = (r.variants || []).reduce((sum, v) => sum + ((v.suggestions || []).length), 0);
      return `${hookChipHTML(r.source)}<span class="mut">source ${esc(r.source || "—")}</span><div>${count} lines</div>`;
    }
    if (o.type === "ingest") return `<div><b>${esc(o.runId || "")}</b></div><div class="mut">${esc(o.note || "")}</div>`;
    if (o.type === "text") return `<div>${esc(o.title || "")}</div><div class="mut clamp">${esc(o.body || "")}</div>`;
    if (o.html) return o.html;
    return `<div class="mut">${esc(o.note || t.idle)}</div>`;
  }

  function buildNode(n) {
    const t = TYPES[n.type];
    const issues = nodeIssues(n);
    const el = document.createElement("article");
    el.className = "node" + (n.id === selectedId ? " sel" : "") + (issues.length ? " has-warn" : "") + (n.phase === "run" ? " phase-run" : "") + (n.winBranch ? " is-win" : "");
    el.dataset.id = n.id; el.dataset.type = n.type;
    el.style.left = n.x + "px"; el.style.top = n.y + "px";
    const title = n.nick || t.label;
    const kicker = (n.winBranch ? "★ " : "") + (n.approach ? n.approach : t.label);
    el.innerHTML =
      `<header data-tip="${esc(t.tip)}"><i class="accent"></i><div><div class="kicker">${esc(kicker)}</div><div class="title">${esc(title)}</div></div>` +
      `<button class="x" type="button" aria-label="Remove ${esc(t.label)}">×</button></header>` +
      `<p class="desc">${esc(n.approach && n.nick ? t.desc : t.desc)}</p>` +
      (t.accepts.length ? `<button class="port in" type="button" data-dir="in" data-tip="Input. Accepts ${esc(t.accepts.map((x) => PLAIN[x]).join(" or "))}. Drag from another step's output." aria-label="Input port"></button>` : "") +
      (t.emits ? `<button class="port out" type="button" data-dir="out" data-tip="Output: ${esc(PLAIN[t.emits])}. Drag onto the next step's input." aria-label="Output port"></button>` : "") +
      `<div class="sum">${summaryHTML(n)}${issues.length ? `<div class="issue">${esc(issues[0])}</div>` : ""}</div>`;
    return el;
  }

  function renderPalette() {
    $("#pal").innerHTML = TYPE_ORDER.map((type) => {
      const t = TYPES[type];
      return `<button type="button" class="pal-item${type === "branch" ? " star" : ""}" data-type="${type}" data-tip="${esc(t.tip)}" aria-label="Add ${esc(t.label)}"><i></i><span><b>${esc(t.label)}</b><small>${esc(t.desc)}</small></span><em>Add</em></button>`;
    }).join("");
  }

  function render() {
    world.querySelectorAll(".node").forEach((el) => el.remove());
    pipeline.nodes.forEach((n) => world.appendChild(buildNode(n)));
    $("#empty").hidden = pipeline.nodes.length > 0;
    $("#pname").value = pipeline.name;
    wires();
    updateWarn();
    if (selectedId) renderInspector();
  }
  function paint(n) {
    const el = qNode(n.id);
    const fresh = buildNode(n);
    if (el) el.replaceWith(fresh); else world.appendChild(fresh);
    wires(); updateWarn();
  }

  function portWorld(nodeId, dir) {
    const node = qNode(nodeId); if (!node) return null;
    const port = node.querySelector(".port." + dir); if (!port) return null;
    const r = port.getBoundingClientRect(); const vr = vp.getBoundingClientRect();
    return { x: (r.left + r.width / 2 - vr.left - T.x) / T.k, y: (r.top + r.height / 2 - vr.top - T.y) / T.k };
  }
  function clientToWorld(cx, cy) {
    const vr = vp.getBoundingClientRect();
    return { x: (cx - vr.left - T.x) / T.k, y: (cy - vr.top - T.y) / T.k };
  }
  function worldToScreen(p) {
    const r = vp.getBoundingClientRect();
    return { x: r.left + T.x + p.x * T.k, y: r.top + T.y + p.y * T.k };
  }
  function curve(a, b) {
    const dx = Math.max(48, Math.abs(b.x - a.x) * 0.45);
    return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
  }
  function curveMid(a, b) {
    const dx = Math.max(48, Math.abs(b.x - a.x) * 0.45);
    const c1 = { x: a.x + dx, y: a.y }, c2 = { x: b.x - dx, y: b.y };
    const u = 0.5, t = 0.5;
    return {
      x: u*u*u*a.x + 3*u*u*t*c1.x + 3*u*t*t*c2.x + t*t*t*b.x,
      y: u*u*u*a.y + 3*u*u*t*c1.y + 3*u*t*t*c2.y + t*t*t*b.y
    };
  }
  function wires() {
    let maxX = 900, maxY = 900;
    pipeline.nodes.forEach((n) => { maxX = Math.max(maxX, n.x + 420); maxY = Math.max(maxY, n.y + 280); });
    svg.setAttribute("width", maxX); svg.setAttribute("height", maxY);
    let html = "";
    pipeline.edges.forEach((e) => {
      const a = portWorld(e.from, "out"), b = portWorld(e.to, "in");
      if (!a || !b) return;
      const problem = edgeProblem(e);
      const d = curve(a, b);
      html += `<path class="hit" data-edge="${esc(e.id)}" d="${d}"></path>`;
      html += `<path class="wire${problem ? " bad" : ""}${e.id === selectedEdge ? " sel" : ""}" d="${d}"><title>${esc(problem || "Connection")}</title></path>`;
      html += `<circle cx="${b.x}" cy="${b.y}" r="3.5" class="${problem ? "bad" : ""}"></circle>`;
      if (problem) { const mid = curveMid(a, b); html += `<text class="wlab" x="${mid.x}" y="${mid.y - 6}">${esc(shortEdge(problem))}</text>`; }
    });
    if (connectState && connectState.d) html += `<path class="temp" d="${connectState.d}"></path>`;
    svg.innerHTML = html;
    placeCut(); mini();
  }
  function placeCut() {
    const btn = $("#cutwire");
    if (!selectedEdge) { btn.hidden = true; return; }
    const e = pipeline.edges.find((x) => x.id === selectedEdge);
    if (!e) { btn.hidden = true; return; }
    const a = portWorld(e.from, "out"), b = portWorld(e.to, "in");
    if (!a || !b) { btn.hidden = true; return; }
    const s = worldToScreen(curveMid(a, b));
    btn.style.left = s.x + "px"; btn.style.top = s.y + "px"; btn.hidden = false;
  }

  function apply() {
    world.style.transform = `translate(${T.x}px,${T.y}px) scale(${T.k})`;
    $("#zoomlbl").textContent = Math.round(T.k * 100) + "%";
    placeCut(); mini();
  }
  function zoomAt(cx, cy, factor) {
    const k = Math.min(1.6, Math.max(0.35, T.k * factor));
    T.x = cx - (cx - T.x) * (k / T.k); T.y = cy - (cy - T.y) * (k / T.k); T.k = k; apply();
  }
  function zoomTo(nodes) {
    if (!nodes.length) return;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    nodes.forEach((n) => { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x + 250); y1 = Math.max(y1, n.y + 130); });
    const pad = 28, W = vp.clientWidth, H = vp.clientHeight;
    const k = Math.min(1.05, (W - 2 * pad) / Math.max(1, x1 - x0), (H - 2 * pad) / Math.max(1, y1 - y0));
    T.k = Math.max(0.35, k);
    T.x = pad + (W - 2 * pad - (x1 - x0) * T.k) / 2 - x0 * T.k;
    T.y = pad + (H - 2 * pad - (y1 - y0) * T.k) / 2 - y0 * T.k;
    apply();
  }
  function fit() { zoomTo(pipeline.nodes); }

  function mini() {
    const c = $("#mini"), g = c.getContext("2d");
    g.clearRect(0, 0, c.width, c.height);
    if (!pipeline.nodes.length) return;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    pipeline.nodes.forEach((n) => { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x + 244); y1 = Math.max(y1, n.y + 120); });
    const pad = 8, s = Math.min((c.width - 2 * pad) / (x1 - x0), (c.height - 2 * pad) / (y1 - y0));
    c._m = { x0, y0, s, pad };
    pipeline.nodes.forEach((n) => {
      g.fillStyle = n.winBranch ? "#facc15" : (TYPES[n.type].color + "aa");
      g.fillRect(pad + (n.x - x0) * s, pad + (n.y - y0) * s, Math.max(3, 244 * s), Math.max(3, 110 * s));
    });
    g.strokeStyle = "#e7eaf2"; g.lineWidth = 1;
    const rx = pad + ((-T.x / T.k) - x0) * s, ry = pad + ((-T.y / T.k) - y0) * s;
    g.strokeRect(rx, ry, (vp.clientWidth / T.k) * s, (vp.clientHeight / T.k) * s);
  }

  function placeForNew(at) {
    if (at) return { x: at.x - 20, y: at.y - 20 };
    if (!pipeline.nodes.length) return clientToWorld(vp.clientWidth / 2, vp.clientHeight / 2);
    let maxX = 40;
    pipeline.nodes.forEach((n) => { maxX = Math.max(maxX, n.x + 270); });
    return { x: maxX + 30, y: 48 };
  }
  function addNode(type, at) {
    if (!TYPES[type]) return;
    const p = placeForNew(at);
    const n = { id: uid(type), type, x: p.x, y: p.y, config: structuredClone(TYPES[type].defaults), output: null, phase: "idle", nick: "", approach: "", spawnedBy: "", branchKey: "", winBranch: false };
    pipeline.nodes.push(n);
    hideMenu();
    render();
    selectNode(n.id);
    status("Added " + TYPES[type].label + ". Drag its port to connect it, or set it on the right.");
    saveLocalSoon();
    return n;
  }
  function removeNode(id) {
    const extra = pipeline.nodes.filter((n) => n.spawnedBy === id).map((n) => n.id);
    const drop = new Set([id, ...extra]);
    pipeline.nodes = pipeline.nodes.filter((n) => !drop.has(n.id));
    pipeline.edges = pipeline.edges.filter((e) => !drop.has(e.from) && !drop.has(e.to));
    if (drop.has(selectedId)) selectedId = null;
    selectedEdge = null;
    render(); renderInspector(); saveLocalSoon();
    status(extra.length ? "Removed that step and its branches." : "Removed the step.");
  }
  function addEdge(from, to) {
    if (from === to) return;
    if (pipeline.edges.some((e) => e.from === from && e.to === to)) return;
    pipeline.edges.push({ id: uid("e"), from, to });
    wires(); updateWarn(); saveLocalSoon();
    const problem = edgeProblem(pipeline.edges[pipeline.edges.length - 1]);
    status(problem ? "Connected, with a warning: " + problem : "Connected.");
  }
  function removeEdge(id) {
    pipeline.edges = pipeline.edges.filter((e) => e.id !== id);
    if (selectedEdge === id) selectedEdge = null;
    wires(); updateWarn(); renderInspector(); saveLocalSoon();
  }

  function selectNode(id) {
    selectedId = id; selectedEdge = null;
    world.querySelectorAll(".node.sel").forEach((el) => el.classList.remove("sel"));
    const el = id && qNode(id); if (el) el.classList.add("sel");
    placeCut(); renderInspector();
  }
  function selectEdge(id) {
    selectedEdge = id; selectedId = null;
    world.querySelectorAll(".node.sel").forEach((el) => el.classList.remove("sel"));
    wires(); renderInspector();
  }

  function updateWarn() {
    const list = allIssues();
    const btn = $("#warnbtn");
    if (!list.length) { btn.hidden = true; return; }
    btn.hidden = false;
    btn.textContent = list.length + (list.length === 1 ? " warning" : " warnings");
    btn.dataset.tip = "Warnings explain bad wires and missing settings. They do not block Run.";
  }

  function field(label, tip, control) {
    return `<label class="field"><span data-tip="${esc(tip)}">${esc(label)}</span>${control}</label>`;
  }
  function opts(list, cur) {
    return list.map(([v, lab]) => `<option value="${esc(v)}"${v === cur ? " selected" : ""}>${esc(lab)}</option>`).join("");
  }
  function checks(group, choices, cur) {
    const set = new Set(cur || []);
    return `<div class="checks">${choices.map(([v, lab]) => `<label><input type="checkbox" data-group="${group}" value="${esc(v)}"${set.has(v) ? " checked" : ""}> ${esc(lab)}</label>`).join("")}</div>`;
  }

  function inspectorFields(n) {
    const c = n.config;
    if (n.type === "ingest") {
      return field("Source", "Saved run replays results without rendering. Sample and upload can start a real cut. Branch renders need the mp4 on disk.",
        `<select data-key="source">${opts([["run", "Saved run"], ["sample", "Sample video"], ["upload", "Upload"]], c.source)}</select>`) +
        (c.source === "run" ? field("Run", "A finished cutter run. v2_niosh_multi is the sample set in this repo. Its mp4 may be absent, so replaying works and re-rendering does not.",
          `<select data-key="run">${(catalog.runs.length ? catalog.runs : [c.run]).filter(Boolean).map((r) => `<option${r === c.run ? " selected" : ""}>${esc(r)}</option>`).join("")}</select>`) : "") +
        (c.source === "sample" ? field("Sample", "Files in samples/. This checkout often has the SRT but not the mp4.",
          `<select data-key="sample"><option value="">Choose…</option>${catalog.samples.map((s) => `<option${s === c.sample ? " selected" : ""}>${esc(s)}</option>`).join("")}</select>`) : "") +
        (c.source === "upload" ? field("Video file", "Used for a real cut and for branch renders. Max size is the server upload cap.",
          `<input type="file" data-key="file" accept="video/*">`) + (c.fileName ? `<div class="mut">${esc(c.fileName)}</div>` : "") : "") +
        field("Variant count for a new full cut", "Passed to POST /api/cut as n. Branch renders ignore this and cut one recipe each.",
          `<input type="number" data-key="n" min="2" max="20" value="${esc(c.n)}">`);
    }
    if (n.type === "transcribe") return field("Engine note", "auto shows whatever the run used. Whisper and SRT describe intent; the cutter picks SRT automatically for the NIOSH sample.",
      `<select data-key="engine">${opts([["auto", "Whatever the run used"], ["whisper", "faster-whisper base"], ["srt", "Human SRT"]], c.engine)}</select>`);
    if (n.type === "hook_pick") return field("How many moments to show", "Filters a saved run for display. A branch render uses the strategy below instead.",
      `<input type="number" data-key="n" min="1" max="8" value="${esc(c.n)}">`) +
      field("Hook for a branch render", "strongest, second, and earliest are the cutter's hook choices.",
        `<select data-key="hookStrategy">${opts([["strongest", "Strongest line"], ["second", "Second-best line"], ["earliest", "Earliest line"]], c.hookStrategy || "strongest")}</select>`);
    if (n.type === "clip") return field("Lengths", "Seconds the cutter targets. A branch uses the first checked length.",
      checks("lengths", [["15", "15s"], ["30", "30s"], ["45", "45s"]], (c.lengths || []).map(String))) +
      field("Pacing", "tight_punch jumps silent gaps and punch-zooms. raw_punch keeps pauses.",
        `<select data-key="pacing">${opts([["any", "Any (when replaying)"], ["tight_punch", "Tight punch"], ["raw_punch", "Raw punch"], ["tight", "Tight"], ["raw", "Raw"]], c.pacing)}</select>`) +
      field("Ending", "loop cuts on the last word. fade dips the picture out. A branch render uses this.",
        `<select data-key="ending">${opts([["loop", "Loop"], ["fade", "Fade"]], c.ending || "loop")}</select>`) +
      field("Count for a full cut", "Only POST /api/cut uses this.", `<input type="number" data-key="n" min="2" max="20" value="${esc(c.n)}">`);
    if (n.type === "subtitle") return field("Caption style", "word_pop is the big muted-autoplay style. sentence is a lower third.",
      `<select data-key="style">${opts([["any", "Any"], ["word_pop", "Word pop"], ["sentence", "Sentence"], ["none", "None"]], c.style)}</select>`) +
      field("Hook title overlay", "On puts the opening line at the top for 3 seconds.",
        `<select data-key="hookTitle">${opts([["any", "Any"], ["on", "On"], ["off", "Off"]], c.hookTitle)}</select>`);
    if (n.type === "crop") return field("Aspects", "The first checked aspect is what a branch renders.",
      checks("aspects", [["9:16", "9:16 vertical"], ["1:1", "1:1 square"], ["4:5", "4:5"], ["16:9", "16:9"]], c.aspects || [])) +
      field("Channel", "Sent to cutter v2 with the aspect. Leave this on “From aspect” and the canvas picks TikTok, X, LinkedIn, or YouTube from the frame.",
        `<select data-key="channel">${opts([["", "From aspect"], ["TikTok", "TikTok"], ["Reels", "Reels"], ["Shorts", "Shorts"], ["X", "X"], ["LinkedIn", "LinkedIn"], ["YouTube", "YouTube"], ["IG_Feed", "IG_Feed"], ["IG_Stories", "IG_Stories"], ["FB_Reels", "FB_Reels"], ["FB_Feed", "FB_Feed"]], c.channel || "")}</select>`);
    if (n.type === "persona_score") return field("Mode", "auto uses the persona service's own fallback. llm returns 503 without an API key; this canvas then shows a stub instead of failing.",
      `<select data-key="mode">${opts([["auto", "Auto"], ["heuristic", "Heuristic"], ["llm", "LLM"]], c.mode)}</select>`);
    if (n.type === "hook_rewrite") return field("Mode", "auto asks Grok when XAI_API_KEY or GROK_API_KEY is set, and uses template lines otherwise. llm returns 503 without a key; this canvas then shows a stub. heuristic is templates only.",
      `<select data-key="mode">${opts([["auto", "Auto"], ["heuristic", "Template (heuristic)"], ["llm", "LLM · Grok"]], c.mode || "auto")}</select>`) +
      field("Channels", "Leave every box off for three lines per persona. Check a channel to ask for three lines per persona per channel. Ids are lowercased.",
        checks("channels", CHANNELS, c.channels || []));
    if (n.type === "channel_test") return field("Channels", "Ids match channels.py, lowercased. The simulated A/B table is separate from persona fit.",
      checks("channels", CHANNELS, c.channels || []));
    if (n.type === "pick_winner") return field("Rule", "Persona aggregate, simulated A/B, or the legacy caption tie-break. The numbers are not one scale.",
      `<select data-key="prefer">${opts([["persona", "Persona aggregate"], ["channel", "Simulated channel A/B"], ["run", "Legacy run winner"]], c.prefer)}</select>`) +
      (c.prefer === "channel" ? field("Channel", "Which simulated test to read.", `<select data-key="channel">${opts(CHANNELS, c.channel)}</select>`) : "");
    if (n.type === "publish") return `<p class="mut">Copies the legacy winner file on the server. Nothing is posted to a platform.</p>`;
    if (n.type === "branch") return "";
    return "";
  }

  function resultHTML(n) {
    const o = n.output;
    if (!o) return "";
    if (o.type === "persona" && o.response) return personaHTML(o.response);
    if (o.type === "hooks" && o.response) return hooksHTML(o.response);
    if (o.type === "channel") return channelHTML(o);
    if (o.type === "winner") return `<h3>Choice</h3><p><b>${esc(o.id || "–")}</b></p><p>${esc(o.note || "")}</p>` + (o.aggregate != null ? `<p>Persona aggregate ${fmtPersona(o.aggregate)} <span class="mut">(0–1, not the legacy channel score)</span></p>` : "");
    if (o.type === "publish") return `<h3>Publish</h3><p>${esc(o.note || "")}</p>` + (o.url ? `<p><a href="${esc(o.url)}">Open the local winner file</a></p>` : "") + (o.error ? `<p class="issue">${esc(o.error)}</p>` : "");
    if (o.type === "text" || o.type === "ingest") return `<h3>Result</h3><p>${esc(o.title || o.runId || "")}</p><p class="mut">${esc(o.body || o.note || "")}</p>` + (o.lines ? `<ul>${o.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : "") + (o.video ? `<video src="${esc(o.video)}" controls style="width:100%;border-radius:8px;margin-top:8px"></video>` : "");
    if (o.type === "branch" && o.winner) return `<h3>Winning branch</h3><p><b>${esc(o.winner.name)}</b> · aggregate ${fmtPersona(o.winner.aggregate)}</p><p class="mut">${esc(o.winner.note || "")}</p><ul>${(o.winner.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`;
    return "";
  }

  function personaHTML(r) {
    const v = (r.variants || []).find((x) => x.id === r.winner) || (r.variants || [])[0] || {};
    const personas = v.personas || [];
    return `<h3>Persona response</h3>${chipHTML(r.source)} <span class="mut">${esc(r.model_version || "")}</span>
      <div class="disclaimer">${esc(r.disclaimer || "No disclaimer was returned.")}</div>
      <p>Winner <b>${esc(r.winner || "–")}</b> · persona aggregate <b>${fmtPersona(v.aggregate)}</b></p>
      <p class="mut">This 0–1 aggregate is not the legacy /api/score channel score. Don't read them as the same number.</p>
      ${(personas).map((p) => `<p><b>${esc(p.label || p.id)}</b> ${fmtPersona(p.score)} <span class="mut">${esc(p.description || "")}</span><br><span class="mut">${esc((p.reasons || []).slice(0, 2).join(" · "))}</span></p>`).join("")}
      <h3>Other variants</h3>
      <table><tr><th>Variant</th><th>Aggregate</th><th>Source</th></tr>
      ${(r.variants || []).map((x) => `<tr class="${x.id === r.winner ? "win" : ""}"><td>${esc(x.id)}</td><td>${fmtPersona(x.aggregate)}</td><td>${esc(x.source || r.source || "")}</td></tr>`).join("")}
      </table>`;
  }
  function hooksHTML(r) {
    const anyChannel = (r.variants || []).some((v) => (v.suggestions || []).some((s) => s.channel));
    const blocks = (r.variants || []).map((v) => `<h3>${esc(v.id)}</h3><p>Original: ${esc(v.original_hook || "–")}</p>
      <table><tr><th>Persona</th>${anyChannel ? "<th>Channel</th>" : ""}<th>Hook</th><th>Estimate</th><th>Why</th></tr>
      ${(v.suggestions || []).map((s) => `<tr><td>${esc(s.persona)}<div>${hookChipHTML(s.origin || r.source)}</div></td>${anyChannel ? `<td>${esc(s.channel || "")}</td>` : ""}<td>${esc(s.hook || "–")}${s.title_card ? `<div class="mut">Title card: ${esc(s.title_card)}</div>` : ""}</td><td>${estimateCell(s)}</td><td>${esc(s.reason || "")}</td></tr>`).join("")}
      </table>`).join("");
    return `<h3>Hook rewrite</h3>${hookChipHTML(r.source)} <span class="mut">source ${esc(r.source || "—")}${r.model_version ? " · " + esc(r.model_version) : ""}</span><div class="disclaimer">${esc(r.disclaimer || "")}</div>${blocks}`;
  }
  function channelHTML(o) {
    const fits = (o.fits || []).map((f) => `<tr><td>${esc(f.label)}</td><td><span class="mut">${esc(f.id)}</span></td><td>${fmtPersona(f.fit)}</td></tr>`).join("");
    const abs = (o.ab || []).map((b) => `<h3>${esc(b.channel)} <span class="chip ab">Simulated A/B</span></h3><p class="mut">${esc(b.note || "")}</p>
      <table><tr><th>Clip</th><th>P(best)</th><th>Watch</th><th>Views</th></tr>
      ${(b.items || []).map((i) => `<tr class="${i.id === b.winner ? "win" : ""}"><td>${esc(i.id)}</td><td>${i.pBest == null ? "–" : Math.round(i.pBest * 1000) / 10 + "%"}</td><td>${i.watch == null ? "–" : i.watch}</td><td>${i.views == null ? "–" : i.views}</td></tr>`).join("")}
      </table>`).join("");
    return `<h3>Persona channel fit</h3><p class="mut">Fit from the persona response for ${esc(o.fitFor || "the winner")}. Not predicted views and not the legacy channel score.</p>
      <table><tr><th>Channel</th><th>Id</th><th>Fit</th></tr>${fits}</table>
      <h3>Simulated traffic</h3><p class="mut">Mock A/B from /api/channels. Not live platform data, and not the persona aggregate.</p>${o.abError ? `<p class="issue">${esc(o.abError)}</p>` : abs}`;
  }

  function renderInspector() {
    if (selectedEdge && !selectedId) {
      const e = pipeline.edges.find((x) => x.id === selectedEdge);
      const msg = e ? edgeProblem(e) : "";
      insp.innerHTML = `<h2>Connection</h2><p>${msg ? esc(msg) : "This wire matches the step order."}</p><button id="insp-cut" type="button">Disconnect</button>`;
      const b = $("#insp-cut"); if (b) b.onclick = () => removeEdge(selectedEdge);
      return;
    }
    const n = selectedId && nodeById(selectedId);
    if (!n) {
      insp.innerHTML = `<div class="welcome"><h2>How a pipeline works</h2>
        <p class="lead">One long video becomes short, subtitled variants. Personas rank them. A simulated channel test and a local file copy follow.</p>
        <ol>
          <li>Add steps from the left, <b>+ Add step</b>, or a right-click on the canvas.</li>
          <li>Drag from the right port to the next left port.</li>
          <li><b>Try approaches</b> turns a sentence into several real cutter recipes.</li>
          <li>Persona scores and hook rewrites are labelled heuristic, LLM, template, or simulated stub. Channel tests are simulated.</li>
        </ol>
        <p class="mut">Amber warnings mean a wire or a setting looks wrong. You can still experiment.</p></div>`;
      return;
    }
    const t = TYPES[n.type];
    const issues = nodeIssues(n);
    let extra = "";
    if (n.type === "branch") {
      extra = field("Edit idea", "Plain language. Example: a fast punchy cut, a calm storytelling cut, and big captions for silent viewing.",
        `<textarea class="idea" data-key="idea">${esc(n.config.idea || "")}</textarea>`) +
        `<p><button type="button" id="build-branches" class="pri">Build branches</button> <button type="button" id="render-branches">Render branches</button></p>
         <p class="mut">Build lays out one column per approach. Render runs cutter v2 on each column, one at a time, then scores and rewrites hooks.</p>`;
    }
    insp.innerHTML = `<div class="kicker" style="color:${t.color}">${esc(t.label)}</div><h2>${esc(n.nick || t.label)}</h2>
      <p class="lead" data-tip="${esc(t.tip)}">${esc(t.desc)} ${esc(t.tip)}</p>
      ${issues.length ? `<div class="disclaimer">${issues.map(esc).join("<br>")}</div>` : ""}
      ${extra}${inspectorFields(n)}${resultHTML(n)}`;
    const build = $("#build-branches"), rend = $("#render-branches");
    if (build) build.onclick = () => buildBranches(n.id);
    if (rend) rend.onclick = () => renderBranches(n.id);
  }

  function onInsp(e) {
    const el = e.target, n = selectedId && nodeById(selectedId);
    if (!n || !el || !el.dataset) return;
    if (el.dataset.group) {
      if (e.type !== "change") return;
      const cur = new Set((n.config[el.dataset.group] || []).map(String));
      if (el.checked) cur.add(el.value); else cur.delete(el.value);
      if (el.dataset.group === "lengths") n.config.lengths = ["15", "30", "45"].filter((v) => cur.has(v)).map(Number);
      else n.config[el.dataset.group] = [...cur];
      afterConfig(n, false); return;
    }
    const key = el.dataset.key;
    if (!key) return;
    if (el.type === "file" || el.tagName === "SELECT") { if (e.type !== "change") return; }
    else if (e.type !== "input") return;
    if (el.type === "file") {
      const f = el.files && el.files[0];
      pendingFiles[n.id] = f || null;
      n.config.fileName = f ? f.name : "";
      n.config.source = "upload";
      afterConfig(n, true); return;
    }
    if (el.type === "number") n.config[key] = el.value === "" ? "" : Number(el.value);
    else n.config[key] = el.value;
    afterConfig(n, el.tagName === "SELECT");
  }
  function afterConfig(n, rebuild) {
    paint(n); saveLocalSoon(); updateWarn();
    if (rebuild) renderInspector();
  }

  function hideMenu() { $("#menu").hidden = true; menuAt = null; }
  function openMenu(x, y, at, nodeId) {
    const menu = $("#menu");
    const add = TYPE_ORDER.map((type) => {
      const t = TYPES[type];
      return `<button type="button" data-add="${type}" style="--c:${t.color}"><i></i><span><b>${esc(t.label)}</b><small>${esc(t.desc)}</small></span></button>`;
    }).join("");
    const head = nodeId
      ? `<button type="button" data-remove="${esc(nodeId)}"><span><b>Remove this step</b><small>Also removes branches it spawned</small></span></button>`
      : "";
    menu.innerHTML = `<span class="mh">${nodeId ? "This step" : "Add a step"}</span>${head}<span class="mh">Add a step</span>${add}`;
    menu.hidden = false;
    const w = 260, h = Math.min(menu.scrollHeight || 420, innerHeight * 0.7);
    menu.style.left = Math.max(8, Math.min(x, innerWidth - w - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(y, innerHeight - 80)) + "px";
    menuAt = at;
  }

  function clearSpawn(branchId) {
    const ids = new Set(pipeline.nodes.filter((n) => n.spawnedBy === branchId).map((n) => n.id));
    pipeline.nodes = pipeline.nodes.filter((n) => !ids.has(n.id));
    pipeline.edges = pipeline.edges.filter((e) => !ids.has(e.from) && !ids.has(e.to));
  }
  async function buildBranches(branchId) {
    const n = nodeById(branchId); if (!n) return;
    const text = n.config.idea || "";
    status("Turning that sentence into cutter settings…");
    let res;
    try { res = await j("/api/edit-idea", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) }); }
    catch (e) { n.output = { type: "error", message: e.message }; n.phase = "err"; paint(n); renderInspector(); return; }
    clearSpawn(branchId);
    const chain = ["hook_pick", "clip", "subtitle", "crop", "persona_score", "hook_rewrite"];
    const colW = 270, rowH = 148;
    (res.branches || []).forEach((b, i) => {
      const x = n.x + 300 + i * colW;
      let prev = n.id;
      chain.forEach((type, k) => {
        const node = {
          id: uid(type), type, x, y: n.y + k * rowH,
          config: structuredClone(TYPES[type].defaults), output: null, phase: "idle",
          nick: k === 0 ? b.name : "", approach: b.name, spawnedBy: branchId, branchKey: b.key, winBranch: false
        };
        const s = b.settings || {};
        if (type === "hook_pick") node.config.hookStrategy = s.hook || "strongest";
        if (type === "clip") { node.config.lengths = [s.length || 30]; node.config.pacing = s.pacing || "tight_punch"; node.config.ending = s.ending || "loop"; node.config.n = 1; }
        if (type === "subtitle") { node.config.style = s.captions || "sentence"; node.config.hookTitle = s.hook_title ? "on" : "off"; }
        if (type === "crop") { node.config.aspects = [s.aspect || "9:16"]; node.config.channel = s.channel || ""; }
        node.output = { type: "text", title: recipeLine(s), body: "Not rendered yet. Render branches runs cutter v2." };
        pipeline.nodes.push(node);
        pipeline.edges.push({ id: uid("e"), from: prev, to: node.id });
        prev = node.id;
      });
    });
    n.output = { type: "branch", source: res.source, model: res.model, disclaimer: res.disclaimer, names: (res.branches || []).map((b) => b.name), winner: null };
    n.phase = "ok";
    render();
    selectNode(branchId);
    zoomTo([n, ...pipeline.nodes.filter((x) => x.spawnedBy === branchId)]);
    status((res.source === "llm" ? "Grok" : "Template fallback") + " built " + (res.branches || []).length + " branches. Edit any step, then Render branches.");
    saveLocalSoon();
  }
  function recipeLine(s) {
    if (!s) return "";
    return [s.length ? s.length + "s" : "", s.pacing, s.captions, s.aspect, s.hook].filter(Boolean).join(" · ");
  }
  function groupsOf(branchId) {
    const kids = pipeline.nodes.filter((n) => n.spawnedBy === branchId);
    const keys = [];
    kids.forEach((n) => { if (n.branchKey && keys.indexOf(n.branchKey) < 0) keys.push(n.branchKey); });
    return keys.map((key) => {
      const nodes = kids.filter((n) => n.branchKey === key);
      const head = nodes.find((n) => n.type === "hook_pick") || nodes[0];
      return { key, name: (head && head.approach) || key, nodes, settings: settingsFrom(nodes) };
    });
  }
  function settingsFrom(nodes) {
    const by = {}; nodes.forEach((n) => { by[n.type] = n; });
    const clip = by.clip, sub = by.subtitle, crop = by.crop, hook = by.hook_pick;
    const lengths = (clip && clip.config.lengths) || [30];
    const pacing = clip && clip.config.pacing && clip.config.pacing !== "any" ? clip.config.pacing : "tight_punch";
    const captions = sub && sub.config.style && sub.config.style !== "any" ? sub.config.style : "sentence";
    const aspect = (crop && crop.config.aspects && crop.config.aspects[0]) || "9:16";
    const channel = (crop && crop.config.channel) || ({ "9:16": "TikTok", "1:1": "X", "4:5": "LinkedIn", "16:9": "YouTube" }[aspect] || "TikTok");
    return { hook: (hook && hook.config.hookStrategy) || "strongest", length: Number(lengths[0]) || 30, pacing, captions, hook_title: !!(sub && sub.config.hookTitle === "on"), ending: (clip && clip.config.ending) || "loop", aspect, channel };
  }
  function upstreamIngest(node) {
    const ans = ancestors(node.id);
    const linked = pipeline.nodes.find((n) => ans.has(n.id) && n.type === "ingest");
    return linked || pipeline.nodes.find((n) => n.type === "ingest") || null;
  }
  function setBranchPhase(nodes, phase, note) {
    const recipe = recipeLine(settingsFrom(nodes));
    nodes.forEach((n) => {
      n.phase = phase === "error" ? "err" : (phase === "done" ? "ok" : "run");
      if (note && (n.type === "clip" || n.type === "hook_pick")) n.output = { type: "text", title: note, body: recipe };
      paint(n);
    });
  }
  async function renderBranches(branchId) {
    const n = nodeById(branchId); if (!n) return;
    const groups = groupsOf(branchId);
    if (!groups.length) { status("Build branches first."); return; }
    const ing = upstreamIngest(n);
    if (!ing) { status("Add an Ingest step so the cutter has a video."); return; }
    const body = { branches: groups.map((g) => ({ key: g.key, name: g.name, settings: g.settings })) };
    try {
      if (ing.config.source === "upload") {
        const file = pendingFiles[ing.id];
        if (!file) throw new Error("Choose the video again on Ingest.");
        const fd = new FormData(); fd.append("video", file);
        const up = await j("/api/branch/upload", { method: "POST", body: fd });
        body.token = up.token;
      } else if (ing.config.source === "sample") {
        if (!ing.config.sample) throw new Error("Pick a sample on Ingest.");
        body.sample = ing.config.sample;
      } else {
        if (!ing.config.run) throw new Error("Pick a saved run on Ingest.");
        body.run = ing.config.run;
      }
    } catch (e) { status(e.message); return; }
    groups.forEach((g) => setBranchPhase(g.nodes, "queued", "Queued for cutter v2"));
    status("Queueing " + groups.length + " real renders, one at a time…");
    let queued;
    try { queued = await j("/api/branch/queue", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }); }
    catch (e) {
      groups.forEach((g) => setBranchPhase(g.nodes, "error", e.message));
      n.output = Object.assign({}, n.output, { type: "branch" });
      status(e.message); renderInspector(); return;
    }
    const byKey = {}; groups.forEach((g) => { byKey[g.key] = g; });
    for (let i = 0; i < 600; i++) {
      const st = await j("/api/branch/status?batch=" + encodeURIComponent(queued.batch));
      (st.items || []).forEach((item) => {
        const g = byKey[item.key]; if (!g || g.scored) return;
        if (item.status === "running") setBranchPhase(g.nodes, "run", "Rendering · " + (item.phase || "cutter v2"));
        if (item.status === "queued") setBranchPhase(g.nodes, "queued", "Queued · one render at a time");
        if (item.status === "error") setBranchPhase(g.nodes, "error", item.error || "Render failed");
      });
      const items = st.items || [];
      const runningItem = items.find((item) => item.status === "running");
      const settled = items.filter((item) => item.status === "done" || item.status === "error").length;
      if (n.output && n.output.type === "branch") {
        n.output.progress = (runningItem ? ("Rendering " + runningItem.name + " · " + (runningItem.phase || "cutter v2")) : "Waiting on the cutter") + " · " + settled + "/" + items.length + " finished";
        paint(n);
      }
      const doneItems = items.filter((item) => item.status === "done" && byKey[item.key] && !byKey[item.key].scored);
      for (const item of doneItems) await scoreBranch(byKey[item.key], item);
      if ((st.items || []).length && (st.items || []).every((item) => item.status === "done" || item.status === "error")) break;
      await new Promise((r) => setTimeout(r, 1000));
    }
    highlightWinner(branchId);
    render(); selectNode(branchId); saveLocalSoon();
  }
  function hookRequest(node, extra) {
    const c = (node && node.config) || {};
    const req = Object.assign({ mode: c.mode || "auto", n: 3 }, extra || {});
    if (c.channels && c.channels.length) req.channels = c.channels.slice();
    return req;
  }
  async function scoreBranch(g, item) {
    g.scored = true;
    const manifest = await fetchJsonOptional(item.manifest_url);
    const tagged = manifest ? Object.assign({}, manifest, { variant_id: g.key, id: g.key }) : { variant_id: g.key, id: g.key };
    g.nodes.forEach((node) => { node.phase = "ok"; paint(node); });
    const clip = g.nodes.find((n) => n.type === "clip");
    if (clip) {
      clip.output = { type: "text", title: item.video ? "Rendered " + (item.variant_id || "v01") : "Rendered", body: "Real cutter v2 file. Open it below.", video: item.video || "" };
      paint(clip);
    }
    const persona = g.nodes.find((n) => n.type === "persona_score");
    const hooks = g.nodes.find((n) => n.type === "hook_rewrite");
    if (persona) { persona.phase = "run"; persona.output = { type: "text", title: "Scoring…", body: "" }; paint(persona); }
    let response = null;
    try {
      response = await window.ClipCutPersona.score({ manifests: [tagged], mode: (persona && persona.config.mode) || "auto" });
    } catch (e) {
      response = window.ClipCutPersona.assembleStub([{ id: "v01" }], e.message);
    }
    if (persona) { persona.phase = "ok"; persona.output = { type: "persona", response }; paint(persona); }
    if (hooks) { hooks.phase = "run"; paint(hooks); }
    let hres = null;
    try { hres = await window.ClipCutHooks.rewrite(hookRequest(hooks, { manifests: [tagged] })); }
    catch (e) { hres = window.ClipCutHooks.assembleStub([{ id: "v01", hook: "" }], e.message); }
    if (hooks) { hooks.phase = "ok"; hooks.output = { type: "hooks", response: hres }; paint(hooks); }
    g.persona = response;
    status("Scored " + g.name + " (" + (response.source || "stub") + ").");
  }
  function highlightWinner(branchId) {
    const n = nodeById(branchId); if (!n) return;
    const groups = groupsOf(branchId);
    let best = null;
    groups.forEach((g) => {
      const resp = g.persona || ((g.nodes.find((x) => x.type === "persona_score") || {}).output || {}).response;
      const v = resp && ((resp.variants || []).find((x) => x.id === resp.winner) || (resp.variants || [])[0]);
      const agg = v && Number(v.aggregate);
      if (!Number.isFinite(agg)) return;
      if (!best || agg > best.agg || (agg === best.agg && g.key < best.key)) best = { key: g.key, name: g.name, agg, resp, variant: v };
    });
    if (!best) {
      pipeline.nodes.forEach((node) => { if (node.spawnedBy === branchId) node.winBranch = false; });
      status("No persona aggregate to rank. Check the branch errors.");
      return;
    }
    const top = (best.variant.personas || []).slice().sort((a, b) => Number(b.score) - Number(a.score))[0];
    const reasons = (top && top.reasons) || [];
    pipeline.nodes.forEach((node) => {
      const win = !!(node.spawnedBy === branchId && node.branchKey === best.key);
      node.winBranch = win;
      if (win && node.type === "hook_pick") {
        node.output = { type: "text", title: "Winner · aggregate " + fmtPersona(best.agg), body: reasons.slice(0, 2).join(" ") };
      }
    });
    const src = best.resp.source;
    n.output = Object.assign({}, n.output || {}, {
      type: "branch",
      progress: "",
      winner: {
        key: best.key, name: best.name, aggregate: best.agg,
        note: src === "stub"
          ? "Simulated stub ranking. Placeholder aggregate, not the persona heuristic and not the legacy channel score."
          : "Highest persona aggregate. Not the legacy channel score.",
        reasons
      }
    });
    status("Winning branch: " + best.name + " (aggregate " + fmtPersona(best.agg) + ", " + (src || "unknown") + ").");
  }

  function transcriptLines(tr) {
    if (!tr) return [];
    const arr = Array.isArray(tr) ? tr : (tr.segments || []);
    const lines = [];
    arr.forEach((item) => { if (item && typeof item.text === "string" && lines.length < 3) lines.push(item.text); });
    return lines;
  }
  async function pollCut(run, node) {
    for (let i = 0; i < 400; i++) {
      const s = await j("/api/run/" + encodeURIComponent(run));
      if (s.error) throw new Error(s.error);
      node.output = { type: "ingest", runId: run, note: `Rendering ${s.rendered || 0}/${s.n || "?"} · scored ${s.scored || 0}` };
      node.phase = "run"; paint(node);
      if (!s.running && s.results) return s;
      await new Promise((r) => setTimeout(r, 2000));
    }
    throw new Error("Timed out waiting for the cut.");
  }
  async function resolveIngest(n) {
    const c = n.config;
    const clip = pipeline.nodes.find((x) => x.type === "clip" && !x.spawnedBy);
    const count = Math.max(2, Math.min(20, Number((clip && clip.config.n) || c.n) || 12));
    if (c.source === "run") {
      if (!c.run) throw new Error("Ingest has no saved run.");
      const runState = await j("/api/run/" + encodeURIComponent(c.run));
      if (!runState.results) throw new Error("That run has no scored results yet.");
      const manifest = await fetchJsonOptional("/runs/" + encodeURIComponent(c.run) + "/manifest.json");
      const transcript = await fetchJsonOptional("/runs/" + encodeURIComponent(c.run) + "/transcript.json");
      return { runId: c.run, runState, manifest, lines: transcriptLines(transcript), variants: runState.results.slice(), fresh: false };
    }
    const st = await j("/api/status");
    if (st.busy) throw new Error("Busy: " + st.run + " is already rendering. One job at a time.");
    const fd = new FormData(); fd.append("n", String(count));
    if (c.source === "sample") {
      if (!c.sample) throw new Error("Pick a sample.");
      fd.append("sample", c.sample);
    } else {
      const file = pendingFiles[n.id];
      if (!file) throw new Error("Choose a video file on Ingest.");
      fd.append("video", file);
    }
    const started = await j("/api/cut", { method: "POST", body: fd });
    const runState = await pollCut(started.run, n);
    return { runId: started.run, runState, manifest: await fetchJsonOptional("/runs/" + started.run + "/manifest.json"), lines: [], variants: runState.results.slice(), fresh: true };
  }

  function viewText(title, body) { return { type: "text", title, body }; }

  async function execLinear(n, packet) {
    const c = n.config;
    if (n.type === "transcribe") {
      const m = packet.manifest || {};
      return { output: { type: "text", title: m.transcriber || c.engine || "Transcript", body: (packet.lines || []).join(" "), lines: packet.lines, note: "From the saved run. Changing the engine here does not re-transcribe." } };
    }
    if (n.type === "hook_pick") {
      const map = new Map();
      packet.variants.forEach((v) => {
        const id = (v.recipe && v.recipe.hook) || "hook";
        if (!map.has(id)) map.set(id, { id, text: v.hook_phrase || v.hook_text || "", n: 0 });
        map.get(id).n++;
      });
      let moments = [...map.values()].slice(0, Math.max(1, Number(c.n) || 5));
      const ids = new Set(moments.map((m) => m.id));
      let variants = packet.variants.filter((v) => ids.has((v.recipe && v.recipe.hook) || "hook"));
      if (!variants.length) variants = packet.variants;
      return { output: viewText(moments.length + " hooks", moments.map((m) => m.id + " “" + m.text + "”").join(" · ")), packet: Object.assign({}, packet, { variants }) };
    }
    if (n.type === "clip") {
      let variants = packet.variants;
      const want = new Set((c.lengths || []).map(Number));
      if (want.size) {
        const f = variants.filter((v) => want.has(Number(v.recipe && v.recipe.length)));
        if (f.length) variants = f;
      }
      if (c.pacing && c.pacing !== "any") {
        const f = variants.filter((v) => v.recipe && v.recipe.pacing === c.pacing);
        if (f.length) variants = f;
      }
      const preview = variants.slice(0, 3).map((v) => v.variant_id + " " + Math.round(v.duration_s || 0) + "s").join(" · ");
      return { output: viewText(variants.length + " clips", preview + " · Legacy batch ranks stay on this step only, not next to persona aggregate."), packet: Object.assign({}, packet, { variants }) };
    }
    if (n.type === "subtitle") {
      let variants = packet.variants;
      if (c.style && c.style !== "any") {
        const f = variants.filter((v) => v.recipe && v.recipe.captions === c.style);
        if (f.length) variants = f;
      }
      return { output: viewText("Captions " + (c.style || "any"), variants.length + " clips match. This filters a finished run; it does not re-burn captions."), packet: Object.assign({}, packet, { variants }) };
    }
    if (n.type === "crop") {
      let variants = packet.variants;
      const want = new Set(c.aspects || []);
      if (want.size) {
        const f = variants.filter((v) => want.has(v.aspect || (v.recipe && v.recipe.aspect)));
        if (f.length) variants = f;
      }
      return { output: viewText("Formats", [...new Set(variants.map((v) => v.aspect || "9:16"))].join(", ")), packet: Object.assign({}, packet, { variants }) };
    }
    if (n.type === "persona_score") {
      const all = ((packet.runState && packet.runState.results) || []).map((v) => v.variant_id);
      const ids = packet.variants.map((v) => v.variant_id);
      const subset = ids.length && ids.length < all.length ? ids : undefined;
      const response = await window.ClipCutPersona.score({ run: packet.runId, mode: c.mode || "auto", variant_ids: subset });
      return { output: { type: "persona", response }, packet: Object.assign({}, packet, { persona: response }) };
    }
    if (n.type === "hook_rewrite") {
      const response = await window.ClipCutHooks.rewrite(hookRequest(n, { run: packet.runId }));
      return { output: { type: "hooks", response }, packet: Object.assign({}, packet, { hooks: response }) };
    }
    if (n.type === "channel_test") {
      let report = null, abError = "";
      try { report = await j("/api/channels/" + encodeURIComponent(packet.runId)); }
      catch (e) { abError = e.message; }
      const want = new Set(c.channels || []);
      const persona = packet.persona;
      const winner = persona && ((persona.variants || []).find((v) => v.id === persona.winner) || (persona.variants || [])[0]);
      const fits = ((winner && winner.channels) || []).filter((ch) => !want.size || want.has(String(ch.id || "").toLowerCase()));
      const ab = ((report && report.blocks) || []).filter((b) => !want.size || want.has(String(b.channel || "").toLowerCase())).slice(0, 5).map((b) => ({
        channel: b.channel, winner: b.winner, note: b.ab_source || "Simulated",
        items: (b.items || []).slice(0, 4).map((i) => ({ id: i.variant_id, pBest: i.ab && i.ab.p_best, watch: i.ab && i.ab.watch_pct, views: i.ab && i.ab.views }))
      }));
      return { output: { type: "channel", fits, fitFor: winner && winner.id, ab, abError }, packet: Object.assign({}, packet, { channelReport: report }) };
    }
    if (n.type === "pick_winner") {
      let chosen;
      if (c.prefer === "channel") {
        const blocks = (packet.channelReport && packet.channelReport.blocks) || [];
        const b = blocks.find((x) => String(x.channel || "").toLowerCase() === String(c.channel || "").toLowerCase()) || blocks[0];
        chosen = { type: "winner", id: b && b.winner, note: "Simulated A/B winner" + (b ? " on " + b.channel : "") + ". Not the persona aggregate." };
      } else if (c.prefer === "run") {
        chosen = { type: "winner", id: packet.runState && packet.runState.winner, note: "Legacy run winner (captioned clip preferred on a near-tie). Different rule from persona aggregate." };
      } else {
        const p = packet.persona || {};
        const v = (p.variants || []).find((x) => x.id === p.winner);
        chosen = { type: "winner", id: p.winner, aggregate: v && v.aggregate, note: "Persona response winner, ranked by aggregate (" + (p.source || "unknown") + ")." };
      }
      return { output: chosen, packet: Object.assign({}, packet, { chosen }) };
    }
    if (n.type === "publish") {
      const ans = ancestors(n.id);
      const hasClip = [...ans].some((id) => { const x = nodeById(id); return x && x.type === "clip"; });
      if (!hasClip) return { output: { type: "publish", note: "Skipped. Publish is not downstream of a clip, so nothing was copied." } };
      try {
        const d = await j("/api/deploy/" + encodeURIComponent(packet.runId), { method: "POST" });
        const personaId = packet.chosen && packet.chosen.id;
        let note = "Copied " + d.winner + " locally. Nothing was posted to a platform.";
        if (personaId && personaId !== d.winner) note += " Persona winner was " + personaId + ". Deploy uses the legacy file, not the persona aggregate.";
        return { output: { type: "publish", url: d.url, note } };
      } catch (e) {
        return { output: { type: "publish", error: e.message, note: "Deploy did not copy a file. The mp4 may be missing from this checkout." } };
      }
    }
    return { output: viewText(TYPES[n.type].label, "Nothing to show.") };
  }

  function topo(nodes) {
    const ids = new Set(nodes.map((n) => n.id));
    const indeg = {}; nodes.forEach((n) => { indeg[n.id] = 0; });
    pipeline.edges.forEach((e) => { if (ids.has(e.from) && ids.has(e.to)) indeg[e.to]++; });
    const q = nodes.filter((n) => !indeg[n.id]).map((n) => n.id);
    const out = [];
    while (q.length) {
      const id = q.shift(); out.push(id);
      pipeline.edges.forEach((e) => { if (e.from === id && ids.has(e.to)) { indeg[e.to]--; if (!indeg[e.to]) q.push(e.to); } });
    }
    nodes.forEach((n) => { if (out.indexOf(n.id) < 0) out.push(n.id); });
    return out;
  }

  async function runPipeline() {
    if (running) return;
    running = true; const token = ++runToken;
    $("#runbtn").disabled = true;
    try {
      const main = pipeline.nodes.filter((n) => !n.spawnedBy);
      const order = topo(main);
      const ingest = order.map(nodeById).find((n) => n && n.type === "ingest");
      if (!ingest) { status("Add an Ingest step first."); return; }
      status("Running the main pipeline…");
      ingest.phase = "run"; paint(ingest);
      const base = await resolveIngest(ingest);
      if (token !== runToken) return;
      ingest.phase = "ok";
      ingest.output = { type: "ingest", runId: base.runId, note: (base.variants.length) + " variants" + (base.fresh ? " just rendered." : " from the saved run.") };
      paint(ingest);
      const packets = new Map(); packets.set(ingest.id, base);
      for (const id of order) {
        if (token !== runToken) return;
        const n = nodeById(id);
        if (!n || n.type === "ingest" || n.type === "branch" || n.spawnedBy) continue;
        const incoming = pipeline.edges.filter((e) => e.to === id);
        const up = incoming.map((e) => packets.get(e.from)).filter(Boolean)[0];
        if (!up) { n.output = { type: "empty", message: "Connect this step, then run again." }; n.phase = "idle"; paint(n); continue; }
        n.phase = "run"; paint(n);
        try {
          const result = await execLinear(n, up);
          n.output = result.output; n.phase = "ok";
          packets.set(id, result.packet || up);
        } catch (err) {
          n.output = { type: "error", message: err.message || String(err) }; n.phase = "err";
          packets.set(id, up);
        }
        paint(n);
      }
      const branches = pipeline.nodes.filter((n) => n.type === "branch");
      for (const b of branches) {
        if (token !== runToken) return;
        if (groupsOf(b.id).length) await renderBranches(b.id);
      }
      status("Run finished.");
      renderInspector(); saveLocalSoon();
    } catch (err) {
      status(err.message || String(err));
    } finally {
      running = false; $("#runbtn").disabled = false;
    }
  }

  const TOUR = [
    { title: "One video, many cuts", body: "ClipCut turns one long talking-head into short variants, scores four viewer personas, and copies a winner locally. It does not post to a platform. This canvas is that pipeline." },
    { sel: "#addbtn", title: "Add a step", body: "Use + Add step, click Add on the left, or right-click the canvas. That is how a new block gets onto the graph." },
    { sel: ".pal-item.star", title: "Try approaches", body: "Type a sentence like “a fast punchy cut, a calm storytelling cut, and big captions.” Build branches fans out a real recipe for each one. Grok is used when an API key exists; otherwise a keyword template, and the canvas says which." },
    { sel: ".port.out", title: "Connect", body: "Drag from a circle on the right to a circle on the left. Click a wire and press Delete to disconnect. An amber wire is a warning, such as publish before clip. You can leave it while you experiment." },
    { sel: "#inspector", title: "Configure", body: "Click a step. The panel explains every setting. Persona scores show their source and disclaimer. Channel tests are labelled simulated. Persona aggregate is not the legacy channel score." },
    { sel: "#runbtn", title: "Run", body: "Run replays a saved run through the main chain, or starts a full cut from an upload. Render branches on Try approaches queues cutter v2, one video at a time, then scores each result." }
  ];
  let tourIndex = 0;
  function showTour(i) {
    tourIndex = i;
    const step = TOUR[i];
    const card = $("#tour"); card.hidden = false;
    $("#tourtitle").textContent = step.title;
    $("#tourbody").textContent = step.body;
    $("#toursteps").textContent = (i + 1) + " / " + TOUR.length;
    $("#tournext").textContent = i === TOUR.length - 1 ? "Done" : "Next";
    const spot = $("#spot");
    if (!step.sel) { spot.hidden = true; card.style.left = "50%"; card.style.top = "45%"; card.style.transform = "translate(-50%,-50%)"; return; }
    const el = document.querySelector(step.sel);
    if (!el) { spot.hidden = true; return; }
    const r = el.getBoundingClientRect();
    spot.hidden = false;
    spot.style.left = (r.left - 6) + "px"; spot.style.top = (r.top - 6) + "px";
    spot.style.width = (r.width + 12) + "px"; spot.style.height = (r.height + 12) + "px";
    card.style.transform = "none";
    let left = Math.min(r.left, innerWidth - 380), top = r.bottom + 12;
    if (top > innerHeight - 200) top = Math.max(12, r.top - 200);
    card.style.left = Math.max(12, left) + "px"; card.style.top = top + "px";
  }
  function endTour() { $("#tour").hidden = true; $("#spot").hidden = true; localStorage.setItem(TOUR_KEY, "1"); }

  function bind() {
    renderPalette();
    $("#pal").addEventListener("click", (e) => { const b = e.target.closest("[data-type]"); if (b) addNode(b.dataset.type); });
    $("#addbtn").addEventListener("click", (e) => { e.stopPropagation(); const r = e.currentTarget.getBoundingClientRect(); openMenu(r.left, r.bottom + 6, null, null); });
    $("#emptyadd").addEventListener("click", () => { const r = $("#addbtn").getBoundingClientRect(); openMenu(r.left, r.bottom + 6, null, null); });
    $("#menu").addEventListener("click", (e) => {
      const add = e.target.closest("[data-add]");
      const rem = e.target.closest("[data-remove]");
      if (add) addNode(add.dataset.add, menuAt);
      if (rem) { removeNode(rem.dataset.remove); hideMenu(); }
    });
    document.addEventListener("pointerdown", (e) => { if (!e.target.closest("#menu") && !e.target.closest("#addbtn")) hideMenu(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { hideMenu(); endTour(); }
      if (e.target.closest("input,textarea,select")) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        if (selectedEdge) removeEdge(selectedEdge);
        else if (selectedId) removeNode(selectedId);
      }
    });
    vp.addEventListener("contextmenu", (e) => {
      if (e.target.closest("textarea,input")) return;
      e.preventDefault();
      const node = e.target.closest(".node");
      const at = clientToWorld(e.clientX, e.clientY);
      openMenu(e.clientX, e.clientY, at, node ? node.dataset.id : null);
    });
    world.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".x")) return;
      const port = e.target.closest(".port");
      if (port) { e.stopPropagation(); startConnect(port, e); return; }
      const nodeEl = e.target.closest(".node");
      if (!nodeEl) return;
      e.stopPropagation();
      const id = nodeEl.dataset.id;
      selectNode(id);
      const n = nodeById(id); if (!n) return;
      const sx = e.clientX, sy = e.clientY, ox = n.x, oy = n.y;
      function mv(ev) {
        if (Math.hypot(ev.clientX - sx, ev.clientY - sy) < 3) return;
        n.x = ox + (ev.clientX - sx) / T.k; n.y = oy + (ev.clientY - sy) / T.k;
        nodeEl.style.left = n.x + "px"; nodeEl.style.top = n.y + "px"; wires();
      }
      function up() { removeEventListener("pointermove", mv); removeEventListener("pointerup", up); saveLocalSoon(); }
      addEventListener("pointermove", mv); addEventListener("pointerup", up);
    });
    world.addEventListener("click", (e) => { const x = e.target.closest(".x"); if (x) { e.stopPropagation(); removeNode(x.closest(".node").dataset.id); } });
    svg.addEventListener("click", (e) => { const id = e.target.getAttribute && e.target.getAttribute("data-edge"); if (id) { e.stopPropagation(); selectEdge(id); } });
    vp.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".node") || e.target.closest(".port") || e.target.closest(".hit") || e.target.closest("#empty") || e.target.closest("#mini")) return;
      const sx = e.clientX, sy = e.clientY, ox = T.x, oy = T.y;
      vp.classList.add("panning");
      function mv(ev) { T.x = ox + ev.clientX - sx; T.y = oy + ev.clientY - sy; apply(); }
      function up() { vp.classList.remove("panning"); removeEventListener("pointermove", mv); removeEventListener("pointerup", up); }
      addEventListener("pointermove", mv); addEventListener("pointerup", up);
    });
    vp.addEventListener("click", (e) => { if (e.target.closest(".node") || e.target.closest(".hit") || e.target.closest("#empty")) return; selectNode(null); });
    vp.addEventListener("wheel", (e) => { e.preventDefault(); const r = vp.getBoundingClientRect(); zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.001)); }, { passive: false });
    $("#zin").onclick = () => zoomAt(vp.clientWidth / 2, vp.clientHeight / 2, 1.15);
    $("#zout").onclick = () => zoomAt(vp.clientWidth / 2, vp.clientHeight / 2, 0.87);
    $("#fitbtn").onclick = fit;
    $("#runbtn").onclick = runPipeline;
    $("#resetbtn").onclick = () => { pipeline = builtin(); selectedId = null; render(); fit(); saveLocalSoon(); status("Reset to the default pipeline."); };
    $("#emptyreset").onclick = $("#resetbtn").onclick;
    $("#savebtn").onclick = async () => {
      const slug = (pipeline.name || "pipeline").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "pipeline";
      try {
        await j("/api/pipelines/" + slug, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(snapshot(false)) });
        status("Saved on this server as " + slug + " and in this browser.");
        await refreshLists();
      } catch (e) { status(e.message); }
    };
    $("#loadsel").onchange = async (e) => {
      const name = e.target.value; e.target.value = "";
      if (!name) return;
      if (name === "__builtin__") { pipeline = builtin(); }
      else { try { pipeline = normalize(await j("/api/pipelines/" + name)); } catch (err) { status(err.message); return; } }
      selectedId = null; render(); fit(); saveLocalSoon();
    };
    $("#pname").addEventListener("input", () => { pipeline.name = $("#pname").value; saveLocalSoon(); });
    $("#warnbtn").onclick = () => { const list = allIssues(); if (list[0] && list[0].nodeId) selectNode(list[0].nodeId); else if (list[0]) selectEdge(list[0].edgeId); };
    $("#cutwire").onclick = () => { if (selectedEdge) removeEdge(selectedEdge); };
    insp.addEventListener("input", onInsp);
    insp.addEventListener("change", onInsp);
    $("#tourbtn").onclick = () => showTour(0);
    $("#tournext").onclick = () => { if (tourIndex >= TOUR.length - 1) endTour(); else showTour(tourIndex + 1); };
    $("#tourback").onclick = () => showTour(Math.max(0, tourIndex - 1));
    $("#tourskip").onclick = endTour;
    const tip = $("#tip");
    document.addEventListener("pointerover", (e) => {
      const t = e.target.closest && e.target.closest("[data-tip]");
      if (!t) { tip.hidden = true; return; }
      tip.hidden = false; tip.textContent = t.getAttribute("data-tip");
    });
    document.addEventListener("pointermove", (e) => {
      if (tip.hidden) return;
      tip.style.left = Math.min(e.clientX + 12, innerWidth - 300) + "px";
      tip.style.top = Math.min(e.clientY + 16, innerHeight - 80) + "px";
    });
    document.addEventListener("pointerout", (e) => { if (e.target.closest && e.target.closest("[data-tip]")) tip.hidden = true; });
    const mc = $("#mini");
    mc.addEventListener("pointerdown", (e) => { const m = mc._m; if (!m) return; const r = mc.getBoundingClientRect(); const wx = ((e.clientX - r.left) * mc.width / r.width - m.pad) / m.s + m.x0; const wy = ((e.clientY - r.top) * mc.height / r.height - m.pad) / m.s + m.y0; T.x = vp.clientWidth / 2 - wx * T.k; T.y = vp.clientHeight / 2 - wy * T.k; apply(); });
  }

  function startConnect(port, e) {
    const fromId = port.closest(".node").dataset.id;
    const dir = port.dataset.dir;
    connectState = { id: fromId, dir, d: "" };
    function mv(ev) {
      const a = portWorld(fromId, dir); const b = clientToWorld(ev.clientX, ev.clientY);
      if (!a) return;
      const from = dir === "out" ? a : b, to = dir === "out" ? b : a;
      connectState.d = curve(from, to); wires();
    }
    function up(ev) {
      removeEventListener("pointermove", mv); removeEventListener("pointerup", up);
      connectState = null; wires();
      const hit = portAt(ev.clientX, ev.clientY);
      if (!hit) return;
      const toId = hit.closest(".node").dataset.id;
      const toDir = hit.dataset.dir;
      let src = fromId, dst = toId;
      if (dir === "in" && toDir === "out") { src = toId; dst = fromId; }
      else if (dir === "out" && toDir === "in") { src = fromId; dst = toId; }
      else if (dir === "out") { src = fromId; dst = toId; }
      else { src = toId; dst = fromId; }
      addEdge(src, dst);
    }
    addEventListener("pointermove", mv); addEventListener("pointerup", up);
  }
  function portAt(x, y) {
    const stack = document.elementsFromPoint(x, y);
    for (const el of stack) { const p = el.closest && el.closest(".port"); if (p) return p; }
    let best = null, bestD = 28;
    document.querySelectorAll(".port").forEach((p) => {
      const r = p.getBoundingClientRect();
      const d = Math.hypot(x - (r.left + r.width / 2), y - (r.top + r.height / 2));
      if (d < bestD) { best = p; bestD = d; }
    });
    return best;
  }

  async function refreshLists() {
    try {
      const [runs, samples, chans, pipes] = await Promise.all([
        j("/api/runs"), j("/api/samples"), j("/api/list_channels"), j("/api/pipelines")
      ]);
      catalog = { runs, samples, channels: chans.channels || [], pipelines: pipes.pipelines || [] };
      const sel = $("#loadsel");
      sel.innerHTML = `<option value="">Load…</option><option value="__builtin__">Default pipeline</option>` +
        catalog.pipelines.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    } catch (e) { status("API not fully reachable yet. You can still edit the graph. " + e.message); }
  }

  function init() {
    bind();
    pipeline = loadLocal() || builtin();
    render();
    requestAnimationFrame(() => { wires(); fit(); });
    refreshLists();
    window.CC = { run: runPipeline, addNode, issues: allIssues, get pipeline() { return pipeline; } };
    window.READY = true;
    if (!localStorage.getItem(TOUR_KEY)) setTimeout(() => showTour(0), 400);
  }
  init();
})();
