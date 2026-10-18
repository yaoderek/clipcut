/* Thin client for the live POST /api/persona/hooks route (docs/PERSONA_API.md).
   Request: {run} or {manifests}, optional variant_ids, personas, channels, n (1–4, default 3), mode.
   A 200 body with `variants` is returned as-is. source is "heuristic" (template lines),
   "llm" (Grok, needs XAI_API_KEY or GROK_API_KEY), or "mixed".
   Each suggestion: hook, title_card, reason, predicted_delta, predicted_delta_label
   "heuristic_estimate", origin. On 400/404/503/network errors the client returns source "stub". */
(function (root) {
  var PERSONAS = ["fast_swiper", "casual_browser", "completionist", "sharer_engager"];
  var CHANNELS = ["tiktok", "reels", "shorts", "x", "linkedin", "youtube", "ig_feed", "ig_stories", "fb_reels", "fb_feed"];
  var DISCLAIMER_REAL = "These hook suggestions are template rewrites, or LLM-simulated lines when source is \"llm\" or \"mixed\". They are not trained and they are not validated on real viewers. predicted_delta is a heuristic rescore of the rewritten opening with the persona scorer (predicted_delta_label is \"heuristic_estimate\"), not a measured lift.";

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

  function titleFrom(hook) {
    var words = String(hook || "").replace(/[^\w\s']/g, " ").split(/\s+/).filter(Boolean);
    if (words.length < 2) return "";
    return words.slice(-2).join(" ").toUpperCase();
  }

  function metasFrom(req) {
    var list = [];
    (req.manifests || []).forEach(function (m, i) {
      var hook = "";
      if (m) hook = m.hook_text || m.hook_phrase || "";
      if (!hook && m && Array.isArray(m.captions)) {
        var cue = m.captions.filter(function (c) { return c && Number(c.start) <= 8 && c.text; })[0];
        if (cue) hook = cue.text;
      }
      list.push({
        id: (m && (m.variant_id || m.id)) || ("m" + String(i + 1).padStart(2, "0")),
        hook: String(hook || "").slice(0, 280)
      });
    });
    if (req.variant_ids && req.variant_ids.length) {
      var have = {};
      list.forEach(function (m) { have[m.id] = m; });
      list = req.variant_ids.map(function (id) { return have[id] || { id: id, hook: "" }; });
    }
    if (!list.length) list = [{ id: "v01", hook: "" }];
    return list;
  }

  function wantedPersonas(req) {
    var list = req && req.personas && req.personas.length ? req.personas : PERSONAS;
    return list.filter(function (id, i) { return PERSONAS.indexOf(id) >= 0 && list.indexOf(id) === i; });
  }

  function wantedChannels(req) {
    if (!req || !req.channels || !req.channels.length) return [null];
    return req.channels.filter(function (id, i) { return CHANNELS.indexOf(id) >= 0 && req.channels.indexOf(id) === i; });
  }

  function wantedN(req) {
    var n = req && req.n != null ? Number(req.n) : 3;
    if (!isFinite(n)) n = 3;
    n = Math.round(n);
    if (n < 1) n = 1;
    if (n > 4) n = 4;
    return n;
  }

  function framesFor(hook) {
    var line = String(hook || "").replace(/[.?!]+$/, "");
    return [
      { hook: "Wait — did you know " + line + "?", why: "Question-shaped placeholder." },
      { hook: "One thing: " + line + ".", why: "Number-first placeholder. The word \"one\" is not a count from the video." },
      { hook: "Stop — " + line + ".", why: "Claim-shaped placeholder." },
      { hook: "You — " + line + ".", why: "Direct-address placeholder." }
    ];
  }

  function assembleStub(metas, reason, req) {
    var why = reason ? String(reason) : "the hook route did not answer";
    var personas = wantedPersonas(req);
    var channels = wantedChannels(req);
    var n = wantedN(req);
    if (!personas.length) personas = PERSONAS.slice();
    return {
      source: "stub",
      model_version: "canvas-hooks-stub",
      disclaimer: "Simulated stub. POST /api/persona/hooks was unavailable (" + why + "). These lines are not the template rewriter and not Grok. predicted_delta values are placeholders, not a heuristic rescore (predicted_delta_label \"heuristic_estimate\") and not a measured lift.",
      variants: (metas || []).map(function (m) {
        var suggestions = [];
        var card = titleFrom(m.hook);
        personas.forEach(function (persona) {
          channels.forEach(function (channel) {
            if (!m.hook) {
              for (var k = 0; k < n; k++) {
                var empty = {
                  persona: persona,
                  hook: "",
                  reason: "Simulated stub. No opening line on this variant, so nothing was invented.",
                  predicted_delta: 0,
                  predicted_delta_label: "stub_placeholder",
                  origin: "stub"
                };
                if (channel) empty.channel = channel;
                suggestions.push(empty);
              }
              return;
            }
            framesFor(m.hook).slice(0, n).forEach(function (frame, i) {
              var row = {
                persona: persona,
                hook: frame.hook,
                title_card: card,
                reason: "Simulated stub for " + persona.replace(/_/g, " ") + ". " + frame.why + " Not a template rewrite and not a Grok line.",
                predicted_delta: round4((hash01(m.id + ":" + persona + ":" + (channel || "") + ":" + i) - 0.45) * 0.12),
                predicted_delta_label: "stub_placeholder",
                origin: "stub"
              };
              if (channel) row.channel = channel;
              suggestions.push(row);
            });
          });
        });
        return { id: m.id, original_hook: m.hook || "", suggestions: suggestions };
      })
    };
  }

  async function loadRunMetas(run) {
    if (!run || typeof fetch !== "function") return [];
    try {
      var r = await fetch("/api/run/" + encodeURIComponent(run));
      if (!r.ok) return [];
      var body = await r.json();
      return (body.results || []).map(function (v) {
        return { id: v.variant_id, hook: v.hook_phrase || v.hook_text || "" };
      });
    } catch (e) { return []; }
  }

  function buildPayload(req) {
    var payload = {};
    if (req.manifests) payload.manifests = req.manifests;
    else payload.run = req.run;
    if (req.variant_ids && req.variant_ids.length) payload.variant_ids = req.variant_ids;
    if (req.personas && req.personas.length) payload.personas = req.personas;
    if (req.channels && req.channels.length) payload.channels = req.channels;
    if (req.n != null && req.n !== "") payload.n = req.n;
    payload.mode = req.mode || "auto";
    return payload;
  }

  async function rewrite(req) {
    req = req || {};
    var payload = buildPayload(req);
    var metas = req.manifests ? metasFrom(req) : await loadRunMetas(req.run);
    if (req.variant_ids && req.variant_ids.length && !req.manifests) {
      var have = {};
      metas.forEach(function (m) { have[m.id] = m; });
      metas = req.variant_ids.map(function (id) { return have[id] || { id: id, hook: "" }; });
    }
    if (["heuristic", "llm", "auto"].indexOf(payload.mode) < 0) {
      if (!metas.length) metas = metasFrom(req);
      return assembleStub(metas, "mode must be heuristic, llm, or auto", req);
    }
    try {
      var r = await fetch("/api/persona/hooks", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload)
      });
      var text = await r.text();
      var body = null;
      try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
      if (r.ok && body && Array.isArray(body.variants)) return body;
      var msg = (body && (body.error || body.detail)) || (r.status + " " + r.statusText);
      if (r.status === 503) msg = "LLM mode needs an API key (XAI_API_KEY or GROK_API_KEY) (503). " + msg;
      if (!metas.length) metas = metasFrom(req);
      return assembleStub(metas, msg, req);
    } catch (e) {
      if (!metas.length) metas = metasFrom(req);
      return assembleStub(metas, (e && e.message) || "network error", req);
    }
  }

  root.ClipCutHooks = {
    rewrite: rewrite,
    assembleStub: assembleStub,
    PERSONAS: PERSONAS,
    CHANNELS: CHANNELS,
    DISCLAIMER_REAL: DISCLAIMER_REAL
  };
})(typeof window !== "undefined" ? window : globalThis);
