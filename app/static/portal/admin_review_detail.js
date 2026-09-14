"use strict";

const DETAIL_DOC_TYPE_LABELS = {
  bank_card: "银行卡",
  id_card: "身份证",
};

const DETAIL_REVIEW_RESULT_LABELS = {
  pass: "通过",
  review: "待复核",
  reject: "拒绝",
  error: "错误",
};

document.addEventListener("DOMContentLoaded", () => {
  const status = document.querySelector("#adminReviewDetailStatus");
  const statusTitle = document.querySelector("#adminReviewDetailStatusTitle");
  const statusMessage = document.querySelector("#adminReviewDetailStatusMessage");
  const content = document.querySelector("#adminReviewDetailContent");
  const requestIdField = document.querySelector("#detailRequestId");
  const docTypeField = document.querySelector("#detailDocType");
  const filenameField = document.querySelector("#detailFilename");
  const ocrModeField = document.querySelector("#detailOcrMode");
  const createdAtField = document.querySelector("#detailCreatedAt");
  const reviewResultField = document.querySelector("#detailReviewResult");
  const qualityResultField = document.querySelector("#detailQualityResult");
  const errorMessageField = document.querySelector("#detailErrorMessage");
  const qualityReasonsField = document.querySelector("#detailQualityReasons");
  const reviewReasonsField = document.querySelector("#detailReviewReasons");
  const fieldsJsonField = document.querySelector("#detailFieldsJson");
  const aiStatus = document.querySelector("#aiAssistantStatus");
  const aiExplainButton = document.querySelector("#aiExplainButton");
  const aiMessage = document.querySelector("#aiAssistantMessage");
  const aiResult = document.querySelector("#aiAssistantResult");
  const aiExplanationText = document.querySelector("#aiExplanationText");
  const aiReasonList = document.querySelector("#aiReasonList");
  const aiActionList = document.querySelector("#aiActionList");
  const aiCitationList = document.querySelector("#aiCitationList");
  const aiMeta = document.querySelector("#aiMeta");
  let currentRequestId = null;

  function textValue(value, fallback = "无") {
    if (value === null || value === undefined || value === "") {
      return fallback;
    }
    return String(value);
  }

  function docTypeLabel(value) {
    if (typeof value !== "string" || value === "") {
      return "未知类型";
    }
    return DETAIL_DOC_TYPE_LABELS[value] || value;
  }

  function reviewResultLabel(value) {
    if (typeof value !== "string" || value === "") {
      return "未知状态";
    }
    return DETAIL_REVIEW_RESULT_LABELS[value] || "未知状态";
  }

  function localTimestamp(value) {
    if (typeof value !== "string" || value === "") {
      return "无";
    }
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }

  function setStatus(title, message, state) {
    status.hidden = false;
    status.dataset.state = state;
    statusTitle.textContent = title;
    statusMessage.textContent = message;
  }

  function renderReasons(container, reasons) {
    container.replaceChildren();
    if (!Array.isArray(reasons) || reasons.length === 0) {
      const item = document.createElement("li");
      item.textContent = "无";
      container.append(item);
      return;
    }

    for (const reason of reasons) {
      const item = document.createElement("li");
      item.textContent = textValue(reason);
      container.append(item);
    }
  }

  function structuredValue(value) {
    if (value === null || value === undefined || value === "") {
      return "无";
    }
    if (typeof value === "object") {
      try {
        return JSON.stringify(value);
      } catch {
        return String(value);
      }
    }
    return String(value);
  }

  function normalizedFields(value) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return value;
    }
    if (typeof value !== "string" || value === "") {
      return null;
    }
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }

  function renderFields(value) {
    fieldsJsonField.replaceChildren();
    const fields = normalizedFields(value);
    if (!fields) {
      const plainText = document.createElement("p");
      plainText.className = "plain-field-value";
      plainText.textContent = textValue(value);
      fieldsJsonField.append(plainText);
      return;
    }

    const entries = Object.entries(fields);
    if (entries.length === 0) {
      const emptyText = document.createElement("p");
      emptyText.className = "plain-field-value";
      emptyText.textContent = "无";
      fieldsJsonField.append(emptyText);
      return;
    }

    const list = document.createElement("dl");
    list.className = "admin-field-list";
    for (const [key, fieldValue] of entries) {
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = key;
      description.textContent = structuredValue(fieldValue);
      row.append(term, description);
      list.append(row);
    }
    fieldsJsonField.append(list);
  }

  const AI_STATE_LABELS = {
    idle: "未请求",
    loading: "正在生成",
    ready: "已生成",
    degraded: "已降级",
    error: "请求失败",
  };

  const AI_ROOT_CAUSE_LABELS = {
    quality: "影像质量层",
    field: "字段解析层",
    infrastructure: "服务端或调用方",
    unknown: "语料未收录",
  };

  function setAiState(state) {
    aiStatus.dataset.state = state;
    aiStatus.textContent = AI_STATE_LABELS[state] || AI_STATE_LABELS.idle;
  }

  function setAiMessage(message, tone) {
    aiMessage.textContent = message || "";
    if (tone) {
      aiMessage.dataset.tone = tone;
    } else {
      delete aiMessage.dataset.tone;
    }
  }

  function appendDefinitionRow(list, term, description) {
    const row = document.createElement("div");
    const termNode = document.createElement("dt");
    const descriptionNode = document.createElement("dd");
    termNode.textContent = term;
    descriptionNode.textContent = description;
    row.append(termNode, descriptionNode);
    list.append(row);
  }

  function renderAiReasonDetails(reasonDetails) {
    aiReasonList.replaceChildren();
    if (!Array.isArray(reasonDetails) || reasonDetails.length === 0) {
      const empty = document.createElement("li");
      empty.className = "ai-empty";
      empty.textContent = "本次审核记录没有原因码，无需逐条解释。";
      aiReasonList.append(empty);
      return;
    }

    for (const detail of reasonDetails) {
      const item = document.createElement("li");
      item.className = "ai-reason-item";
      const record = detail && typeof detail === "object" ? detail : {};
      if (record.root_cause) {
        item.dataset.rootCause = String(record.root_cause);
      }

      const heading = document.createElement("div");
      heading.className = "ai-reason-head";
      const code = document.createElement("strong");
      code.textContent = textValue(record.code);
      const tag = document.createElement("span");
      tag.className = "ai-reason-tag";
      tag.textContent = AI_ROOT_CAUSE_LABELS[record.root_cause] || "未分类";
      heading.append(code, tag);
      item.append(heading);

      if (record.known === false) {
        const unknownNote = document.createElement("p");
        unknownNote.className = "ai-reason-unknown";
        unknownNote.textContent = "该原因码尚未收录到知识库语料，请人工确认含义。";
        item.append(unknownNote);
      }

      const rows = document.createElement("dl");
      rows.className = "ai-reason-detail";
      if (record.meaning) {
        appendDefinitionRow(rows, "含义", record.meaning);
      }
      if (record.trigger) {
        appendDefinitionRow(rows, "触发条件", record.trigger);
      }
      if (record.implementation) {
        appendDefinitionRow(rows, "实现位置", record.implementation);
      }
      if (record.advice) {
        appendDefinitionRow(rows, "处置建议", record.advice);
      }
      if (record.user_message) {
        appendDefinitionRow(rows, "用户提示话术", record.user_message);
      }
      if (rows.childElementCount > 0) {
        item.append(rows);
      }
      aiReasonList.append(item);
    }
  }

  function renderAiTextList(container, items, emptyText, itemClassName) {
    container.replaceChildren();
    if (!Array.isArray(items) || items.length === 0) {
      const empty = document.createElement("li");
      empty.className = "ai-empty";
      empty.textContent = emptyText;
      container.append(empty);
      return;
    }

    for (const entry of items) {
      const item = document.createElement("li");
      item.className = itemClassName;
      if (entry && typeof entry === "object") {
        const title = document.createElement("strong");
        title.textContent = textValue(entry.title);
        const snippet = document.createElement("span");
        snippet.className = "ai-citation-snippet";
        snippet.textContent = textValue(entry.snippet);
        item.append(title, snippet);
        const meta = document.createElement("span");
        meta.className = "ai-citation-meta";
        const category = textValue(entry.category, "未分类");
        const score = typeof entry.score === "number" ? entry.score.toFixed(2) : "无";
        meta.textContent = `分类 ${category} · 相关度 ${score}`;
        item.append(meta);
      } else {
        item.textContent = textValue(entry);
      }
      container.append(item);
    }
  }

  function renderAiMeta(result) {
    aiMeta.replaceChildren();
    const engine = result.engine && typeof result.engine === "object" ? result.engine : {};
    const rows = [
      [
        "解释生成",
        engine.generation === "llm" ? "大模型生成" : "模板生成（未配置大模型）",
      ],
      ["查询改写", engine.rewrite === "llm" ? "大模型改写" : "规则同义词扩展"],
      ["结果重排", engine.rerank === "llm" ? "大模型重排" : "共识度重排"],
      ["检索后端", textValue(engine.retrieval, "未知")],
      [
        "置信度",
        typeof result.confidence === "number" ? result.confidence.toFixed(2) : "无",
      ],
      [
        "耗时",
        typeof result.latency_ms === "number"
          ? result.latency_ms < 1
            ? "< 1 ms"
            : `${Math.round(result.latency_ms)} ms`
          : "无",
      ],
    ];
    for (const [term, value] of rows) {
      appendDefinitionRow(aiMeta, term, value);
    }
  }

  function renderAiResult(result) {
    aiExplanationText.textContent = textValue(result.explanation, "AI 未返回解释正文。");
    renderAiReasonDetails(result.reason_details);
    renderAiTextList(aiActionList, result.actions, "本次未生成额外处置建议。", "ai-action-item");
    renderAiTextList(
      aiCitationList,
      result.citations,
      "本次没有检索到可引用的知识片段。",
      "ai-citation-item",
    );
    renderAiMeta(result);
    aiResult.hidden = false;
  }

  async function requestAiExplanation() {
    if (!currentRequestId) {
      setAiState("error");
      setAiMessage("审核记录尚未加载完成，请稍后再试。", "warning");
      return;
    }
    if (!window.portalSecurity || typeof window.portalSecurity.fetchWithCsrf !== "function") {
      setAiState("error");
      setAiMessage("页面安全组件未就绪，请刷新页面后重试。", "warning");
      return;
    }

    setAiState("loading");
    setAiMessage("正在检索审核知识库并生成解释，请稍候。", null);
    aiResult.hidden = true;
    aiExplainButton.disabled = true;

    try {
      const response = await window.portalSecurity.fetchWithCsrf(
        `/ai/explain/${encodeURIComponent(currentRequestId)}`,
        { method: "POST" },
      );

      if (response.status === 401) {
        setAiState("error");
        setAiMessage("登录状态已失效，请重新登录。", "warning");
        return;
      }
      if (response.status === 403) {
        setAiState("error");
        setAiMessage("当前账号没有调用 AI 复核助手的权限。", "warning");
        return;
      }
      if (response.status === 404) {
        setAiState("error");
        setAiMessage("未找到该审核记录，无法生成解释。", "warning");
        return;
      }
      if (!response.ok) {
        throw new Error("ai explain request failed");
      }

      const result = await response.json();
      if (!result || typeof result !== "object" || Array.isArray(result)) {
        throw new Error("ai explain response is invalid");
      }

      if (result.available !== true) {
        setAiState("degraded");
        setAiMessage(textValue(result.message, "AI 复核服务暂不可用。"), "warning");
        aiResult.hidden = true;
        return;
      }

      setAiState("ready");
      const unknownCodes = Array.isArray(result.unknown_reason_codes)
        ? result.unknown_reason_codes
        : [];
      if (unknownCodes.length > 0) {
        setAiMessage(
          `以下原因码尚未收录知识库语料，需人工确认：${unknownCodes.join("、")}`,
          "warning",
        );
      } else if (result.degraded === true) {
        setAiMessage(
          "未配置大模型，已用模板与规则检索生成解释；事实内容取自知识库，可正常参考。",
          "note",
        );
      } else {
        setAiMessage("解释已生成，请结合影像与解析字段自行核对。", null);
      }
      renderAiResult(result);
    } catch {
      setAiState("error");
      setAiMessage("AI 复核助手请求失败，请稍后重试。", "warning");
    } finally {
      aiExplainButton.disabled = false;
    }
  }

  function renderRecord(record) {
    requestIdField.textContent = textValue(record.request_id);
    docTypeField.textContent = docTypeLabel(record.doc_type);
    filenameField.textContent = textValue(record.filename);
    ocrModeField.textContent = textValue(record.ocr_mode);
    createdAtField.textContent = localTimestamp(record.created_at);
    reviewResultField.textContent = reviewResultLabel(record.review_result);
    qualityResultField.textContent = textValue(record.quality_result);
    errorMessageField.textContent = textValue(record.error_message);
    renderReasons(qualityReasonsField, record.quality_reasons);
    renderReasons(reviewReasonsField, record.review_reasons);
    renderFields(record.fields_json);
    status.hidden = true;
    content.hidden = false;
  }

  function requestIdFromPath() {
    const encodedRequestId = window.location.pathname.split("/").pop() || "";
    try {
      return decodeURIComponent(encodedRequestId);
    } catch {
      return null;
    }
  }

  async function loadRecord() {
    const requestId = requestIdFromPath();
    if (!requestId) {
      setStatus("审核记录加载失败", "审核记录加载失败，请稍后重试。", "error");
      return;
    }

    setStatus("正在加载审核详情", "正在根据 request_id 查询数据库记录。", "loading");
    try {
      const response = await fetch(`/review-records/${encodeURIComponent(requestId)}`);
      if (response.status === 401) {
        content.hidden = true;
        setStatus("登录状态已失效", "登录状态已失效，请重新登录。", "error");
        window.location.href = "/login";
        return;
      }
      if (response.status === 403) {
        content.hidden = true;
        setStatus(
          "无权查看审核记录",
          "当前账号没有查看审核记录的权限。",
          "error",
        );
        return;
      }
      if (response.status === 404) {
        setStatus("未找到该审核记录", "请检查 request_id 是否完整、正确。", "error");
        return;
      }
      if (!response.ok) {
        throw new Error("review detail request failed");
      }
      const record = await response.json();
      if (!record || typeof record !== "object" || Array.isArray(record)) {
        throw new Error("review detail response is invalid");
      }
      renderRecord(record);
      currentRequestId =
        typeof record.request_id === "string" && record.request_id
          ? record.request_id
          : requestId;
    } catch {
      setStatus("审核记录加载失败", "审核记录加载失败，请稍后重试。", "error");
    }
  }

  setAiState("idle");
  aiExplainButton.addEventListener("click", requestAiExplanation);
  loadRecord();
});
