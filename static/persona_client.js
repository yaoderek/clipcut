/* Thin client for POST /api/persona/score.
   The route is owned by the personas lane (docs/PERSONA_API.md on
   personas/explainable-scoring). This file does not implement that route
   and does not call POST /api/score.

   On 400 / 404 / 503 / network errors / a body without `variants`, it returns
   the same shape with source "stub". Stub numbers are placeholders. They are
   not the persona heuristic, not an LLM judgment, and not legacy channel
   scores (those use a different scale — never copy them into aggregate). */
(function (root) {
  var PERSONA_ORDER = ["fast_swiper", "casual_browser", "completionist", "sharer_engager"];
  var PERSONAS = [
    { id: "fast_swiper", label: "Fast swiper", description: "Younger swipe-feed viewer. Leaves unless the hook lands immediately and shots stay short." },
    { id: "casual_browser", label: "Casual browser", description: "Lean-back viewer, often muted. Captions and a mid length matter most." },
    { id: "completionist", label: "Completionist", description: "Will finish a longer informative clip. Rush cuts matter less than running on too long." },
    { id: "sharer_engager", label: "Sharer", description: "Forwards a claim-shaped, loopable clip. The opening line and the loop matter more than cut rate." }
  ];
  var WEIGHTS = { fast_swiper: 0.3, casual_browser: 0.3, completionist: 0.3, sharer_engager: 0.1 };
  var CHANNELS = [
    { id: "tiktok", label: "TikTok" },
    { id: "reels", label: "Reels" },
    { id: "shorts", label: "Shorts" },
    { id: "x", label: "X" },
    { id: "linkedin", label: "LinkedIn" },
    { id: "youtube", label: "YouTube" },
    { id: "ig_feed", label: "IG_Feed" },
    { id: "ig_stories", label: "IG_Stories" },
    { id: "fb_reels", label: "FB_Reels" },
    { id: "fb_feed", label: "FB_Feed" }
  ];
  var CONTRIB_KEYS = ["hook_timing", "hook_strength", "pacing", "captions", "length", "topic_match", "loop", "silence", "hook_title"];

  function round4(n) {
    return Math.round(Number(n) * 10000) / 10000;
  }

  function hash01(s) {
    var h = 2166136261;
    var str = String(s);
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0) / 4294967295;
  }

  function assembleStub(metas, reason) {
    var list = (metas || []).map(function (m, i) {
      return { id: (m && (m.id || m.variant_id)) || ("m" + String(i + 1).padStart(2, "0")) };
    });
    if (!list.length) list = [{ id: "v01" }];
    var variants = list.map(function (m) {
      var personas = PERSONAS.map(function (p) {
        var score = round4(0.35 + 0.4 * hash01(m.id + ":" + p.id));
        var contributions = {};
        CONTRIB_KEYS.forEach(function (k) {
          contributions[k] = round4((hash01(m.id + ":" + p.id + ":" + k) - 0.5) * 0.08);
        });
        return {
          id: p.id,
          label: p.label,
          description: p.description,
          score: score,
          reasons: ["Simulated stub placeholder. Not a persona heuristic and not a legacy score."],
          contributions: contributions
        };
      });
      var aggregate = 0;
      personas.forEach(function (p) { aggregate += p.score * WEIGHTS[p.id]; });
      aggregate = round4(aggregate);
      var channels = CHANNELS.map(function (c) {
        return {
          id: c.id,
          label: c.label,
          fit: round4(0.3 + 0.55 * hash01(m.id + ":ch:" + c.id)),
          reasons: ["Simulated stub fit. Not a legacy channel score and not predicted views."]
        };
      });
      channels.sort(function (a, b) {
        return (b.fit - a.fit) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
      });
      return { id: m.id, source: "stub", aggregate: aggregate, personas: personas, channels: channels };
    });
    var winner = variants[0].id;
    variants.forEach(function (v) {
      if (v.aggregate > variants.find(function (x) { return x.id === winner; }).aggregate) winner = v.id;
      else if (v.aggregate === variants.find(function (x) { return x.id === winner; }).aggregate && v.id < winner) winner = v.id;
    });
    var why = reason ? String(reason) : "the persona route did not return a score";
    return {
      source: "stub",
      model_version: "canvas-persona-stub",
      disclaimer: "Simulated stub. POST /api/persona/score was unavailable (" + why + "). These placeholder scores are not the persona heuristic, not an LLM judgment, and not the legacy /api/score channel score (different scale). They are not a trained model and not real platform results.",
      winner: winner,
      variants: variants
    };
  }

  function variantMetasFromRun(run) {
    var results = (run && run.results) || [];
    return results.map(function (v) { return { id: v.variant_id }; });
  }

  async function loadMetas(req) {
    if (req.manifests && req.manifests.length) {
      return req.manifests.map(function (m, i) {
        return { id: m.variant_id || m.id || ("m" + String(i + 1).padStart(2, "0")) };
      });
    }
    if (req.run && typeof fetch === "function") {
      try {
        var r = await fetch("/api/run/" + encodeURIComponent(req.run));
        if (r.ok) return variantMetasFromRun(await r.json());
      } catch (e) { /* fall through to a single placeholder */ }
    }
    return [];
  }

  function applyVariantFilter(metas, ids) {
    if (!ids || !ids.length) {
      return metas.slice().sort(function (a, b) { return a.id < b.id ? -1 : a.id > b.id ? 1 : 0; });
    }
    var have = {};
    metas.forEach(function (m) { have[m.id] = m; });
    return ids.map(function (id) { return have[id] || { id: id }; });
  }

  async function score(req) {
    req = req || {};
    var mode = req.mode || "auto";
    if (["heuristic", "llm", "auto"].indexOf(mode) < 0) {
      var metasBad = applyVariantFilter(await loadMetas(req), req.variant_ids);
      return assembleStub(metasBad, "mode must be heuristic, llm, or auto");
    }
    var payload = { mode: mode };
    if (req.manifests) payload.manifests = req.manifests;
    else payload.run = req.run;
    if (req.variant_ids && req.variant_ids.length) payload.variant_ids = req.variant_ids;
    try {
      var r = await fetch("/api/persona/score", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload)
      });
      var text = await r.text();
      var body = null;
      try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
      if (r.ok && body && Array.isArray(body.variants)) return body;
      var msg = (body && (body.error || body.detail)) || (r.status + " " + r.statusText);
      if (r.status === 503) msg = "LLM mode needs an API key (503). " + msg;
      var metas = applyVariantFilter(await loadMetas(req), req.variant_ids);
      return assembleStub(metas, msg);
    } catch (e) {
      var metasErr = applyVariantFilter(await loadMetas(req), req.variant_ids);
      return assembleStub(metasErr, (e && e.message) || "network error");
    }
  }

  root.ClipCutPersona = {
    score: score,
    assembleStub: assembleStub,
    PERSONA_ORDER: PERSONA_ORDER,
    CHANNELS: CHANNELS
  };
})(typeof window !== "undefined" ? window : globalThis);
