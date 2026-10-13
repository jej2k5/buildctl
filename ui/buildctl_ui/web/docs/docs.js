"use strict";
// Marks the chapter you are reading in the bar: the last one whose top has passed 40% of the
// viewport. Keeps that chapter's link in view when the bar scrolls sideways on narrow screens.
(() => {
  const links = [...document.querySelectorAll("[data-chapter]")];
  const sections = links.map((link) => document.getElementById(link.dataset.chapter));
  let current = -1;
  function update() {
    const line = window.innerHeight * 0.4;
    let index = 0;
    sections.forEach((section, i) => { if (section && section.getBoundingClientRect().top <= line) index = i; });
    if (index === current) return;
    current = index;
    // "location": the chapter you are in, not a separate page
    links.forEach((link, i) => i === index ? link.setAttribute("aria-current", "location") : link.removeAttribute("aria-current"));
    const link = links[index], bar = link.closest(".chapters");
    if (link.offsetLeft < bar.scrollLeft || link.offsetLeft + link.offsetWidth > bar.scrollLeft + bar.clientWidth) {
      bar.scrollLeft = link.offsetLeft - 16;
    }
  }
  let queued = false;
  window.addEventListener("scroll", () => {
    if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; update(); }); }
  }, { passive: true });
  window.addEventListener("hashchange", update);
  update();
})();

// A copy button on every code block. It copies the block's text exactly, so a command never
// picks up a stray character from a hand selection. Without script the blocks read the same.
(() => {
  if (!navigator.clipboard) return;
  const status = document.createElement("span");  // the button's own label stays "Copy code"
  status.className = "vh"; status.setAttribute("aria-live", "polite");
  document.body.append(status);
  document.querySelectorAll("pre > code").forEach((code) => {
    const pre = code.parentElement, button = document.createElement("button");
    button.type = "button"; button.className = "copy"; button.textContent = "Copy";
    button.setAttribute("aria-label", "Copy " + (pre.dataset.lang ? pre.dataset.lang + " " : "") + "code");
    button.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(code.textContent); button.textContent = "Copied"; }
      catch { button.textContent = "Copy failed"; }
      status.textContent = button.textContent;
      setTimeout(() => { button.textContent = "Copy"; status.textContent = ""; }, 2000);
    });
    pre.classList.add("has-copy");
    pre.prepend(button);
  });
})();
