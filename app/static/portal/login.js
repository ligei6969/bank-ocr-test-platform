"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("#loginForm");
  const submitButton = document.querySelector("#loginSubmit");
  const errorMessage = document.querySelector("#loginError");
  const query = new URLSearchParams(window.location.search);
  let submitting = false;

  if (query.get("error") === "1") {
    errorMessage.textContent = "用户名或密码错误。";
    errorMessage.hidden = false;
  }

  form.addEventListener("submit", (event) => {
    if (submitting) {
      event.preventDefault();
      return;
    }

    submitting = true;
    submitButton.disabled = true;
    submitButton.textContent = "登录中...";
  });
});
