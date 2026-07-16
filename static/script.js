/* Aperture — AI Interview Console frontend logic */
(() => {
  "use strict";

  const els = {
    setupPanel: document.getElementById("setupPanel"),
    progressPanel: document.getElementById("progressPanel"),
    reportPanel: document.getElementById("reportPanel"),
    startForm: document.getElementById("startForm"),
    startBtn: document.getElementById("startBtn"),
    cancelBtn: document.getElementById("cancelBtn"),
    endBtn: document.getElementById("endBtn"),
    statusValue: document.getElementById("statusValue"),
    botStatusValue: document.getElementById("botStatusValue"),
    timerValue: document.getElementById("timerValue"),
    roleValue: document.getElementById("roleValue"),
    experienceValue: document.getElementById("experienceValue"),
    currentQuestion: document.getElementById("currentQuestion"),
    reportStatusText: document.getElementById("reportStatusText"),
    emailStatusText: document.getElementById("emailStatusText"),
    pulseLine: document.getElementById("pulseLine"),
    downloadPdfBtn: document.getElementById("downloadPdfBtn"),
    newInterviewBtn: document.getElementById("newInterviewBtn"),
    themeToggle: document.getElementById("themeToggle"),
    toastContainer: document.getElementById("toastContainer"),
    qaBreakdownList: document.getElementById("qaBreakdownList"),
  };

  let state = {
    sessionId: null,
    pollTimer: null,
    clockTimer: null,
    elapsedSeconds: 0,
  };

  // ---------- toast ----------
  function toast(message, isError = false) {
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " error" : "");
    el.textContent = message;
    els.toastContainer.appendChild(el);
    setTimeout(() => el.remove(), 4500);
  }

  // ---------- theme ----------
  function initTheme() {
    const saved = localStorage.getItem("aperture-theme");
    if (saved === "light") document.body.classList.add("light");
  }
  els.themeToggle.addEventListener("click", () => {
    document.body.classList.toggle("light");
    localStorage.setItem("aperture-theme", document.body.classList.contains("light") ? "light" : "dark");
  });
  initTheme();

  // ---------- pulse waveform (signature element) ----------
  let pulsePhase = 0;
  function drawPulse() {
    const points = [];
    const width = 400, height = 60, mid = height / 2;
    for (let x = 0; x <= width; x += 8) {
      const active = state.pollTimer !== null;
      const amp = active ? 14 + Math.random() * 10 : 3;
      const y = mid + Math.sin((x + pulsePhase) * 0.06) * amp * Math.sin(x * 0.01 + pulsePhase * 0.02);
      points.push(`${x},${y.toFixed(1)}`);
    }
    els.pulseLine.setAttribute("points", points.join(" "));
    pulsePhase += 4;
    requestAnimationFrame(drawPulse);
  }
  requestAnimationFrame(drawPulse);

  // ---------- panel switching ----------
  function showPanel(name) {
    els.setupPanel.classList.toggle("hidden", name !== "setup");
    els.progressPanel.classList.toggle("hidden", name !== "progress");
    els.reportPanel.classList.toggle("hidden", name !== "report");
  }

  // ---------- form submit ----------
  els.startForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const formData = new FormData(els.startForm);
    const payload = {
      name: formData.get("name").trim(),
      email: formData.get("email").trim(),
      meet_link: formData.get("meet_link").trim(),
    };
    const role = (formData.get("role") || "").trim();
    const experienceLevel = (formData.get("experience_level") || "").trim();
    if (role) payload.role = role;
    if (experienceLevel) payload.experience_level = experienceLevel;

    els.startBtn.disabled = true;
    els.startBtn.textContent = "Starting…";

    try {
      const res = await fetch("/api/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to start interview.");

      state.sessionId = data.session_id;
      toast("Interview started — bot is joining the call.");
      showPanel("progress");
      startPolling();
      startClock();
    } catch (err) {
      toast(err.message, true);
    } finally {
      els.startBtn.disabled = false;
      els.startBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="currentColor"/></svg> Start interview';
    }
  });

  els.cancelBtn.addEventListener("click", () => els.startForm.reset());

  // ---------- polling ----------
  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(pollStatus, 3000);
    pollStatus();
  }
  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  function stopClock() {
    if (state.clockTimer) clearInterval(state.clockTimer);
    state.clockTimer = null;
  }
  function startClock() {
    stopClock();
    state.elapsedSeconds = 0;
    state.clockTimer = setInterval(() => {
      state.elapsedSeconds += 1;
      const m = String(Math.floor(state.elapsedSeconds / 60)).padStart(2, "0");
      const s = String(state.elapsedSeconds % 60).padStart(2, "0");
      els.timerValue.textContent = `${m}:${s}`;
    }, 1000);
  }

  async function pollStatus() {
    if (!state.sessionId) return;
    try {
      const res = await fetch(`/api/status/${state.sessionId}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Status check failed.");

      els.statusValue.textContent = formatLabel(data.status);
      els.botStatusValue.textContent = formatLabel(data.bot_status);
      els.roleValue.textContent = data.role || "Not yet determined";
      els.experienceValue.textContent = data.experience_level || "Not yet determined";
      if (data.current_question) els.currentQuestion.textContent = data.current_question;
      if (data.error_message) toast(data.error_message, true);

      if (["completed", "failed", "ended"].includes(data.status)) {
        stopPolling();
        stopClock();
        if (data.status === "completed" && data.report_ready) {
          els.reportStatusText.textContent = data.email_sent
            ? "Report ready and emailed to you."
            : `Report ready (email delivery failed${data.email_error ? ": " + data.email_error : ""} — download the PDF below).`;
          await loadReport(data.email_sent);
        } else if (data.status === "failed") {
          toast(data.error_message || "Interview failed.", true);
          showPanel("setup");
        } else {
          toast("Interview ended.");
          showPanel("setup");
        }
      }
    } catch (err) {
      toast(err.message, true);
    }
  }

  function formatLabel(value) {
    if (!value) return "—";
    return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  // ---------- end interview ----------
  els.endBtn.addEventListener("click", async () => {
    if (!state.sessionId) return;
    els.endBtn.disabled = true;
    try {
      const res = await fetch("/api/end", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to end interview.");
      if (data.bot_removal_confirmed === false) {
        toast("Interview ended, but the bot may still be in the call — check the Bot status field.", true);
      } else {
        toast("Interview ended.");
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      els.endBtn.disabled = false;
    }
  });

  // ---------- report ----------
  async function loadReport(emailSent) {
    try {
      const res = await fetch(`/api/report/${state.sessionId}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Report not available.");
      renderReport(data, emailSent);
      showPanel("report");
    } catch (err) {
      toast(err.message, true);
    }
  }

  function renderReport(r, emailSent) {
    document.getElementById("scoreOverall").textContent = r.overall_score ?? "—";
    document.getElementById("scoreCommunication").textContent = r.communication_score ?? "—";
    document.getElementById("scoreTechnical").textContent = r.technical_score ?? "—";
    document.getElementById("scoreConfidence").textContent = r.confidence_score ?? "—";
    document.getElementById("scoreProblemSolving").textContent = r.problem_solving_score ?? "—";
    document.getElementById("summaryText").textContent = r.summary || "No summary available.";
    els.emailStatusText.textContent = emailSent
      ? `Sent to ${r.candidate_email || "your email"}.`
      : "Email delivery wasn't available — use the download button below.";

    fillList("strengthsList", r.strengths);
    fillList("weaknessesList", r.weaknesses);
    fillList("recommendationsList", r.recommendations);
    fillQaBreakdown(r.qa_breakdown);
  }

  function fillList(id, items) {
    const el = document.getElementById(id);
    el.innerHTML = "";
    (items && items.length ? items : ["None noted."]).forEach((item) => {
      const li = document.createElement("li");
      li.textContent = item;
      el.appendChild(li);
    });
  }

  function fillQaBreakdown(qaBreakdown) {
    els.qaBreakdownList.innerHTML = "";
    if (!qaBreakdown || !qaBreakdown.length) {
      els.qaBreakdownList.innerHTML = "<p>No questions recorded.</p>";
      return;
    }
    qaBreakdown.forEach((q) => {
      const card = document.createElement("div");
      card.className = "qa-item";
      const timeLabel = q.time_taken_seconds != null ? `${Math.round(q.time_taken_seconds)}s` : "N/A";
      card.innerHTML = `
        <div class="qa-item-header">
          <span>Q${q.question_number} · ${q.category}</span>
          <span>Score: ${q.score ?? "—"}/10 · Time: ${timeLabel}</span>
        </div>
        <p class="qa-item-question"><strong>Q:</strong> ${escapeHtml(q.question)}</p>
        <p class="qa-item-answer"><strong>A:</strong> ${escapeHtml(q.answer) || "(no answer captured)"}</p>
        ${q.feedback ? `<p class="qa-item-feedback"><strong>Feedback:</strong> ${escapeHtml(q.feedback)}</p>` : ""}
      `;
      els.qaBreakdownList.appendChild(card);
    });
  }

  function escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  els.downloadPdfBtn.addEventListener("click", () => {
    if (!state.sessionId) return;
    window.location.href = `/api/report/${state.sessionId}?format=pdf`;
  });

  els.newInterviewBtn.addEventListener("click", () => {
    state.sessionId = null;
    els.startForm.reset();
    els.timerValue.textContent = "00:00";
    els.currentQuestion.textContent = "Waiting for the bot to join the call…";
    els.roleValue.textContent = "Not yet determined";
    els.experienceValue.textContent = "Not yet determined";
    showPanel("setup");
  });
})();

