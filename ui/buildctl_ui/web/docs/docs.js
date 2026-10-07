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
    links.forEach((link, i) => link.setAttribute("aria-current", i === index ? "true" : "false"));
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
