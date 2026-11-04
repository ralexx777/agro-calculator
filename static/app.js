(function () {
  "use strict";

  const form = document.getElementById("calc");
  if (!form) return;

  const byId = (id) => document.getElementById(id);
  const crops = window.CROPS || {};
  const select = byId("culture");
  const fields = ["area", "density", "m1000", "price", "germination", "purity"];

  Object.keys(crops).forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  });

  const num = (id) => {
    const value = parseFloat(String(byId(id).value).replace(",", "."));
    return Number.isFinite(value) ? value : 0;
  };

  const fmt = (value, digits) =>
    value.toLocaleString("ru-RU", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });

  function compute() {
    const area = Math.max(0, num("area"));
    const density = Math.max(0, num("density"));
    const m1000 = Math.max(0, num("m1000"));
    const price = Math.max(0, num("price"));
    const germination = Math.min(100, Math.max(0.1, num("germination")));
    const purity = Math.min(100, Math.max(0.1, num("purity")));

    const pg = (germination * purity) / 100;
    const rate = pg > 0 ? (density * m1000 * 100) / pg : 0;
    const total = rate * area;
    const cost = total * price;

    byId("r-rate").textContent = fmt(rate, 1);
    byId("r-pg").textContent = fmt(pg, 1) + " %";
    byId("r-seeds").textContent = fmt(density * 100, 0) + " шт/м²";
    byId("r-total").textContent = fmt(total, 0) + " кг";
    byId("r-tons").textContent = fmt(total / 1000, 2) + " т";
    byId("r-cost").textContent = fmt(cost, 0) + " ₽";
    byId("r-per-ha").textContent = area > 0 ? fmt(cost / area, 0) + " ₽/га" : "—";

    return {
      area: area,
      density: density,
      m1000: m1000,
      price: price,
      germination: germination,
      purity: purity,
    };
  }

  function applyCrop() {
    const crop = crops[select.value];
    if (!crop) return;
    byId("m1000").value = crop.m1000;
    byId("density").value = crop.density;
    compute();
  }

  select.addEventListener("change", applyCrop);
  fields.forEach((id) => byId(id).addEventListener("input", compute));
  form.addEventListener("submit", (event) => event.preventDefault());

  const saveButton = byId("save");
  const note = byId("save-note");

  saveButton.addEventListener("click", async () => {
    const data = compute();
    note.textContent = "Сохраняем…";
    saveButton.disabled = true;
    try {
      const response = await fetch("/api/save", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": window.CSRF,
        },
        body: JSON.stringify(Object.assign({ culture: select.value }, data)),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || "сервер отклонил расчёт");
      }
      note.textContent = "Сохранено — обновляем историю";
      setTimeout(() => location.reload(), 400);
    } catch (error) {
      note.textContent = "Не удалось сохранить: " + error.message;
      saveButton.disabled = false;
    }
  });

  applyCrop();
})();
