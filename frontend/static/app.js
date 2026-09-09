(() => {
  const $ = (id) => document.getElementById(id);
  const tokenKey = "nexuspmt_op_token";
  let horizon = "rt";
  let chart;
  let pnlData = {};

  const tokenInput = $("opToken");
  tokenInput.value = localStorage.getItem(tokenKey) || "";
  tokenInput.addEventListener("change", () => {
    localStorage.setItem(tokenKey, tokenInput.value.trim());
  });

  function opHeaders() {
    const t = tokenInput.value.trim();
    return t ? { "X-Nexus-Token": t, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
  }

  function money(cents) {
    const v = (Number(cents) || 0) / 100;
    return (v < 0 ? "-" : "") + "$" + Math.abs(v).toFixed(2);
  }

  function setPill(el, text, cls) {
    el.textContent = text;
    el.className = "pill " + (cls || "");
  }

  function renderWorldmapGate(snap) {
    const blocker = $("wmBlocker");
    const required = snap.worldmap_required !== false;
    const ready = !!snap.worldmap_ready;
    if (required && !ready) {
      blocker.classList.remove("hidden");
      $("wmBlockReason").textContent =
        snap.worldmap_block_reason ||
        "NexusPMT cannot run without SK AI WorldMap telemetry.";
      $("wmBlockDetail").textContent = JSON.stringify(
        {
          sidecar_status: snap.worldmap_health?.sidecar_status,
          status: snap.worldmap_health?.status,
          error: snap.worldmap_health?.error,
        },
        null,
        2
      );
    } else {
      blocker.classList.add("hidden");
    }
  }

  function renderPills(snap) {
    setPill($("pillMode"), (snap.trading_mode || "paper").toUpperCase(), snap.trading_mode === "live" ? "bad" : "warn");
    setPill($("pillEnv"), (snap.kalshi_env || "demo").toUpperCase());
    const fs = snap.failsafes?.state || "unknown";
    setPill($("pillFs"), fs.toUpperCase(), fs === "running" ? "ok" : fs === "paused" ? "warn" : "bad");
    const ready = !!snap.worldmap_ready;
    const wm = snap.worldmap_health?.status || snap.worldmap_health?.sidecar_status || "…";
    setPill(
      $("pillWm"),
      ready ? "WM READY" : "WM DOWN",
      ready || wm === 200 || wm === "HEALTHY" ? "ok" : "bad"
    );
    if ($("selMode") && snap.trading_mode) $("selMode").value = snap.trading_mode;
    if ($("selEnv") && snap.kalshi_env) $("selEnv").value = snap.kalshi_env;
    if ($("bookTag")) {
      const paper = snap.paper;
      let tag = snap.book_key || `${snap.kalshi_env}:${snap.trading_mode}`;
      if (paper) {
        tag += ` · PAPER $${((paper.equity_cents || 0) / 100).toFixed(2)}→$${(
          (paper.target_cents || 10000) / 100
        ).toFixed(0)}`;
        if (paper.gate_ready) tag += " · GATE OK";
      }
      if (!snap.live_unlock) tag += " · LIVE LOCKED";
      $("bookTag").textContent = tag;
    }
    renderWorldmapGate(snap);
  }

  function colorizeTerminalLine(line) {
    const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    let cls = "scan";
    if (/\bENTER\b/.test(line)) cls = "enter";
    else if (/\bSKIP\b/.test(line)) cls = "skip";
    else if (/\bERROR\b|\bWARN\b/.test(line)) cls = "error";
    return `<span class="${cls}">${esc(line)}</span>`;
  }

  function renderTerminal(lines) {
    const el = $("oppTerminal");
    if (!el) return;
    const arr = lines || [];
    el.innerHTML = arr.map(colorizeTerminalLine).join("\n") || '<span class="scan">waiting for Kalshi scan…</span>';
    el.scrollTop = el.scrollHeight;
  }

  function renderWheel(nodes) {
    const root = $("wheelStream");
    root.innerHTML = "";
    const list = nodes || [];
    if (!list.length) {
      root.innerHTML = '<div class="item">No wheel nodes yet — click REFRESH WHEEL (needs OP TOKEN).</div>';
      return;
    }
    list.slice().reverse().forEach((n) => {
      const div = document.createElement("div");
      div.className = "item o" + (n.order || 1);
      div.innerHTML = `<strong>L${n.order}</strong> · ${n.domain} · ${n.title}
        <div style="color:#8b9bb4;font-size:0.72rem">conf ${((n.confidence || 0) * 100).toFixed(0)}% · ${(n.kalshi_keywords || []).join(", ")}</div>`;
      root.appendChild(div);
    });
  }

  function renderDaily(snap) {
    const el = $("dailyPnl");
    const v = snap.session_pnl_cents != null ? snap.session_pnl_cents : (snap.daily_pnl_cents || 0);
    el.textContent = money(v);
    el.className = "daily" + (v < 0 ? " neg" : "");
    const line = $("pnlEquityLine");
    if (line) {
      const eq = snap.equity_cents != null ? money(snap.equity_cents) : money(snap.live_equity_cents || snap.cash_cents || 0);
      const cash = money(snap.cash_cents != null ? snap.cash_cents : snap.paper_cash_cents);
      line.textContent = `Equity ${eq} · Cash ${cash} · Source ${(snap.portfolio_source || "paper").toUpperCase()} · Book ${snap.book_key || "—"}`;
    }
    const halt = $("haltBanner");
    if (halt) {
      if (snap.trading_halted) {
        halt.classList.remove("hidden");
        $("haltReason").textContent = snap.trading_halt_reason
          ? `reason: ${snap.trading_halt_reason}`
          : "kill switch active — trades blocked";
      } else {
        halt.classList.add("hidden");
      }
    }
  }

  function renderEdges(edges, scannedAt, evaluations) {
    const body = $("edgeBody");
    body.innerHTML = "";
    const evals = evaluations || [];
    const list = evals.length ? evals : (edges || []);
    if ($("oppHint")) {
      $("oppHint").textContent = list.length
        ? `Kalshi → xAI+Wheel · ${list.length} scored · strong ENTER ≈ 90% book cash${scannedAt ? " · " + scannedAt : ""}`
        : "Waiting for Kalshi opportunity scan…";
    }
    if (!list.length) {
      body.innerHTML = "<tr><td colspan='6'>No evaluations yet</td></tr>";
      return;
    }
    list.slice(0, 40).forEach((e) => {
      const tr = document.createElement("tr");
      const voi = e.value_of_interest != null ? (e.value_of_interest * 100).toFixed(0) + "%" : "—";
      const mkt = e.market_prob != null ? (e.market_prob * 100).toFixed(1) + "%" : "—";
      const model = e.model_prob != null ? (e.model_prob * 100).toFixed(1) + "%" : "—";
      const edge = e.edge != null ? (e.edge * 100).toFixed(1) + "%" : "—";
      const verdict = e.verdict || e.action || "—";
      tr.innerHTML = `<td>${e.ticker || ""}</td><td>${voi}</td><td>${mkt}</td>
        <td>${model}</td><td>${edge}</td><td>${verdict}</td>`;
      body.appendChild(tr);
    });
  }

  function formatLocalTs(ts) {
    if (!ts) return "";
    try {
      const d = new Date(ts);
      if (!Number.isNaN(d.getTime())) {
        return d.toLocaleString(undefined, {
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        });
      }
    } catch (_) { /* fall through */ }
    return String(ts).replace("T", " ").slice(0, 19);
  }

  function renderLedger(rows) {
    const body = $("ledgerBody");
    body.innerHTML = "";
    (rows || []).forEach((r) => {
      const tr = document.createElement("tr");
      const t = r.ts_display || formatLocalTs(r.ts);
      const msg = (r.message || "").replace(/</g, "&lt;");
      tr.innerHTML = `<td>${t}</td><td>${r.kind}</td><td>${r.ticker || ""}</td><td>${r.side || ""}</td>
        <td>${r.qty ?? ""}</td><td>${r.price_cents ?? ""}</td><td>${r.mode || ""}</td><td>${r.status}</td><td title="${msg}">${msg}</td>`;
      body.appendChild(tr);
    });
  }

  function renderPortfolio(snap) {
    const root = $("portfolio");
    const paper = snap.paper;
    const usingPaper = (snap.trading_mode || "paper") === "paper" && paper;
    const source = usingPaper ? "PAPER SHADOW (live marks)" : (snap.portfolio_source || "paper").toUpperCase();
    const cash = usingPaper
      ? money(paper.cash_cents)
      : money(snap.cash_cents != null ? snap.cash_cents : snap.paper_cash_cents);
    const equity = usingPaper
      ? money(paper.equity_cents)
      : snap.live_equity_cents != null
        ? money(snap.live_equity_cents)
        : null;
    const posVal = snap.live_portfolio_value_cents != null ? money(snap.live_portfolio_value_cents) : null;
    const realized = usingPaper
      ? money(paper.realized_pnl_cents)
      : snap.live_realized_pnl_cents != null
        ? money(snap.live_realized_pnl_cents)
        : null;
    const positions = usingPaper
      ? (paper.positions || []).map((p) => ({
          ticker: p.ticker,
          side: p.side,
          qty: p.qty,
          avg_price_cents: p.entry_price_cents,
          mark: p.mark_yes_prob,
        }))
      : snap.positions || [];
    let html = `<div class="item"><strong>SOURCE: ${source}</strong>${snap.portfolio_updated_ts ? " · " + formatLocalTs(snap.portfolio_updated_ts) : ""}</div>`;
    html += `<div class="item">Cash: <strong>${cash}</strong></div>`;
    if (usingPaper) {
      html += `<div class="item">Paper equity: <strong>${equity}</strong> · session ${money(paper.session_pnl_cents)} · W/L ${paper.wins || 0}/${paper.losses || 0}</div>`;
      html += `<div class="item">Gate: ${money(paper.equity_cents)} → ${money(paper.target_cents)} ${paper.gate_ready ? "✓ READY" : "(locked)"}</div>`;
      if (realized) html += `<div class="item">Realized PnL: <strong>${realized}</strong></div>`;
    } else if (source === "LIVE") {
      if (posVal) html += `<div class="item">Positions value: <strong>${posVal}</strong></div>`;
      if (equity) html += `<div class="item">Equity: <strong>${equity}</strong></div>`;
      if (realized) html += `<div class="item">Realized PnL: <strong>${realized}</strong></div>`;
    }
    if (!positions.length) {
      html += `<div class="item">No open positions</div>`;
    } else {
      html += positions.map((p) => {
        const extra = p.exposure_cents != null ? ` · exp ${money(p.exposure_cents)}` : "";
        const rpnl = p.realized_pnl_cents != null ? ` · rPnL ${money(p.realized_pnl_cents)}` : "";
        const mk = p.mark != null ? ` · mark ${(Number(p.mark) * 100).toFixed(0)}¢` : "";
        return `<div class="item">${p.ticker} · ${p.side} × ${p.qty} · avg ${p.avg_price_cents ?? "—"}¢${mk}${extra}${rpnl}</div>`;
      }).join("");
    }
    root.innerHTML = html;
  }

  function updateChart() {
    const series = pnlData[horizon] || { points: [] };
    const labels = (series.points || []).map((p) => new Date(p.ts * 1000).toLocaleTimeString());
    const values = (series.points || []).map((p) => p.equity_cents / 100);
    $("horizonStats").textContent =
      `Δ ${money(series.change_cents)} (${(series.change_pct || 0).toFixed(2)}%) · high ${money(series.high_cents)} · low ${money(series.low_cents)}`;

    const ctx = $("pnlChart").getContext("2d");
    if (!chart) {
      chart = new Chart(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [{
            data: values,
            borderColor: "#00e5ff",
            backgroundColor: "rgba(0,229,255,0.12)",
            fill: true,
            tension: 0.25,
            pointRadius: 0,
            borderWidth: 2,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: "#8b9bb4", maxTicksLimit: 6 }, grid: { color: "rgba(139,155,180,0.1)" } },
            y: { ticks: { color: "#8b9bb4" }, grid: { color: "rgba(139,155,180,0.1)" } },
          },
        },
      });
    } else {
      chart.data.labels = labels;
      chart.data.datasets[0].data = values;
      chart.update("none");
    }
  }

  function applySnapshot(snap) {
    renderPills(snap);
    renderDaily(snap);
    renderWheel(snap.wheel);
    renderEdges(snap.edges, null, snap.evaluations);
    renderTerminal(snap.terminal);
    renderLedger(snap.ledger);
    renderPortfolio(snap);
    pnlData = snap.pnl || {};
    updateChart();
    $("lastIngest").textContent = snap.last_ingest_ts || "—";
  }

  async function refresh() {
    const res = await fetch("/api/dashboard");
    applySnapshot(await res.json());
  }

  async function control(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: opHeaders(),
      body: body ? JSON.stringify(body) : "{}",
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
    if (!res.ok) {
      alert("Control failed: " + res.status + " " + text);
      return null;
    }
    if (data && data.ok === false) {
      alert(data.error || "Control rejected");
      return data;
    }
    await refresh();
    return data;
  }

  $("btnKill").onclick = () => control("/api/controls/kill");
  $("btnPause").onclick = () => control("/api/controls/pause");
  $("btnResume").onclick = () => control("/api/controls/resume");
  $("btnClearKill").onclick = () => control("/api/controls/clear-kill");
  $("btnFlatten").onclick = () => {
    const confirmTxt = prompt("Type FLATTEN to close all positions");
    if (confirmTxt) control("/api/controls/flatten", { confirm: confirmTxt });
  };
  $("btnRefreshWheel").onclick = async () => {
    const data = await control("/api/controls/refresh-wheel");
    if (data && data.ok) alert("Wheel refreshed: " + data.nodes + " nodes (" + (data.model || "?") + ")");
  };
  $("btnRefreshPortfolio").onclick = async () => {
    const data = await control("/api/controls/refresh-portfolio");
    if (data && data.ok === false) alert(data.error || "Portfolio refresh failed");
  };
  $("btnResetPnl").onclick = async () => {
    const data = await control("/api/controls/reset-live-pnl");
    if (data && data.ok) alert("Live PnL re-anchored at " + money(data.equity_cents) + " — trading resumed");
  };
  $("btnApplyMode").onclick = async () => {
    const trading_mode = $("selMode").value;
    const kalshi_env = $("selEnv").value;
    let confirmTxt = "";
    if (trading_mode === "live" && kalshi_env === "production") {
      confirmTxt = prompt('Type LIVE PRODUCTION to enable live trading on production Kalshi');
    } else if (trading_mode === "live") {
      confirmTxt = prompt("Type LIVE to enable live trading (still on selected env)");
    } else if (kalshi_env === "production") {
      confirmTxt = prompt("Type PRODUCTION to switch Kalshi env to production (paper still safer)");
    }
    if ((trading_mode === "live" || kalshi_env === "production") && !confirmTxt) return;
    await control("/api/controls/trading-config", {
      trading_mode,
      kalshi_env,
      confirm: confirmTxt || "",
    });
  };

  document.querySelectorAll("#horizonTabs button").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll("#horizonTabs button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      horizon = btn.dataset.h;
      updateChart();
    };
  });

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") applySnapshot(msg.data);
      else if (msg.type === "pnl") {
        pnlData = msg.pnl || pnlData;
        updateChart();
      } else if (msg.type === "wheel") renderWheel(msg.nodes);
      else if (msg.type === "edges") renderEdges(msg.edges, msg.scanned_at, msg.evaluations);
      else if (msg.type === "terminal") {
        if (msg.terminal) renderTerminal(msg.terminal);
        else if (msg.line) {
          const el = $("oppTerminal");
          if (el) {
            el.innerHTML += (el.innerHTML ? "\n" : "") + colorizeTerminalLine(msg.line);
            el.scrollTop = el.scrollHeight;
          }
        }
      } else if (msg.type === "portfolio") refresh();
      else if (msg.type === "worldmap") {
        if (msg.worldmap_ready === false || msg.worldmap_ready === true) {
          refresh();
        } else if (msg.start_result) {
          $("wmBlockDetail").textContent = JSON.stringify(msg.start_result, null, 2);
          refresh();
        }
      } else if (msg.type === "trading_config") refresh();
      else if (msg.type === "ledger" || msg.type === "failsafes") refresh();
    };
    ws.onclose = () => setTimeout(connectWs, 2000);
  }

  $("btnStartWm").onclick = async () => {
    $("wmBlockDetail").textContent = "Starting WorldMap container…";
    await control("/api/controls/start-worldmap");
    setTimeout(() => refresh().catch(console.error), 3000);
  };
  $("btnRecheckWm").onclick = () => refresh().catch(console.error);

  refresh().catch(console.error);
  connectWs();
  setInterval(() => refresh().catch(() => {}), 15000);
})();
