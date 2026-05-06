"use strict";

const $ = (id) => document.getElementById(id);
const qs = (sel, root = document) => root.querySelector(sel);
const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
    mode: "simple",
    sessionId: null,
    description: "",
    triage: null,
    result: null,
};

// ─── init ─────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
    loadModels();

    // Top-level tabs (single / batch / chat)
    qsa(".top-tab").forEach(btn => {
        btn.addEventListener("click", () => {
            const tab = btn.dataset.tab;
            qsa(".top-tab").forEach(b => b.classList.toggle("active", b === btn));
            $("tab-single").hidden = tab !== "single";
            $("tab-batch").hidden = tab !== "batch";
            $("tab-chat").hidden = tab !== "chat";
        });
    });

    // Mode toggle (внутри single)
    qsa(".mode-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            qsa(".mode-btn").forEach(b => b.classList.toggle("active", b === btn));
            const mode = btn.dataset.mode;
            state.mode = mode;
            $("mode-simple").hidden = mode !== "simple";
            $("mode-detailed").hidden = mode !== "detailed";
        });
    });

    $("start-btn").addEventListener("click", onStart);
    $("finalize-btn").addEventListener("click", onFinalize);
    $("back-to-input").addEventListener("click", () => goToStep("input"));
    $("reset-btn").addEventListener("click", onReset);
    $("copy-result-btn").addEventListener("click", onCopyResult);

    $("description").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            onStart();
        }
    });

    // Batch
    $("batch-pick-btn").addEventListener("click", () => $("batch-file").click());
    $("batch-file").addEventListener("change", onBatchFileChosen);
    $("batch-start-btn").addEventListener("click", onBatchStart);
    $("batch-new-btn").addEventListener("click", onBatchReset);

    // Chat
    $("chat-send-btn").addEventListener("click", onChatSend);
    $("chat-reset-btn").addEventListener("click", onChatReset);
    $("chat-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            onChatSend();
        }
    });
});


async function loadModels() {
    try {
        const r = await fetch("/api/models");
        const data = await r.json();
        const sel = $("model-select");
        sel.innerHTML = "";
        for (const m of data.models) {
            const opt = document.createElement("option");
            opt.value = m;
            opt.textContent = m;
            if (m === data.default) opt.selected = true;
            sel.appendChild(opt);
        }
    } catch (e) {
        console.warn("Не удалось загрузить список моделей:", e);
    }
}


// ─── shape helpers ────────────────────────────────────────────────────────────

function gatherInput() {
    if (state.mode === "simple") {
        return {
            mode: "simple",
            description: $("description").value.trim(),
            fields: null,
        };
    }
    const fields = {
        "Полное наименование": $("f-name").value.trim(),
        "Назначение/функция":  $("f-purpose").value.trim(),
        "Материал/состав":      $("f-material").value.trim(),
        "Степень обработки":    $("f-processing").value,
        "Форма поставки":       $("f-form").value,
        "Торговая марка":       $("f-brand").value.trim(),
        "Страна происхождения": $("f-country").value.trim(),
        "Технические характеристики": $("f-tech").value.trim(),
    };
    return { mode: "detailed", description: null, fields };
}


// ─── step navigation ─────────────────────────────────────────────────────────

function goToStep(step) {
    $("step-input").hidden = step !== "input";
    $("step-questions").hidden = step !== "questions";
    $("step-result").hidden = step !== "result";
    window.scrollTo({ top: 0, behavior: "smooth" });
}


// ─── handlers ─────────────────────────────────────────────────────────────────

async function onStart() {
    const input = gatherInput();
    const hasInput = (input.description && input.description.trim())
        || (input.fields && Object.values(input.fields).some(v => v && String(v).trim()));
    if (!hasInput) {
        flashError("Опишите товар или заполните поля");
        return;
    }

    setStatus("Анализ описания и определение группы…");
    setBusy(true);

    try {
        const r = await fetch("/api/classify/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                ...input,
                model: $("model-select").value,
            }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        const data = await r.json();

        state.sessionId = data.session_id;
        state.description = data.description;
        state.triage = data;

        renderTriageStep(data);
        goToStep("questions");
    } catch (e) {
        flashError(`Ошибка: ${e.message}`);
    } finally {
        setStatus("");
        setBusy(false);
    }
}


async function onFinalize() {
    const answers = qsa(".question-item").map(item => ({
        id: item.dataset.qid,
        question: item.dataset.question,
        answer: qs("input, textarea", item).value.trim(),
    })).filter(a => a.answer);

    setStatus("Подбор кандидатов и финальная классификация…");
    setBusy(true);

    try {
        const r = await fetch("/api/classify/finalize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                session_id: state.sessionId,
                answers,
                model: $("model-select").value,
            }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        const data = await r.json();

        state.result = data;
        renderResultStep(data);
        goToStep("result");
    } catch (e) {
        flashError(`Ошибка: ${e.message}`);
    } finally {
        setStatus("");
        setBusy(false);
    }
}


