const fileInput = document.querySelector("#fileInput");
const dropZone = document.querySelector("#dropZone");
const submitButton = document.querySelector("#submitButton");
const resetButton = document.querySelector("#resetButton");
const imagePreview = document.querySelector("#imagePreview");
const emptyPreview = document.querySelector("#emptyPreview");
const fileMeta = document.querySelector("#fileMeta");
const message = document.querySelector("#message");
const reviewResult = document.querySelector("#reviewResult");
const qualityResult = document.querySelector("#qualityResult");
const latency = document.querySelector("#latency");
const cardNumber = document.querySelector("#cardNumber");
const cardholderName = document.querySelector("#cardholderName");
const validDate = document.querySelector("#validDate");
const brightness = document.querySelector("#brightness");
const blur = document.querySelector("#blur");
const glare = document.querySelector("#glare");
const ocrText = document.querySelector("#ocrText");
const rawJson = document.querySelector("#rawJson");

let selectedFile = null;

function formatValue(value) {
  if (value === null || value === undefined || value === "") {
    return "--";
  }
  return String(value);
}

function setStatus(element, value) {
  const text = formatValue(value);
  element.textContent = text;
  element.className = "status";
  const normalized = text.toLowerCase();
  if (["pass", "review", "reject", "error"].includes(normalized)) {
    element.classList.add(normalized);
  } else {
    element.classList.add("neutral");
  }
}

function setMessage(text, isError = false) {
  message.textContent = text;
  message.style.color = isError ? "var(--reject)" : "var(--muted)";
}

function renderEmpty() {
  setStatus(reviewResult, "未提交");
  setStatus(qualityResult, "--");
  latency.textContent = "--";
  cardNumber.textContent = "--";
  cardholderName.textContent = "--";
  validDate.textContent = "--";
  brightness.textContent = "--";
  blur.textContent = "--";
  glare.textContent = "--";
  ocrText.textContent = "--";
  rawJson.textContent = "{}";
}

function renderResult(data, elapsedMs) {
  setStatus(reviewResult, data.review_result);
  setStatus(qualityResult, data.quality?.quality_result);
  latency.textContent = `${elapsedMs} ms`;

  cardNumber.textContent = formatValue(data.fields?.card_number);
  cardholderName.textContent = formatValue(data.fields?.name);
  validDate.textContent = formatValue(data.fields?.valid_date);
  brightness.textContent = formatValue(data.quality?.brightness);
  blur.textContent = data.quality?.is_blur === true ? "是" : "否";
  glare.textContent = data.quality?.has_glare === true ? "是" : "否";
  ocrText.textContent = Array.isArray(data.ocr_text) && data.ocr_text.length ? data.ocr_text.join("\n") : "--";
  rawJson.textContent = JSON.stringify(data, null, 2);
}

function selectFile(file) {
  selectedFile = file;
  submitButton.disabled = !file;

  if (!file) {
    fileInput.value = "";
    fileMeta.textContent = "PNG / JPG";
    imagePreview.hidden = true;
    emptyPreview.hidden = false;
    setMessage("");
    return;
  }

  fileMeta.textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KB`;
  const objectUrl = URL.createObjectURL(file);
  imagePreview.onload = () => URL.revokeObjectURL(objectUrl);
  imagePreview.src = objectUrl;
  imagePreview.hidden = false;
  emptyPreview.hidden = true;
  setMessage("");
}

async function submitReview() {
  if (!selectedFile) {
    return;
  }

  submitButton.disabled = true;
  submitButton.textContent = "审核中";
  setMessage("正在处理图片");
  const startedAt = performance.now();
  const formData = new FormData();
  formData.append("file", selectedFile, selectedFile.name);

  try {
    const response = await fetch("/bank-card/review", {
      method: "POST",
      body: formData,
    });
    const elapsedMs = Math.round(performance.now() - startedAt);
    const data = await response.json();
    rawJson.textContent = JSON.stringify(data, null, 2);

    if (!response.ok) {
      setStatus(reviewResult, "error");
      setStatus(qualityResult, "--");
      latency.textContent = `${elapsedMs} ms`;
      setMessage(data.detail || "请求失败", true);
      return;
    }

    renderResult(data, elapsedMs);
    setMessage("完成");
  } catch (error) {
    setStatus(reviewResult, "error");
    setMessage(error instanceof Error ? error.message : "请求失败", true);
  } finally {
    submitButton.textContent = "开始审核";
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
  selectFile(event.dataTransfer.files?.[0] || null);
});

submitButton.addEventListener("click", submitReview);

resetButton.addEventListener("click", () => {
  selectFile(null);
  renderEmpty();
});

renderEmpty();
