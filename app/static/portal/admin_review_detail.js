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
    } catch {
      setStatus("审核记录加载失败", "审核记录加载失败，请稍后重试。", "error");
    }
  }

  loadRecord();
});
