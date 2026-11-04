/* ToolNet Drama Admin — shared client layer.
   Shell: hamburger + drawer nav + topbar search. Auth+CSRF fetch, formatters,
   toasts, busy buttons, modal helper. No dependencies. */

window.TN = (function () {
  "use strict";

  var CSRF_KEY = "tn_csrf";
  var csrf = "";
  try { csrf = sessionStorage.getItem(CSRF_KEY) || ""; } catch (e) { csrf = ""; }

  function setCsrf(t) {
    csrf = t || "";
    try {
      if (csrf) sessionStorage.setItem(CSRF_KEY, csrf);
      else sessionStorage.removeItem(CSRF_KEY);
    } catch (e) { /* private mode */ }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function ts(t) {
    if (t == null || t === "") return "—";
    var d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
    if (isNaN(d.getTime())) return String(t);
    return d.toISOString().replace("T", " ").slice(0, 16) + "Z";
  }

  function bytes(n) {
    if (n == null) return "n/a";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }

  function num(n) {
    if (n == null) return "—";
    return Number(n).toLocaleString("en-US");
  }
  function capped(n, cap) { return cap && n >= 10000 ? "10,000+" : num(n); }
  function isOk(d) { return d && typeof d === "object" ? d.ok !== false : true; }

  async function api(path, opts) {
    opts = opts || {};
    var headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (csrf) headers["X-CSRF-Token"] = csrf;
    opts.headers = headers;
    opts.credentials = "include";
    var r = await fetch(path, opts);
    if (r.status === 401) { location.href = "/admin"; throw new Error("Session expired"); }
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok || j.ok === false) throw new Error((j && j.error) || ("HTTP " + r.status));
    return j;
  }

  async function me() {
    var j = await api("/admin/api/me");
    setCsrf(j.csrf_token);
    return j;
  }

  async function ensureCsrf() {
    if (!csrf) { try { await me(); } catch (e) { /* page load will handle */ } }
    return csrf;
  }

  function signOut() {
    fetch("/admin/api/logout", { method: "POST", credentials: "include" })
      .catch(function () {})
      .finally(function () {
        setCsrf("");
        location.href = "/admin";
      });
  }

  /* Shell: hamburger (left) + ToolNet Drama brand + search (right) + drawer nav. */
  function mountShell(active, opts) {
    opts = opts || {};
    var items = [
      ["overview", "/admin/overview", "Overview"],
      ["providers", "/admin/providers", "Providers"],
      ["keys", "/admin/keys", "API Keys"],
      ["cache", "/admin/cache", "Episode Cache"]
    ];
    var nav = items.map(function (it) {
      return '<a href="' + it[1] + '"' + (it[0] === active ? ' class="active"' : "") + ">" + esc(it[2]) + "</a>";
    }).join("");

    var header = document.createElement("header");
    header.className = "topbar";
    header.innerHTML =
      '<div class="topbar-inner">' +
        '<button type="button" class="icon-btn" id="tn-menu" aria-label="Open navigation">&#9776;</button>' +
        '<a class="brand" href="/admin/overview"><span class="mark">TD</span>ToolNet Drama</a>' +
        (opts.search
          ? '<div class="topbar-search"><input id="tn-search" type="search" placeholder="' + esc(opts.searchPlaceholder || "Search") + '" aria-label="Search"></div>'
          : '<div class="topbar-search"></div>') +
        '<div class="topbar-right">' +
          '<span class="user"><span class="name" id="tn-user"></span></span>' +
          '<button type="button" class="btn ghost sm" id="tn-signout">Sign out</button>' +
        "</div>" +
      "</div>";
    document.body.insertBefore(header, document.body.firstChild);

    var scrim = document.createElement("div");
    scrim.className = "scrim";
    scrim.id = "tn-scrim";
    var drawer = document.createElement("aside");
    drawer.className = "drawer";
    drawer.setAttribute("aria-label", "Admin navigation");
    drawer.innerHTML =
      '<div class="drawer-head"><span class="mark" style="width:22px;height:22px;border-radius:6px;display:inline-grid;place-items:center;background:#23262e;border:1px solid var(--border-2);font-size:11px;font-weight:700;">TD</span>ToolNet Drama</div>' +
      "<nav>" + nav + "</nav>" +
      '<div class="drawer-foot">Owner console &middot; no customer accounts</div>';
    document.body.appendChild(scrim);
    document.body.appendChild(drawer);

    function closeNav() { document.body.classList.remove("nav-open"); }
    header.querySelector("#tn-menu").addEventListener("click", function () {
      document.body.classList.toggle("nav-open");
    });
    scrim.addEventListener("click", closeNav);
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closeNav(); });
    drawer.querySelectorAll("nav a").forEach(function (a) { a.addEventListener("click", closeNav); });
    header.querySelector("#tn-signout").addEventListener("click", signOut);

    if (opts.search && typeof opts.onSearch === "function") {
      header.querySelector("#tn-search").addEventListener("input", function (ev) {
        opts.onSearch(ev.target.value || "");
      });
    }

    me().then(function (j) {
      var el = header.querySelector("#tn-user");
      if (el) el.textContent = j.username || "";
    }).catch(function () { /* api() already redirects on 401 */ });
  }

  function toast(msg, type) {
    var wrap = document.querySelector(".toast-wrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "toast-wrap";
      document.body.appendChild(wrap);
    }
    var el = document.createElement("div");
    el.className = "toast " + (type || "");
    el.innerHTML = '<span>' + esc(msg) + "</span><button aria-label=\"dismiss\">&times;</button>";
    var close = function () { el.remove(); };
    el.querySelector("button").addEventListener("click", close);
    wrap.appendChild(el);
    setTimeout(close, 5200);
  }

  async function withBusy(btn, fn) {
    if (!btn) return fn();
    var label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span>';
    try {
      return await fn();
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
    }
  }

  /* Minimal modal. Returns {close}. Content element is moved into the dialog. */
  function openModal(title, sub, bodyEl, actions) {
    closeModal();
    var scrim = document.createElement("div");
    scrim.className = "modal-scrim";
    scrim.id = "tn-modal";
    var box = document.createElement("div");
    box.className = "modal";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", title);
    var h = document.createElement("h3");
    h.textContent = title;
    box.appendChild(h);
    if (sub) {
      var p = document.createElement("p");
      p.className = "sub";
      p.textContent = sub;
      box.appendChild(p);
    }
    box.appendChild(bodyEl);
    var row = document.createElement("div");
    row.className = "modal-acts";
    (actions || []).forEach(function (a) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn " + (a.kind || "ghost");
      b.textContent = a.label;
      b.addEventListener("click", function () { a.onClick && a.onClick(b); });
      row.appendChild(b);
    });
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn subtle";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", closeModal);
    row.appendChild(cancel);
    box.appendChild(row);
    scrim.appendChild(box);
    scrim.addEventListener("click", function (ev) { if (ev.target === scrim) closeModal(); });
    document.body.appendChild(scrim);
    requestAnimationFrame(function () { scrim.classList.add("open"); });
    var first = bodyEl.querySelector("input, select");
    if (first) first.focus();
    return { close: closeModal, el: box };
  }

  function closeModal() {
    var m = document.getElementById("tn-modal");
    if (m) m.remove();
  }

  function fieldRow(labelText, inputEl, hint) {
    var wrap = document.createElement("div");
    wrap.className = "field";
    wrap.style.minWidth = "100%";
    var lab = document.createElement("label");
    lab.textContent = labelText;
    wrap.appendChild(lab);
    wrap.appendChild(inputEl);
    if (hint) {
      var h = document.createElement("span");
      h.className = "hint";
      h.textContent = hint;
      wrap.appendChild(h);
    }
    var msg = document.createElement("span");
    msg.className = "invalid-msg";
    wrap.appendChild(msg);
    return wrap;
  }

  function markInvalid(inputEl, message) {
    var wrap = inputEl.closest(".field");
    if (!wrap) return;
    if (message) {
      wrap.classList.add("invalid");
      wrap.querySelector(".invalid-msg").textContent = message;
    } else {
      wrap.classList.remove("invalid");
      wrap.querySelector(".invalid-msg").textContent = "";
    }
  }

  function shortErr(err) {
    var m = String((err && err.message) || err || "Request failed");
    return m.length > 140 ? m.slice(0, 140) + "…" : m;
  }

  return {
    api: api, me: me, ensureCsrf: ensureCsrf, csrf: function () { return csrf; },
    esc: esc, ts: ts, bytes: bytes, num: num, capped: capped, isOk: isOk,
    signOut: signOut, mountShell: mountShell, toast: toast, withBusy: withBusy,
    openModal: openModal, closeModal: closeModal,
    fieldRow: fieldRow, markInvalid: markInvalid, shortErr: shortErr
  };
})();
