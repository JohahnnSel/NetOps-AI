/**
 * app.js — Frontend de NetOps AI.
 *
 * Lee el stream NDJSON de /api/chat/stream (una línea JSON por evento) con
 * fetch() + ReadableStream. Se eligió NDJSON en vez de EventSource/SSE
 * porque EventSource no soporta POST con body, y acá necesitamos mandar el
 * mensaje del usuario en el cuerpo de la petición.
 *
 * Sin frameworks ni dependencias: DOM directo + un renderer markdown-lite
 * propio para el texto estructurado que devuelve el agente
 * (## encabezados, **negritas**, listas, `code`).
 */

(() => {
  "use strict";

  const chatScroll = document.getElementById("chatScroll");
  const chatEmpty = document.getElementById("chatEmpty");
  const composerForm = document.getElementById("composerForm");
  const composerInput = document.getElementById("composerInput");
  const sendBtn = document.getElementById("sendBtn");
  const toolsBody = document.getElementById("toolsBody");
  const historyBody = document.getElementById("historyBody");
  const historyCount = document.getElementById("historyCount");
  const pipeline = document.getElementById("pipeline");
  const pipelineFill = document.getElementById("pipelineFill");
  const clockEl = document.getElementById("clock");
  const connStatus = document.getElementById("connStatus");

  const STAGES = ["thinking", "executing", "analyzing", "done"];

  let isStreaming = false;

  // ------------------------------------------------------------------ //
  // Reloj de la topbar
  // ------------------------------------------------------------------ //
  function tickClock() {
    const now = new Date();
    clockEl.textContent = now.toLocaleTimeString("es-PE", { hour12: false });
  }
  tickClock();
  setInterval(tickClock, 1000);

  // ------------------------------------------------------------------ //
  // Utilidades
  // ------------------------------------------------------------------ //
  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function fmtDuration(ms) {
    if (ms == null) return "";
    return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
  }

  function fmtTime(isoOrEpoch) {
    try {
      const d = typeof isoOrEpoch === "number" ? new Date(isoOrEpoch * 1000) : new Date(isoOrEpoch);
      return d.toLocaleTimeString("es-PE", { hour12: false });
    } catch {
      return "";
    }
  }

  /** Renderer markdown-lite: sin dependencias externas, cubre lo que el
   *  system prompt le pide al modelo (## headers, **bold**, listas, `code`). */
  function renderMarkdownLite(raw) {
    const escaped = escapeHtml(raw)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

    const lines = escaped.split("\n");
    let html = "";
    let inList = false;

    for (const line of lines) {
      const headerMatch = line.match(/^##\s+(.*)/);
      const listMatch = line.match(/^[-*]\s+(.*)/);

      if (headerMatch) {
        if (inList) { html += "</ul>"; inList = false; }
        html += `<h2>${headerMatch[1]}</h2>`;
      } else if (listMatch) {
        if (!inList) { html += "<ul>"; inList = true; }
        html += `<li>${listMatch[1]}</li>`;
      } else if (line.trim() === "") {
        if (inList) { html += "</ul>"; inList = false; }
      } else {
        if (inList) { html += "</ul>"; inList = false; }
        html += `<p>${line}</p>`;
      }
    }
    if (inList) html += "</ul>";
    return html;
  }

  // ------------------------------------------------------------------ //
  // Pipeline de estado (elemento distintivo de la UI)
  // ------------------------------------------------------------------ //
  function setStage(stageName) {
    const idx = STAGES.indexOf(stageName);
    const stages = pipeline.querySelectorAll(".stage");
    stages.forEach((el, i) => {
      el.classList.remove("active", "complete");
      if (i < idx) el.classList.add("complete");
      else if (i === idx) el.classList.add("active");
    });
    const pct = idx < 0 ? 0 : ((idx + 1) / STAGES.length) * 100;
    pipelineFill.style.right = `${100 - pct}%`;
  }

  function resetPipeline() {
    pipeline.querySelectorAll(".stage").forEach((el) => el.classList.remove("active", "complete"));
    pipelineFill.style.right = "100%";
  }

  // ------------------------------------------------------------------ //
  // Chat: construcción de mensajes
  // ------------------------------------------------------------------ //
  function scrollToBottom() {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }

  function addUserMessage(text) {
    if (chatEmpty) chatEmpty.style.display = "none";
    const msg = document.createElement("div");
    msg.className = "msg msg-user";
    msg.innerHTML = `
      <div class="msg-avatar">TÚ</div>
      <div class="msg-bubble"><p>${escapeHtml(text)}</p></div>
    `;
    chatScroll.appendChild(msg);
    scrollToBottom();
  }

  function addAgentMessage() {
    const msg = document.createElement("div");
    msg.className = "msg msg-agent";
    msg.innerHTML = `
      <div class="msg-avatar">AI</div>
      <div class="msg-bubble"><span class="cursor-blink"></span></div>
    `;
    chatScroll.appendChild(msg);
    scrollToBottom();
    return msg.querySelector(".msg-bubble");
  }

  function addErrorMessage(text) {
    if (chatEmpty) chatEmpty.style.display = "none";
    const msg = document.createElement("div");
    msg.className = "msg msg-agent";
    msg.innerHTML = `
      <div class="msg-avatar">AI</div>
      <div class="msg-bubble"><p style="color:var(--accent-crit)">⚠ ${escapeHtml(text)}</p></div>
    `;
    chatScroll.appendChild(msg);
    scrollToBottom();
  }

  // ------------------------------------------------------------------ //
  // Panel de herramientas (traza en vivo)
  // ------------------------------------------------------------------ //
  function clearToolsPanel() {
    toolsBody.innerHTML = "";
  }

  function addToolStartCard(toolName, input) {
    const empty = toolsBody.querySelector(".panel-empty");
    if (empty) empty.remove();

    const card = document.createElement("div");
    card.className = "tool-card";
    const inputTxt = Object.entries(input || {})
      .map(([k, v]) => `${k}=${v}`)
      .join(", ");
    card.innerHTML = `
      <div class="tool-card-head">
        <span class="tool-name">${escapeHtml(toolName)}</span>
        <span class="tool-badge tool-badge-running">ejecutando…</span>
      </div>
      <div class="tool-meta">${escapeHtml(inputTxt)}</div>
    `;
    toolsBody.prepend(card);
    return card;
  }

  function resolveToolCard(card, entry) {
    if (!card) return;
    const badge = card.querySelector(".tool-badge");
    badge.textContent = entry.success ? "ok" : "error";
    badge.className = `tool-badge ${entry.success ? "tool-badge-ok" : "tool-badge-error"}`;

    const meta = card.querySelector(".tool-meta");
    meta.textContent += ` · ${fmtDuration(entry.duration_ms)} · ${fmtTime(entry.timestamp)}`;

    const output = document.createElement("div");
    output.className = "tool-output";
    const text = String(entry.output || "");
    output.textContent = text.length > 400 ? text.slice(0, 400) + "…" : text;
    card.appendChild(output);
  }

  // ------------------------------------------------------------------ //
  // Panel de historial
  // ------------------------------------------------------------------ //
  async function loadHistory() {
    try {
      const resp = await fetch("/api/history");
      if (!resp.ok) throw new Error("no se pudo cargar el historial");
      const rows = await resp.json();
      renderHistory(rows);
    } catch (err) {
      historyBody.innerHTML = `<p class="panel-empty">No se pudo cargar el historial.</p>`;
    }
  }

  function renderHistory(rows) {
    historyCount.textContent = rows.length ? `${rows.length}` : "";
    if (!rows.length) {
      historyBody.innerHTML = `<p class="panel-empty">Todavía no hay conversaciones guardadas.</p>`;
      return;
    }
    historyBody.innerHTML = "";
    for (const row of rows) {
      const item = document.createElement("div");
      item.className = "history-item";
      item.innerHTML = `
        <div class="history-q">${escapeHtml(row.question)}</div>
        <div class="history-meta">${fmtTime(row.created_at)} · ${fmtDuration(row.duration_ms)}</div>
      `;
      historyBody.appendChild(item);
    }
  }

  // ------------------------------------------------------------------ //
  // Envío de mensajes y consumo del stream NDJSON
  // ------------------------------------------------------------------ //
  async function sendMessage(text) {
    if (isStreaming || !text.trim()) return;
    isStreaming = true;
    sendBtn.disabled = true;
    connStatus.innerHTML = `<i class="dot dot-warn"></i><span>procesando…</span>`;

    addUserMessage(text);
    clearToolsPanel();
    resetPipeline();

    let agentBubble = null;
    let agentRawText = "";
    let activeToolCard = null;

    try {
      const resp = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });

      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({}));
        throw new Error(errBody.error || `Error HTTP ${resp.status}`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop(); // línea incompleta, se completa en el próximo chunk

        for (const line of lines) {
          if (!line.trim()) continue;
          const event = JSON.parse(line);

          switch (event.type) {
            case "status":
              setStage(event.step);
              break;

            case "token":
              if (!agentBubble) agentBubble = addAgentMessage();
              agentRawText += event.text;
              agentBubble.innerHTML = renderMarkdownLite(agentRawText) + '<span class="cursor-blink"></span>';
              scrollToBottom();
              break;

            case "tool_start":
              activeToolCard = addToolStartCard(event.tool, event.input);
              break;

            case "tool_result":
              resolveToolCard(activeToolCard, event);
              activeToolCard = null;
              break;

            case "done":
              if (!agentBubble) agentBubble = addAgentMessage();
              agentBubble.innerHTML = renderMarkdownLite(event.reply || agentRawText);
              scrollToBottom();
              break;

            case "error":
              addErrorMessage(event.message || "Error desconocido del agente");
              break;
          }
        }
      }
    } catch (err) {
      addErrorMessage(err.message || "No se pudo conectar con el agente");
    } finally {
      isStreaming = false;
      sendBtn.disabled = false;
      connStatus.innerHTML = `<i class="dot dot-ok"></i><span>modelo listo</span>`;
      loadHistory();
    }
  }

  // ------------------------------------------------------------------ //
  // Composer: textarea auto-resize + submit + atajos de teclado
  // ------------------------------------------------------------------ //
  function autoResize() {
    composerInput.style.height = "auto";
    composerInput.style.height = Math.min(composerInput.scrollHeight, 140) + "px";
  }

  composerInput.addEventListener("input", autoResize);

  composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      composerForm.requestSubmit();
    }
  });

  composerForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = composerInput.value;
    composerInput.value = "";
    autoResize();
    sendMessage(text);
  });

  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      sendMessage(chip.textContent.trim());
    });
  });

  // ------------------------------------------------------------------ //
  // Init
  // ------------------------------------------------------------------ //
  loadHistory();
})();
