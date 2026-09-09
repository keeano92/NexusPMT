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
    renderWorldmapGate(snap);
  }

  function renderDaily(snap) {
    const el = $("dailyPnl");
    const v = snap.daily_pnl_cents || 0;
    el.textContent = money(v);
    el.className = "daily" + (v < 0 ? " neg" : "");
  }

  function renderWheel(nodes) {
    const root = $("wheelStream");
    root.innerHTML = "";
    (nodes || []).slice().reverse().forEach((n) => {
      const div = document.createElement("div");
      div.className = "item o" + (n.order || 1);
      div.innerHTML = `<strong>L${n.order}</strong> · ${n.domain} · ${n.title}
        <div style="color:#8b9bb4;font-size:0.72rem">conf ${(n.confidence * 100).toFixed(0)}% · ${(n.kalshi_keywords || []).join(", ")}</div>`;
      root.appendChild(div);
    });
  }

  function renderEdges(edges) {
    const body = $("edgeBody");
    body.innerHTML = "";
    (edges || []).forEach((e) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${e.ticker}</td><td>${e.category || ""}</td><td>${(e.market_prob * 100).toFixed(1)}%</td>
        <td>${(e.model_prob * 100).toFixed(1)}%</td><td>${(e.edge * 100).toFixed(1)}%</td><td>${e.action}</td>`;
      body.appendChild(tr);
    });
  }

  function renderLedger(rows) {
    const body = $("ledgerBody");
    body.innerHTML = "";
    (rows || []).forEach((r) => {
      const tr = document.createElement("tr");
      const t = (r.ts || "").replace("T", " ").slice(0, 19);
      tr.innerHTML = `<td>${t}</td><td>${r.kind}</td><td>${r.ticker || ""}</td><td>${r.side || ""}</td>
        <td>${r.qty ?? ""}</td><td>${r.price_cents ?? ""}</td><td>${r.mode || ""}</td><td>${r.status}</td><td>${r.message || ""}</td>`;
      body.appendChild(tr);
    });
  }

  function renderPortfolio(snap) {
    const root = $("portfolio");
    const cash = money(snap.paper_cash_cents);
    const positions = snap.positions || [];
    root.innerHTML = `<div class="item">Cash: <strong>${cash}</strong></div>` +
      positions.map((p) => `<div class="item">${p.ticker} · qty ${p.qty} · avg ${p.avg_price_cents}¢ · ${p.side}</div>`).join("");
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
    renderEdges(snap.edges);
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
    if (!res.ok) {
      const err = await res.text();
      alert("Control failed: " + res.status + " " + err);
      return;
    }
    await refresh();
  }

  $("btnKill").onclick = () => control("/api/controls/kill");
  $("btnPause").onclick = () => control("/api/controls/pause");
  $("btnResume").onclick = () => control("/api/controls/resume");
  $("btnClearKill").onclick = () => control("/api/controls/clear-kill");
  $("btnFlatten").onclick = () => {
    const confirm = prompt('Type FLATTEN to close all positions');
    if (confirm) control("/api/controls/flatten", { confirm });
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
      else if (msg.type === "edges") renderEdges(msg.edges);
      else if (msg.type === "worldmap") {
        if (msg.worldmap_ready === false || msg.worldmap_ready === true) {
          refresh();
        } else if (msg.start_result) {
          $("wmBlockDetail").textContent = JSON.stringify(msg.start_result, null, 2);
          refresh();
        }
      } else if (msg.type === "ledger" || msg.type === "failsafes") refresh();
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