function onReset() {
    state.sessionId = null;
    state.description = "";
    state.triage = null;
    state.result = null;
    $("description").value = "";
    qsa("#mode-detailed input, #mode-detailed textarea").forEach(i => i.value = "");
    qsa("#mode-detailed select").forEach(s => s.value = "");
    goToStep("input");
}


function onCopyResult() {
    if (!state.result) return;
    const code = state.result.result?.primary?.code;
    if (code) {
        navigator.clipboard.writeText(code);
        const btn = $("copy-result-btn");
        const original = btn.textContent;
        btn.textContent = "✓ Скопировано";
        setTimeout(() => btn.textContent = original, 1500);
    }
}


// ─── render: step 2 (triage) ─────────────────────────────────────────────────

function renderTriageStep(data) {
    const { group, completeness, missing_aspects, questions } = data;
    const completenessLabel = {
        high: "Высокая",
        medium: "Средняя",
        low: "Недостаточная",
    }[completeness] || "—";
    const completenessClass = `completeness-${completeness}`;

    $("triage-summary").innerHTML = `
        <div class="triage-row">
            <div class="triage-cell">
                <div class="cell-label">Определена группа</div>
                <div class="cell-value">
                    <code>${escapeHtml(group.code || "—")}</code>
                    <span class="cell-text">${escapeHtml(group.name || "")}</span>
                </div>
            </div>
            <div class="triage-cell">
                <div class="cell-label">Полнота описания</div>
                <div class="cell-value">
                    <span class="badge ${completenessClass}">${escapeHtml(completenessLabel)}</span>
                </div>
            </div>
        </div>
        ${missing_aspects && missing_aspects.length ? `
            <div class="missing-aspects">
                <div class="cell-label">Не хватает информации о:</div>
                <div class="aspects-list">
                    ${missing_aspects.map(a => `<span class="aspect-chip">${escapeHtml(a)}</span>`).join("")}
                </div>
            </div>
        ` : ""}
    `;

    const list = $("questions-list");
    list.innerHTML = "";

    if (!questions || questions.length === 0) {
        list.innerHTML = `
            <div class="info-box">
                Описания достаточно для классификации. Нажмите «Получить код» для финального результата.
            </div>
        `;
    } else {
        questions.forEach((q, i) => {
            const item = document.createElement("div");
            item.className = "question-item";
            item.dataset.qid = q.id;
            item.dataset.question = q.question;
            item.innerHTML = `
                <div class="q-number">Вопрос ${i + 1}</div>
                <label>${escapeHtml(q.question)}</label>
                ${q.hint ? `<div class="q-hint">${escapeHtml(q.hint)}</div>` : ""}
                <input type="text" placeholder="Ваш ответ…">
            `;
            list.appendChild(item);
        });
        // фокус на первом
        const firstInput = qs("input", list);
        if (firstInput) setTimeout(() => firstInput.focus(), 100);
    }
}


// ─── render: step 3 (result) ─────────────────────────────────────────────────