/* ============================================================
   PWA: service worker registration + install / continue-in-
   browser prompt
   ============================================================ */
(() => {
  "use strict";

  const DISMISS_KEY = "aperture_install_dismissed_at";
  const DISMISS_DAYS = 14;
  const INSTALLED_KEY = "aperture_installed";

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch((err) => {
        console.warn("Service worker registration failed:", err);
      });
    });
  }

  const overlay = document.getElementById("installOverlay");
  if (!overlay) return; // markup not present on this page

  const installBtn = document.getElementById("installBtn");
  const continueBtn = document.getElementById("continueBrowserBtn");
  const bodyText = document.getElementById("installBody");
  const stepsList = document.getElementById("installSteps");

  const isStandalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true; // iOS Safari

  const isIos = /iphone|ipad|ipod/i.test(window.navigator.userAgent);

  // If we're running standalone right now, the app is obviously
  // installed -- persist that so a later visit in a plain browser tab
  // (where display-mode can't tell us anything) still knows not to ask.
  if (isStandalone) {
    localStorage.setItem(INSTALLED_KEY, "1");
  }

  function isInstalled() {
    return localStorage.getItem(INSTALLED_KEY) === "1";
  }

  function markInstalled() {
    localStorage.setItem(INSTALLED_KEY, "1");
    hideOverlay();
  }

  function recentlyDismissed() {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return false;
    const elapsedDays = (Date.now() - Number(raw)) / (1000 * 60 * 60 * 24);
    return elapsedDays < DISMISS_DAYS;
  }

  function hideOverlay() {
    overlay.hidden = true;
  }

  function dismissOverlay() {
    localStorage.setItem(DISMISS_KEY, String(Date.now()));
    hideOverlay();
  }

  function showOverlay() {
    if (isStandalone || isInstalled() || recentlyDismissed()) return;
    overlay.hidden = false;
  }

  let deferredPrompt = null;

  // Chrome / Edge / Android: native install prompt is available.
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    if (isInstalled()) return; // already installed -- never re-prompt
    deferredPrompt = event;
    installBtn.hidden = false;
    showOverlay();
  });

  // Fires once the browser finishes installing the app -- the most
  // reliable signal we get, so persist it immediately.
  window.addEventListener("appinstalled", () => {
    deferredPrompt = null;
    markInstalled();
  });

  installBtn.addEventListener("click", async () => {
    if (!deferredPrompt) {
      hideOverlay();
      return;
    }
    deferredPrompt.prompt();
    const { outcome } = await deferredPrompt.userChoice;
    deferredPrompt = null;
    // Belt-and-braces: some browsers are slow to fire (or never fire)
    // `appinstalled` after an accepted prompt, so also mark installed
    // here rather than relying on that event alone.
    if (outcome === "accepted") {
      markInstalled();
    } else {
      hideOverlay();
    }
  });

  continueBtn.addEventListener("click", dismissOverlay);

  if (isIos && !isStandalone) {
    // iOS Safari has no beforeinstallprompt -- show manual "Add to
    // Home Screen" steps instead, and hide the native-install button
    // since there's nothing for it to trigger.
    installBtn.hidden = true;
    bodyText.textContent = "Add Aperture to your Home Screen for a faster, full-screen experience.";
    stepsList.hidden = false;
    stepsList.innerHTML =
      "<li>Tap the Share icon in Safari's toolbar</li>" +
      "<li>Scroll down and tap \u201cAdd to Home Screen\u201d</li>" +
      "<li>Tap \u201cAdd\u201d to confirm</li>";
    if (!isInstalled()) {
      setTimeout(showOverlay, 1200);
    }
  }
})();
