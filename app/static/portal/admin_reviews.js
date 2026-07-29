"use strict";

const ADMIN_DOC_TYPE_LABELS = {
  bank_card: "银行卡",
  id_card: "身份证",
};

const ADMIN_REVIEW_RESULT_LABELS = {
  pass: "通过",
  review: "待复核",
  reject: "拒绝",
  error: "错误",
};

document.addEventListener("DOMContentLoaded", () => {
  const filterForm = document.querySelector("#adminReviewFilterForm");
  const docTypeFilter = document.querySelector("#adminDocTypeFilter");
  const reviewResultFilter = document.querySelector("#adminReviewResultFilter");
  const filterButton = document.querySelector("#adminFilterButton");
  const resetButton = document.querySelector("#adminResetButton");
  const requestSearchForm = document.querySelector("#adminRequestSearchForm");
  const requestIdSearch = document.querySelector("#adminRequestIdSearch");
  const listStatus = document.querySelector("#adminReviewListStatus");
  const listMessage = document.querySelector("#adminReviewListMessage");
  const tableBody = document.querySelector("#adminReviewTableBody");

  function textValue(value, fallback = "—") {
    if (value === null || value === undefined || value === "") {
      return fallback;
    }
    return String(value);
  }

  function docTypeLabel(value) {
    if (typeof value !== "string" || value === "") {
      return "未知类型";
    }
    return ADMIN_DOC_TYPE_LABELS[value] || value;
  }

  function reviewResultLabel(value) {
    if (typeof value !== "string" || value === "") {
      return "未知状态";
    }
    return ADMIN_REVIEW_RESULT_LABELS[value] || "未知状态";
  }

  function localTimestamp(value) {
    if (typeof value !== "string" || value === "") {
      return "—";
    }
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }

  function setListStatus(state, label, message = "") {
    listStatus.dataset.state = state;
    listStatus.textContent = label;
    listStatus.classList.toggle("neutral", state === "idle");
    listMessage.textContent = message;
    listMessage.dataset.state = state;
  }

  function appendCell(row, value, className = "") {
    const cell = document.createElement("td");
    cell.textContent = value;
    if (className) {
      cell.className = className;
    }
    row.append(cell);
    return cell;
  }

  function renderTableMessage(title, message, state) {
    tableBody.replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    const container = document.createElement("div");
    const icon = document.createElement("span");
    const heading = document.createElement("strong");
    const detail = document.createElement("p");

    cell.colSpan = 7;
    container.className = "empty-state table-empty";
    container.dataset.state = state;
    icon.className = "empty-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = state === "loading" ? "…" : "□";
    heading.textContent = title;
    detail.textContent = message;

    container.append(icon, heading, detail);
    cell.append(container);
    row.append(cell);
    tableBody.append(row);
  }

  function renderRecords(records) {
    tableBody.replaceChildren();
    if (records.length === 0) {
      renderTableMessage("暂无符合条件的审核记录", "请调整筛选条件后重新查询。", "empty");
      return;
    }

    for (const record of records) {
      const row = document.createElement("tr");
      const requestId = typeof record.request_id === "string" ? record.request_id : "";

      appendCell(row, localTimestamp(record.created_at));
      const requestCell = appendCell(row, textValue(requestId), "request-id-cell");
      requestCell.title = requestId;
      appendCell(row, docTypeLabel(record.doc_type));
      appendCell(row, textValue(record.filename));
      appendCell(row, textValue(record.ocr_mode));

      const resultCell = appendCell(
        row,
        reviewResultLabel(record.review_result),
        "admin-result-cell",
      );
      if (typeof record.review_result === "string") {
        resultCell.dataset.state = record.review_result;
      }

      const actionCell = document.createElement("td");
      if (requestId) {
        const detailLink = document.createElement("a");
        detailLink.className = "table-action-link";
        detailLink.href = `/admin/reviews/${encodeURIComponent(requestId)}`;
        detailLink.textContent = "查看详情";
        actionCell.append(detailLink);
      } else {
        actionCell.textContent = "不可查看";
      }
      row.append(actionCell);
      tableBody.append(row);
    }
  }

  async function loadRecords() {
    const params = new URLSearchParams();
    if (docTypeFilter.value) {
      params.set("doc_type", docTypeFilter.value);
    }
    if (reviewResultFilter.value) {
      params.set("review_result", reviewResultFilter.value);
    }

    const query = params.toString();
    const endpoint = query ? `/review-records?${query}` : "/review-records";
    filterButton.disabled = true;
    resetButton.disabled = true;
    setListStatus("loading", "加载中", "正在加载审核记录…");
    renderTableMessage("正在加载审核记录", "请稍候。", "loading");

    try {
      const response = await fetch(endpoint);
      if (response.status === 401) {
        renderTableMessage(
          "登录状态已失效",
          "登录状态已失效，请重新登录。",
          "error",
        );
        setListStatus("error", "登录失效", "登录状态已失效，请重新登录。");
        window.location.href = "/login";
        return;
      }
      if (response.status === 403) {
        renderTableMessage(
          "无权查看审核记录",
          "当前账号没有查看审核记录的权限。",
          "error",
        );
        setListStatus(
          "error",
          "无权限",
          "当前账号没有查看审核记录的权限。",
        );
        return;
      }
      if (!response.ok) {
        throw new Error("review list request failed");
      }
      const records = await response.json();
      if (!Array.isArray(records)) {
        throw new Error("review list response is not an array");
      }
      renderRecords(records);
      setListStatus("pass", `已加载 ${records.length} 条`, "");
    } catch {
      renderTableMessage(
        "审核记录加载失败",
        "审核记录加载失败，请稍后重试。",
        "error",
      );
      setListStatus("error", "加载失败", "审核记录加载失败，请稍后重试。");
    } finally {
      filterButton.disabled = false;
      resetButton.disabled = false;
    }
  }

  filterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    loadRecords();
  });

  resetButton.addEventListener("click", () => {
    docTypeFilter.value = "";
    reviewResultFilter.value = "";
    loadRecords();
  });

  requestSearchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const requestId = requestIdSearch.value.trim();
    if (!requestId) {
      return;
    }
    window.location.assign(`/admin/reviews/${encodeURIComponent(requestId)}`);
  });

  loadRecords();
});