function renderResultStep(data) {
    const r = data.result;
    const primary = r.primary || {};
    const alternatives = r.alternatives || [];
    const griExplained = r.gri_explained || [];
    const checks = r.checks_required || [];

    const confEmoji = {
        high: "🟢",
        medium: "🟡",
        low: "🔴",
    }[primary.confidence] || "⚪";
    const confLabel = {
        high: "высокая",
        medium: "средняя",
        low: "низкая",
    }[primary.confidence] || "—";

    const hierarchyHtml = primary.hierarchy && primary.hierarchy.length
        ? `<div class="hierarchy">
            ${primary.hierarchy.map((h, i) => `
                <span class="hier-item">
                    <code>${escapeHtml(h.code)}</code>
                    <span>${escapeHtml(h.description)}</span>
                </span>
                ${i < primary.hierarchy.length - 1 ? '<span class="hier-arrow">→</span>' : ''}
            `).join("")}
           </div>`
        : "";

    const altsHtml = alternatives.length
        ? `<div class="result-section">
            <h3>Альтернативные коды</h3>
            <div class="alternatives">
                ${alternatives.map(a => `
                    <div class="alt-item">
                        <div class="alt-head">
                            <code>${escapeHtml(a.code || "—")}</code>
                            ${dutyBadge(a.duty_rate)}
                            <span class="alt-path">${escapeHtml(a.full_path || "")}</span>
                        </div>
                        ${a.why_close ? `<div class="alt-row"><span class="alt-label">Близок:</span> ${escapeHtml(a.why_close)}</div>` : ""}
                        ${a.why_rejected ? `<div class="alt-row"><span class="alt-label">Отвергнут:</span> ${escapeHtml(a.why_rejected)}</div>` : ""}
                    </div>
                `).join("")}
            </div>
           </div>`
        : "";

    const griHtml = griExplained.length
        ? `<div class="result-section">
            <h3>Применённые ОПИ</h3>
            <div class="gri-list">
                ${griExplained.map(g => `
                    <div class="gri-item">
                        <code class="gri-code">ОПИ ${escapeHtml(g.code)}</code>
                        <span>${escapeHtml(g.text)}</span>
                    </div>
                `).join("")}
            </div>
           </div>`
        : "";

    const checksHtml = checks.length
        ? `<div class="result-section">
            <h3>Что проверить вручную</h3>
            <ul class="checks-list">
                ${checks.map(c => `<li>${escapeHtml(c)}</li>`).join("")}
            </ul>
           </div>`
        : "";

    $("result-content").innerHTML = `
        <div class="card result-card">
            <div class="result-head">
                <div class="result-label">Код ТН ВЭД ЕАЭС</div>
                <div class="result-confidence">
                    ${confEmoji} Уверенность: <strong>${escapeHtml(confLabel)}</strong>
                </div>
            </div>

            <div class="result-code-row">
                <div class="result-code">${escapeHtml(primary.code || "—")}</div>
                ${dutyBadge(primary.duty_rate, "large")}
            </div>

            ${hierarchyHtml}

            <div class="result-reasoning">
                <h3>Обоснование</h3>
                <p>${escapeHtml(primary.reasoning || "")}</p>
            </div>

            ${griHtml}
            ${altsHtml}
            ${checksHtml}
        </div>
    `;
}


// ─── chat ─────────────────────────────────────────────────────────────────────

const chatState = {
    chatId: null,
    busy: false,
};

async function onChatSend() {
    if (chatState.busy) return;
    const input = $("chat-input");
    const sendBtn = $("chat-send-btn");
    const text = input.value.trim();
    if (!text) return;

    chatState.busy = true;
    setBusy(true);
    input.value = "";
    input.disabled = true;
    sendBtn.disabled = true;

    appendChatMessage({ role: "user", content: text });
    appendChatTyping();

    let finalized = false;
    try {
        const url = chatState.chatId
            ? `/api/chat/${chatState.chatId}/message`
            : `/api/chat/start`;
        const body = chatState.chatId
            ? { text, model: $("model-select").value }
            : { initial_description: text, model: $("model-select").value };

        const r = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        const data = await r.json();

        chatState.chatId = data.chat_id;
        renderChatMessages(data.messages);
        finalized = data.phase === "finalized";
    } catch (e) {
        removeChatTyping();
        appendChatMessage({ role: "assistant", content: `_Ошибка: ${e.message}_` });
    } finally {
        chatState.busy = false;
        setBusy(false);
        if (finalized) {
            input.placeholder = "Чат завершён. Нажмите «Начать заново».";
            // input + sendBtn остаются disabled
        } else {
            input.disabled = false;
            sendBtn.disabled = false;
            input.focus();
        }
    }
}

function onChatReset() {
    chatState.chatId = null;
    chatState.busy = false;
    $("chat-messages").innerHTML = "";
    const input = $("chat-input");
    input.value = "";
    input.disabled = false;
    input.placeholder = "Опишите товар или ответьте на уточняющий вопрос…";
    $("chat-send-btn").disabled = false;
    input.focus();
}

function renderChatMessages(messages) {
    const box = $("chat-messages");
    box.innerHTML = "";
    for (const m of messages) {
        appendChatMessage(m);
    }
}

function appendChatMessage(m) {
    const box = $("chat-messages");
    const wrap = document.createElement("div");
    wrap.className = `chat-msg chat-msg-${m.role}`;
    wrap.innerHTML = renderMarkdownLite(m.content || "");
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
}

function appendChatTyping() {
    const box = $("chat-messages");
    const wrap = document.createElement("div");
    wrap.className = "chat-msg chat-msg-assistant chat-typing";
    wrap.id = "chat-typing-indicator";
    wrap.innerHTML = `<span class="chat-typing-dot"></span><span class="chat-typing-dot"></span><span class="chat-typing-dot"></span>`;
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
}

function removeChatTyping() {
    const el = $("chat-typing-indicator");
    if (el) el.remove();
}

