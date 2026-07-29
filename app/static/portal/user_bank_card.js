"use strict";

const ALLOWED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png"];
const BANK_CARD_REASON_MESSAGES = {
  image_blur: "图片清晰度不足，请重新拍摄。",
  image_dark: "图片过暗，请在光线充足的环境下重新拍摄。",
  image_bright: "图片过亮，请避免强光直射。",
  glare_detected: "图片存在明显反光，请调整拍摄角度。",
  missing_card_number: "未能识别银行卡号，请重新上传清晰图片。",
  missing_valid_date: "未能识别有效期，请确保卡片信息完整。",
  missing_name: "未能识别持卡人姓名，请重新上传清晰图片。",
  invalid_card_number: "银行卡号未通过规则校验。",
  invalid_valid_date: "银行卡有效期格式异常。",
};

document.addEventListener("DOMContentLoaded", () => {
  const fileInput = document.querySelector("#bankCardFileInput");
  const dropZone = document.querySelector("#bankCardDropZone");
  const selectedFilePanel = document.querySelector("#bankCardSelectedFile");
  const preview = document.querySelector("#bankCardPreview");
  const fileName = document.querySelector("#bankCardFileName");
  const removeButton = document.querySelector("#bankCardRemoveButton");
  const submitButton = document.querySelector("#bankCardSubmitButton");
  const result = document.querySelector("#bankCardResult");
  const resultStatus = document.querySelector("#bankCardResultStatus");
  const resultTitle = document.querySelector("#bankCardResultTitle");
  const resultMessage = document.querySelector("#bankCardResultMessage");
  const requestRow = document.querySelector("#bankCardRequestRow");
  const requestId = document.querySelector("#bankCardRequestId");

  let selectedFile = null;
  let previewUrl = null;
  let submitting = false;

  function revokePreviewUrl() {
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = null;
    }
  }

  function renderResult(state, title, message, currentRequestId = "") {
    const statusLabels = {
      idle: "未提交",
      loading: "处理中",
      pass: "已通过",
      review: "待处理",
      reject: "未通过",
      error: "处理失败",
    };

    result.dataset.state = state;
    resultStatus.dataset.state = state;
    resultStatus.textContent = statusLabels[state] || "状态未知";
    resultStatus.classList.toggle("neutral", state === "idle");
    resultTitle.textContent = title;
    resultMessage.textContent = message;
    requestId.textContent = currentRequestId;
    requestRow.hidden = !currentRequestId;
  }

  function clearResult() {
    renderResult("idle", "暂无认证结果", "请先上传银行卡图片并提交认证。");
  }

  function hasAllowedExtension(file) {
    const normalizedName = file.name.toLowerCase();
    return ALLOWED_IMAGE_EXTENSIONS.some((extension) => normalizedName.endsWith(extension));
  }

  function clearSelectedFile() {
    selectedFile = null;
    fileInput.value = "";
    revokePreviewUrl();
    preview.removeAttribute("src");
    fileName.textContent = "--";
    selectedFilePanel.hidden = true;
    submitButton.disabled = true;
    clearResult();
  }

  function selectFile(file) {
    if (!file || !hasAllowedExtension(file)) {
      clearSelectedFile();
      renderResult("error", "文件格式不支持", "请选择 JPG、JPEG 或 PNG 格式的银行卡图片。");
      return;
    }

    selectedFile = file;
    fileInput.value = "";
    revokePreviewUrl();
    previewUrl = URL.createObjectURL(file);
    preview.src = previewUrl;
    fileName.textContent = file.name;
    selectedFilePanel.hidden = false;
    submitButton.disabled = false;
    clearResult();
  }

  function userMessageForReasons(reasons) {
    if (!Array.isArray(reasons)) {
      return "图片或证件信息需要进一步核验。";
    }

    for (const reason of reasons) {
      if (typeof reason === "string" && BANK_CARD_REASON_MESSAGES[reason]) {
        return BANK_CARD_REASON_MESSAGES[reason];
      }
    }
    return "图片或证件信息需要进一步核验。";
  }

  function renderReviewResponse(data) {
    const status = typeof data.review_result === "string" ? data.review_result : "";
    const currentRequestId = typeof data.request_id === "string" ? data.request_id : "";

    if (status === "pass") {
      renderResult("pass", "认证通过", "银行卡图片审核已通过。", currentRequestId);
    } else if (status === "review") {
      renderResult(
        "review",
        "需要进一步处理",
        userMessageForReasons(data.review_reasons),
        currentRequestId,
      );
    } else if (status === "reject") {
      renderResult("reject", "认证未通过", "银行卡信息未通过规则校验。", currentRequestId);
    } else if (status === "error") {
      renderResult("error", "处理失败", "图片处理失败，请检查文件后重试。", currentRequestId);
    } else {
      renderResult("error", "结果未知", "系统返回了无法识别的审核状态。", currentRequestId);
    }
  }

  function userMessageForErrorDetail(detail) {
    if (typeof detail !== "string") {
      return "图片处理失败，请检查文件后重试。";
    }
    if (detail.includes("Unsupported file type")) {
      return "请选择 JPG、JPEG 或 PNG 格式的银行卡图片。";
    }
    if (detail.includes("file is empty")) {
      return "所选图片为空，请重新选择。";
    }
    if (detail.includes("not a readable image")) {
      return "无法读取所选图片，请重新拍摄或选择其他图片。";
    }
    return "图片处理失败，请检查文件后重试。";
  }

  async function submitReview() {
    if (!selectedFile || submitting) {
      return;
    }

    submitting = true;
    submitButton.disabled = true;
    submitButton.textContent = "认证中...";
    renderResult("loading", "正在认证", "图片正在处理中，请稍候。");

    const formData = new FormData();
    formData.append("file", selectedFile, selectedFile.name);

    try {
      const response = await fetch("/bank-card/review", {
        method: "POST",
        body: formData,
      });

      if (response.status === 401) {
        renderResult(
          "error",
          "登录状态已失效",
          "登录状态已失效，请重新登录。",
        );
        window.location.href = "/login";
        return;
      }

      let data;
      try {
        data = await response.json();
      } catch {
        renderResult("error", "处理失败", "服务返回异常，请稍后重试。");
        return;
      }

      if (!response.ok) {
        const currentRequestId = typeof data.request_id === "string" ? data.request_id : "";
        renderResult(
          "error",
          "处理失败",
          userMessageForErrorDetail(data.detail),
          currentRequestId,
        );
        return;
      }

      renderReviewResponse(data);
    } catch {
      renderResult("error", "网络连接失败", "暂时无法连接服务，请检查网络后重试。");
    } finally {
      submitting = false;
      submitButton.textContent = "提交认证";
      submitButton.disabled = !selectedFile;
    }
  }

  fileInput.addEventListener("change", () => {
    selectFile(fileInput.files?.[0] || null);
  });

  dropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });

  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("dragging");
  });

  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
    selectFile(event.dataTransfer?.files?.[0] || null);
  });

  removeButton.addEventListener("click", clearSelectedFile);
  submitButton.addEventListener("click", submitReview);
  window.addEventListener("beforeunload", revokePreviewUrl);
});
