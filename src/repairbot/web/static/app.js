/*
 * Минимальная интерактивность панели.
 *
 * Нужны ровно два поведения — отправить форму и подставить пришедший
 * фрагмент, и догрузить следующую страницу ленты. Ради них тянуть
 * библиотеку незачем: страницы рендерит сервер, а это остаётся клеем.
 *
 * Разметка управляет поведением через data-атрибуты:
 *   data-replace="#selector"  — чем заменить, куда положить ответ
 *   data-append="#selector"   — добавить ответ в конец
 *   data-remove="#selector"   — убрать элемент после успеха
 */

(function () {
  "use strict";

  function target(el, attr) {
    var selector = el.getAttribute(attr);
    return selector ? document.querySelector(selector) : null;
  }

  async function send(form) {
    var button = form.querySelector("button[type=submit]");
    if (button) {
      if (button.disabled) return; // защита от двойного нажатия
      button.disabled = true;
    }

    try {
      var response = await fetch(form.action, {
        method: (form.method || "post").toUpperCase(),
        body: new FormData(form),
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
      });

      if (response.status === 401 || response.status === 403) {
        // Сессия истекла или прав не хватает — пусть сервер решит, куда вести.
        window.location.reload();
        return;
      }

      var html = await response.text();
      var slot = target(form, "data-replace");
      if (slot) slot.innerHTML = html;

      var doomed = target(form, "data-remove");
      if (doomed && response.ok) {
        doomed.classList.add("resolved");
      }
    } catch (error) {
      var slot2 = target(form, "data-replace");
      if (slot2) slot2.textContent = "Не удалось связаться с сервером";
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function loadMore(link) {
    link.disabled = true;
    link.textContent = "Загружаю…";
    try {
      var response = await fetch(link.dataset.url, {
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
      });
      var html = await response.text();
      var into = target(link, "data-append");
      if (!into) return;

      into.insertAdjacentHTML("beforeend", html);

      // Кнопка следующей страницы приезжает вместе с фрагментом; эту убираем.
      // В таблице кнопка лежит в своей строке — снимаем строку целиком,
      // иначе останется пустой <tr>.
      (link.closest("tr") || link).remove();
    } catch (error) {
      link.textContent = "Ошибка загрузки, нажмите ещё раз";
      link.disabled = false;
    }
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.matches("form[data-replace]")) {
      event.preventDefault();
      send(form);
    }
  });

  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-append][data-url]");
    if (link) {
      event.preventDefault();
      loadMore(link);
    }
  });

  // Фильтры ленты применяются сразу при выборе — без кнопки «Показать».
  document.addEventListener("change", function (event) {
    var control = event.target;
    if (control.matches("[data-autosubmit]") && control.form) {
      control.form.submit();
    }
  });
})();