// Минимальный safe-renderer: bold (**...**), inline code (`...`), italics (_..._), переводы строк.
function renderMarkdownLite(s) {
    // Сначала эскейпим HTML
    let out = String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    // Inline code: `code`
    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold: **text**
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Italic: _text_
    out = out.replace(/(^|[\s(])_([^_]+)_(?=[\s.,!?)]|$)/g, '$1<em>$2</em>');
    // Списки: строки, начинающиеся с "- "
    const lines = out.split("\n");
    const result = [];
    let inList = false;
    for (const ln of lines) {
        if (/^- /.test(ln)) {
            if (!inList) { result.push("<ul>"); inList = true; }
            result.push(`<li>${ln.slice(2)}</li>`);
        } else {
            if (inList) { result.push("</ul>"); inList = false; }
            result.push(ln);
        }
    }
    if (inList) result.push("</ul>");
    return result.join("\n").replace(/\n/g, "<br>");
}


// ─── batch (xlsx) ─────────────────────────────────────────────────────────────

const batchState = {
    file: null,
    jobId: null,
    pollTimer: null,
};

function onBatchFileChosen(e) {
    const f = e.target.files && e.target.files[0];
    batchState.file = f || null;
    $("batch-filename").textContent = f ? f.name : "";
    $("batch-start-btn").disabled = !f;
}

async function onBatchStart() {
    if (!batchState.file) return;
    onBatchReset({ keepFile: true });

    const form = new FormData();
    form.append("file", batchState.file);
    form.append("model", $("model-select").value || "");

    setBusy(true);
    setStatus("Загружаем файл и запускаем обработку…");

    try {
        const r = await fetch("/api/classify/batch", { method: "POST", body: form });
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        const data = await r.json();
        batchState.jobId = data.job_id;
        $("batch-progress").hidden = false;
        $("batch-progress-label").textContent = `Обработано: 0 / ${data.total}`;
        $("batch-progress-status").textContent = "running";
        $("batch-progress-bar").style.width = "0%";
        $("batch-start-btn").disabled = true;
        startBatchPolling();
    } catch (e) {
        flashError(`Ошибка: ${e.message}`);
        $("batch-start-btn").disabled = false;
    } finally {
        setBusy(false);
        setStatus("");
    }
}

function startBatchPolling() {
    stopBatchPolling();
    batchState.pollTimer = setInterval(pollBatchStatus, 1500);
    pollBatchStatus();
}

function stopBatchPolling() {
    if (batchState.pollTimer) {
        clearInterval(batchState.pollTimer);
        batchState.pollTimer = null;
    }
}

async function pollBatchStatus() {
    if (!batchState.jobId) return;
    try {
        const r = await fetch(`/api/classify/batch/${batchState.jobId}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const s = await r.json();
        const pct = s.total > 0 ? Math.round((s.processed / s.total) * 100) : 0;
        $("batch-progress-label").textContent = `Обработано: ${s.processed} / ${s.total}`;
        $("batch-progress-status").textContent = s.status;
        $("batch-progress-bar").style.width = `${pct}%`;
        if (s.errors_count > 0) {
            $("batch-progress-errors").textContent = `Ошибок при обработке строк: ${s.errors_count}`;
        }
        if (s.status === "done" || s.status === "failed") {
            stopBatchPolling();
            $("batch-download-link").href = `/api/classify/batch/${batchState.jobId}/download`;
            $("batch-done").hidden = false;
        }
    } catch (e) {
        // не убиваем polling из-за разовой сетевой ошибки
        console.warn("polling error:", e);
    }
}

function onBatchReset(opts = {}) {
    stopBatchPolling();
    batchState.jobId = null;
    if (!opts.keepFile) {
        batchState.file = null;
        $("batch-file").value = "";
        $("batch-filename").textContent = "";
        $("batch-start-btn").disabled = true;
    }
    $("batch-progress").hidden = true;
    $("batch-done").hidden = true;
    $("batch-progress-errors").textContent = "";
}


// ─── utilities ────────────────────────────────────────────────────────────────

function dutyBadge(rate, size) {
    if (rate === null || rate === undefined || rate === "") return "";
    const cls = size === "large" ? "duty-badge duty-badge-large" : "duty-badge";
    return `<span class="${cls}" title="Импортная пошлина (TWS.BY)">Пошлина: ${escapeHtml(rate)}</span>`;
}

function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = String(s ?? "");
    return div.innerHTML;
}

function setStatus(text) {
    $("status").textContent = text;
}

function setBusy(busy) {
    qsa("button.primary").forEach(b => b.disabled = busy);
}

function flashError(text) {
    setStatus("");
    const status = $("status");
    status.innerHTML = `<span style="color: var(--error)">${escapeHtml(text)}</span>`;
    setTimeout(() => { if (status.textContent === text) status.textContent = ""; }, 4000);
}
