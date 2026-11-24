// Static demo: renders recorded scenario traces from demo/data.js. DOM built with textContent only.
(function () {
  "use strict";

  var data = window.GOV_DEMO;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function pill(ok, text) {
    return el("span", ok ? "pill pill-ok" : "pill pill-bad", text);
  }

  function statePath(states) {
    var box = el("div", "path");
    states.forEach(function (state, i) {
      if (i > 0) box.appendChild(el("span", "arrow", "→"));
      box.appendChild(el("span", "state state-" + state, state));
    });
    return box;
  }

  function card(title, lines, warn) {
    var box = el("div", warn ? "card warn" : "card");
    box.appendChild(el("b", null, title));
    lines.forEach(function (line) { box.appendChild(el("div", null, line)); });
    return box;
  }

  function stepsTable(steps) {
    var wrap = el("div", "table-wrap");
    var table = el("table", "steps");
    var head = el("tr");
    ["#", "Command", "Actor", "Outcome", "State", "Control-plane reason"].forEach(function (h) {
      head.appendChild(el("th", null, h));
    });
    var thead = el("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    var body = el("tbody");
    steps.forEach(function (s) {
      var row = el("tr");
      row.appendChild(el("td", "mono", s.n));
      row.appendChild(el("td", "mono", s.do));
      row.appendChild(el("td", "mono", s.actor));
      var outcome = el("td");
      outcome.appendChild(s.outcome === "ok" ? pill(true, "applied") : pill(false, s.error_code));
      if (s.expect !== "ok" || !s.passed) {
        outcome.appendChild(el("div", "expect", s.passed ? "refusal expected" : "unexpected (expected " + s.expect + ")"));
      }
      row.appendChild(outcome);
      var state = el("td", "mono", (s.state_before || "-") + " → " + (s.state_after || "-"));
      row.appendChild(state);
      var msg = el("td", "msg");
      if (s.note) msg.appendChild(el("span", "note", s.note));
      msg.appendChild(el("span", null, s.message));
      row.appendChild(msg);
      body.appendChild(row);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  }

  function auditTable(entries) {
    var wrap = el("div", "table-wrap");
    var table = el("table", "audit");
    var head = el("tr");
    ["Seq", "Time (UTC)", "Event", "Actor", "Outcome", "From → to", "Entry hash"].forEach(function (h) {
      head.appendChild(el("th", null, h));
    });
    var thead = el("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    var body = el("tbody");
    entries.forEach(function (e) {
      var row = el("tr");
      row.appendChild(el("td", "mono", e.seq));
      row.appendChild(el("td", "mono", e.timestamp.replace("T", " ").replace("Z", "")));
      row.appendChild(el("td", "mono", e.event));
      row.appendChild(el("td", "mono", e.actor));
      var outcome = el("td");
      outcome.appendChild(pill(e.outcome === "applied", e.outcome));
      row.appendChild(outcome);
      row.appendChild(el("td", "mono", (e.from_state || "-") + " → " + (e.to_state || "-")));
      row.appendChild(el("td", "mono", e.entry_hash.split(":")[1].slice(0, 12)));
      body.appendChild(row);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  }

  function render(index) {
    var t = data.scenarios[index];
    var detail = document.getElementById("detail");
    detail.textContent = "";

    var head = el("div", "head");
    head.appendChild(el("h2", null, t.id + " — " + t.title));
    head.appendChild(pill(t.passed, t.passed ? "behaved as specified" : "unexpected behaviour"));
    detail.appendChild(head);
    detail.appendChild(el("p", "desc", t.description));

    var f = t.final;
    detail.appendChild(el("h3", null, "State path"));
    detail.appendChild(statePath(f.path));

    var req = t.request;
    var cards = el("div", "cards");
    var stepLines = req.steps.map(function (s) { return s.action + (s.path ? " " + s.path : ""); });
    cards.appendChild(card("Original request (" + req.requester + ")", [req.title].concat(stepLines,
      req.declared_risk ? ["declared risk: " + req.declared_risk] : [])));
    cards.appendChild(card("Classification", [
      "level: " + (f.level || "FORBIDDEN"),
      f.requirements ? "evidence: " + f.requirements.evidence.join(", ") : "no requirements (blocked)",
      f.requirements ? "human approvals: " + f.requirements.approvals : "",
      "revision " + f.revision + ", facts " + f.facts_fingerprint
    ]));
    if (f.approvals.length || f.grants.length) {
      cards.appendChild(card("Approvals and grants",
        f.approvals.map(function (a) { return a.approval_id + " " + a.approver + " [" + a.status + "] fp " + a.fingerprint; })
          .concat(f.grants.map(function (g) { return g.grant_id + " [" + g.status + "] fp " + g.fingerprint; }))));
    }
    if (f.release) {
      cards.appendChild(card("Release plan " + f.release.release_id + " (" + f.release.version + ")", [
        "stage: " + f.release.stage,
        "verification: " + f.release.verification.status,
        "deployment: " + f.release.deployment
      ], true));
    }
    if (f.execution) {
      var lines = ["status: " + f.execution.status, "changed: " + f.execution.changed_files.join(", ")];
      (f.execution.violations || []).forEach(function (v) { lines.push("violation: " + v); });
      cards.appendChild(card("Execution " + f.execution.execution_id, lines, f.execution.status !== "VERIFIED"));
    } else {
      cards.appendChild(card("Execution", ["never started: the worker was not invoked"]));
    }
    var integrity = t.integrity.problems.length ? t.integrity.problems : ["audit chain intact, task seals match"];
    cards.appendChild(card("Integrity check", integrity, t.integrity.problems.length > 0));
    detail.appendChild(el("h3", null, "Outcome"));
    detail.appendChild(cards);

    detail.appendChild(el("h3", null, "Steps"));
    detail.appendChild(stepsTable(t.steps));
    detail.appendChild(el("h3", null, "Audit trail for this task (hash-chained)"));
    detail.appendChild(auditTable(t.audit));

    document.querySelectorAll("#scenario-list button").forEach(function (b) {
      b.setAttribute("aria-current", b.dataset.index === String(index) ? "true" : "false");
    });
    if (window.history && window.history.replaceState) window.history.replaceState(null, "", "#" + t.id);
  }

  function init() {
    if (!data) {
      document.getElementById("detail").textContent = "demo/data.js is missing.";
      return;
    }
    var passed = data.scenarios.filter(function (s) { return s.passed; }).length;
    document.getElementById("summary-badge").textContent = passed + " of " + data.scenarios.length + " scenarios behaved as specified";
    var list = document.getElementById("scenario-list");
    var start = 0;
    data.scenarios.forEach(function (t, i) {
      if (window.location.hash === "#" + t.id) start = i;
      var item = el("li");
      var button = el("button");
      button.type = "button";
      button.dataset.index = String(i);
      button.appendChild(el("span", "sid", t.id));
      button.appendChild(el("span", "stitle", t.title));
      var end = el("span", "state state-" + t.final.state, "final " + t.final.state);
      button.appendChild(end);
      button.addEventListener("click", function () { render(i); });
      item.appendChild(button);
      list.appendChild(item);
    });
    render(start);
  }

  init();
})();
