"use strict";

const ALLOWED_ID_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png"];
const ID_CARD_REASON_MESSAGES = {
  image_blur: "图片清晰度不足，请重新拍摄。",
  image_dark: "图片过暗，请在光线充足的环境下重新拍摄。",
  image_bright: "图片过亮，请避免强光直射。",
  glare_detected: "图片存在明显反光，请调整拍摄角度。",
  unknown_id_card_side: "未能判断身份证人像面或国徽面，请重新上传清晰完整的图片。",
  missing_name: "未能识别人像面姓名信息。",
  missing_id_number: "未能识别人像面身份证号码。",
  missing_issue_authority: "未能识别国徽面签发机关。",
  missing_valid_period: "未能识别国徽面有效期限。",
};

document.addEventListener("DOMContentLoaded", () => {
  const submitButton = document.querySelector("#idCardSubmitButton");
  const result = document.querySelector("#idCardResult");
  const resultStatus = document.querySelector("#idCardResultStatus");
  let submitting = false;

  function sideElements(prefix, label, expectedSide) {
    return {
      label,
      expectedSide,
      fileInput: document.querySelector(`#${prefix}FileInput`),
      dropZone: document.querySelector(`#${prefix}DropZone`),
      selectedFilePanel: document.querySelector(`#${prefix}SelectedFile`),
      preview: document.querySelector(`#${prefix}Preview`),
      fileName: document.querySelector(`#${prefix}FileName`),
      removeButton: document.querySelector(`#${prefix}RemoveButton`),
      resultPanel: document.querySelector(`#${prefix}Result`),
      resultStatus: document.querySelector(`#${prefix}ResultStatus`),
      resultMessage: document.querySelector(`#${prefix}ResultMessage`),
      detectedSideRow: document.querySelector(`#${prefix}DetectedSideRow`),
      detectedSide: document.querySelector(`#${prefix}DetectedSide`),
      requestRow: document.querySelector(`#${prefix}RequestRow`),
      requestId: document.querySelector(`#${prefix}RequestId`),
      selectedFile: null,
      previewUrl: null,
    };
  }

  const front = sideElements("idCardFront", "人像面", "front");
  const back = sideElements("idCardBack", "国徽面", "back");
  const sides = [front, back];

  function sideLabel(value) {
    if (value === "front") {
      return "人像面";
    }
    if (value === "back") {
      return "国徽面";
    }
    return "未能判断证件面";
  }

  function statusLabel(state) {
    const labels = {
      idle: "未提交",
      loading: "处理中",
      pass: "已通过",
      review: "待处理",
      reject: "未通过",
      error: "处理失败",
    };
    return labels[state] || "状态未知";
  }

  function renderOverallStatus(state, label = "") {
    result.dataset.state = state;
    resultStatus.dataset.state = state;
    resultStatus.textContent = label || statusLabel(state);
    resultStatus.classList.toggle("neutral", state === "idle");
  }

  function renderSideResult(
    side,
    state,
    message,
    detectedSide = "",
    currentRequestId = "",
  ) {
    side.resultPanel.dataset.state = state;
    side.resultStatus.textContent = statusLabel(state);
    side.resultMessage.textContent = message;
    side.detectedSide.textContent = detectedSide;
    side.detectedSideRow.hidden = !detectedSide;
    side.requestId.textContent = currentRequestId;
    side.requestRow.hidden = !currentRequestId;
  }

  function clearAllResults() {
    renderSideResult(front, "idle", "请先上传人像面图片。");
    renderSideResult(back, "idle", "请先上传国徽面图片。");
    renderOverallStatus("idle");
  }

  function revokePreviewUrl(side) {
    if (side.previewUrl) {
      URL.revokeObjectURL(side.previewUrl);
      side.previewUrl = null;
    }
  }

  function updateSubmitButton() {
    submitButton.disabled = submitting || !front.selectedFile || !back.selectedFile;
  }

  function hasAllowedExtension(file) {
    const normalizedName = file.name.toLowerCase();
    return ALLOWED_ID_IMAGE_EXTENSIONS.some((extension) => normalizedName.endsWith(extension));
  }

  function clearSelectedFile(side, clearResults = true) {
    side.selectedFile = null;
    side.fileInput.value = "";
    revokePreviewUrl(side);
    side.preview.removeAttribute("src");
    side.fileName.textContent = "--";
    side.selectedFilePanel.hidden = true;
    if (clearResults) {
      clearAllResults();
    }
    updateSubmitButton();
  }

  function selectFile(side, file) {
    if (!file || !hasAllowedExtension(file)) {
      clearSelectedFile(side);
      renderSideResult(
        side,
        "error",
        `请选择 JPG、JPEG 或 PNG 格式的${side.label}图片。`,
      );
      renderOverallStatus("error");
      return;
    }

    side.selectedFile = file;
    side.fileInput.value = "";
    revokePreviewUrl(side);
    side.previewUrl = URL.createObjectURL(file);
    side.preview.src = side.previewUrl;
    side.fileName.textContent = file.name;
    side.selectedFilePanel.hidden = false;
    clearAllResults();
    updateSubmitButton();
  }

  function userMessageForReasons(reasons) {
    if (!Array.isArray(reasons)) {
      return "图片或证件信息需要进一步核验。";
    }
    for (const reason of reasons) {
      if (typeof reason === "string" && ID_CARD_REASON_MESSAGES[reason]) {
        return ID_CARD_REASON_MESSAGES[reason];
      }
    }
    return "图片或证件信息需要进一步核验。";
  }

  function userMessageForErrorDetail(detail) {
    if (typeof detail !== "string") {
      return "图片处理失败，请检查文件后重试。";
    }
    if (detail.includes("Unsupported file type")) {
      return "请选择 JPG、JPEG 或 PNG 格式的身份证图片。";
    }
    if (detail.includes("file is empty")) {
      return "所选图片为空，请重新选择。";
    }
    if (detail.includes("not a readable image")) {
      return "无法读取所选图片，请重新拍摄或选择其他图片。";
    }
    return "图片处理失败，请检查文件后重试。";
  }

  function responseState(value) {
    return ["pass", "review", "reject", "error"].includes(value) ? value : "error";
  }

  function responseMessage(data, state) {
    if (state === "pass") {
      return "该证件面审核已通过。";
    }
    if (state === "review") {
      return userMessageForReasons(data.review_reasons);
    }
    if (state === "reject") {
      return "身份证信息未通过规则校验。";
    }
    return "图片处理失败，请检查文件后重试。";
  }

  async function submitSide(side) {
    renderSideResult(side, "loading", `${side.label}正在处理中，请稍候。`);
    const formData = new FormData();
    formData.append("file", side.selectedFile, side.selectedFile.name);

    try {
      const response = await fetch("/id-card/review", {
        method: "POST",
        body: formData,
      });

      if (response.status === 401) {
        renderSideResult(
          side,
          "error",
          "登录状态已失效，请重新登录。",
        );
        window.location.href = "/login";
        return "error";
      }

      let data;
      try {
        data = await response.json();
      } catch {
        renderSideResult(side, "error", "服务返回异常，请稍后重试。");
        return "error";
      }

      const currentRequestId = typeof data.request_id === "string" ? data.request_id : "";
      const detectedValue = typeof data.side === "string" ? data.side : "";
      const detectedLabel = sideLabel(detectedValue);
      if (!response.ok) {
        renderSideResult(
          side,
          "error",
          userMessageForErrorDetail(data.detail),
          detectedLabel,
          currentRequestId,
        );
        return "error";
      }

      if (detectedValue && detectedValue !== side.expectedSide) {
        renderSideResult(
          side,
          "review",
          `当前图片识别为${detectedLabel}，请放入${detectedLabel}上传框后重新提交。`,
          detectedLabel,
          currentRequestId,
        );
        return "review";
      }

      const state = responseState(data.review_result);
      renderSideResult(
        side,
        state,
        responseMessage(data, state),
        detectedLabel,
        currentRequestId,
      );
      return state;
    } catch {
      renderSideResult(side, "error", "暂时无法连接服务，请检查网络后重试。");
      return "error";
    }
  }

  function combinedState(states) {
    if (states.includes("error")) {
      return "error";
    }
    if (states.includes("reject")) {
      return "reject";
    }
    if (states.includes("review")) {
      return "review";
    }
    return "pass";
  }

  async function submitReview() {
    if (!front.selectedFile || !back.selectedFile || submitting) {
      return;
    }

    submitting = true;
    submitButton.textContent = "双面审核中...";
    updateSubmitButton();
    renderOverallStatus("loading");

    try {
      const states = await Promise.all([submitSide(front), submitSide(back)]);
      const state = combinedState(states);
      const labels = {
        pass: "双面已通过",
        review: "需要核验",
        reject: "审核未通过",
        error: "处理失败",
      };
      renderOverallStatus(state, labels[state]);
    } finally {
      submitting = false;
      submitButton.textContent = "提交双面审核";
      updateSubmitButton();
    }
  }

  function attachSideInteractions(side) {
    side.fileInput.addEventListener("change", () => {
      selectFile(side, side.fileInput.files?.[0] || null);
    });
    side.dropZone.addEventListener("dragover", (event) => {
      event.preventDefault();
      side.dropZone.classList.add("dragging");
    });
    side.dropZone.addEventListener("dragleave", () => {
      side.dropZone.classList.remove("dragging");
    });
    side.dropZone.addEventListener("drop", (event) => {
      event.preventDefault();
      side.dropZone.classList.remove("dragging");
      selectFile(side, event.dataTransfer?.files?.[0] || null);
    });
    side.removeButton.addEventListener("click", () => clearSelectedFile(side));
  }

  for (const side of sides) {
    attachSideInteractions(side);
  }
  submitButton.addEventListener("click", submitReview);
  window.addEventListener("beforeunload", () => {
    for (const side of sides) {
      revokePreviewUrl(side);
    }
  });
  clearAllResults();
  updateSubmitButton();
});
