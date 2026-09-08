/* The browse interface. Reads bundle/index.json once, then filters, searches,
   sorts, and lays the covers out in a 3D row in the browser. Nothing here
   talks to Discogs. */
(function () {
  "use strict";

  var state = {
    index: null,
    facet: null,
    style: null,
    query: "",
    sort: "shelf",
    list: [],
    active: 0,
  };

  var el = {
    count: document.getElementById("count"),
    search: document.getElementById("search"),
    sort: document.getElementById("sort"),
    facets: document.getElementById("facets"),
    styles: document.getElementById("styles"),
    active: document.getElementById("active"),
    activeText: document.getElementById("active-text"),
    clear: document.getElementById("clear"),
    carousel: document.getElementById("carousel"),
    now: document.getElementById("now"),
    nowLabel: document.getElementById("now-label"),
    nowTitle: document.getElementById("now-title"),
    nowMeta: document.getElementById("now-meta"),
    nowOpen: document.getElementById("now-open"),
    prev: document.getElementById("prev"),
    next: document.getElementById("next"),
    position: document.getElementById("position"),
    empty: document.getElementById("empty"),
    detail: document.getElementById("detail"),
    detailBody: document.getElementById("detail-body"),
    detailClose: document.getElementById("detail-close"),
  };

  var VISIBLE = 7; // covers laid out on each side of the active one

  /* ---------- helpers ---------- */

  function normalize(text) {
    return String(text || "")
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/&/g, " and ")
      .replace(/[^a-z0-9]+/g, " ")
      .trim()
      .replace(/^the /, "");
  }

  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "text") node.textContent = attrs[key];
        else if (attrs[key] != null) node.setAttribute(key, attrs[key]);
      });
    }
    (children || []).forEach(function (child) {
      if (child) node.appendChild(child);
    });
    return node;
  }

  function coverNode(record, className, big) {
    var src = big ? record.cover || record.thumb : record.thumb || record.cover;
    if (!src) return h("div", { class: className + " missing", text: record.title });
    return h("img", { class: className, src: src, alt: "", decoding: "async" });
  }

  function sectionIndex(record) {
    var order = state.index.section_order || [];
    var i = order.indexOf(record.section);
    return i < 0 ? order.length : i;
  }

  function compare(a, b) {
    for (var i = 0; i < a.length; i++) {
      if (a[i] < b[i]) return -1;
      if (a[i] > b[i]) return 1;
    }
    return 0;
  }

  function sortKey(record) {
    var artist = normalize(record.artist_sort || record.artist);
    var title = normalize(record.title);
    var year = record.year || 9999;
    switch (state.sort) {
      case "facet":
        return [record.facet || "￿", artist, year, title];
      case "year":
        return [year, artist, title];
      case "added":
        return [artist, title];
      default:
        return [sectionIndex(record), artist, year, title];
    }
  }

  function labelFor(record) {
    switch (state.sort) {
      case "facet":
        return record.facet || "Other";
      case "year":
        return record.decade ? record.decade + "s" : "Year unknown";
      case "added":
        return record.added_at ? "Added " + record.added_at.slice(0, 10) : "";
      default:
        return "Section " + record.section;
    }
  }

  /* ---------- filtering ---------- */

  function visibleRecords() {
    var records = state.index.records.slice();
    var query = normalize(state.query);
    records = records.filter(function (record) {
      if (state.facet && (record.facets || []).indexOf(state.facet) < 0) return false;
      if (state.style && (record.styles || []).indexOf(state.style) < 0) return false;
      if (query) {
        var hay = [record.artist, record.title, record.label].map(normalize).join(" | ");
        if (hay.indexOf(query) < 0) return false;
      }
      return true;
    });
    if (state.sort === "added") {
      records.sort(function (a, b) {
        var x = a.added_at || "";
        var y = b.added_at || "";
        if (x !== y) return x < y ? 1 : -1;
        return compare(sortKey(a), sortKey(b));
      });
    } else {
      records.sort(function (a, b) {
        return compare(sortKey(a), sortKey(b));
      });
    }
    return records;
  }

  /* ---------- chips ---------- */

  function chip(label, count, pressed, onClick) {
    var node = h("button", { type: "button", class: "chip", "aria-pressed": pressed ? "true" : "false" }, [
      document.createTextNode(label),
      h("small", { text: String(count) }),
    ]);
    node.addEventListener("click", onClick);
    return node;
  }

  function renderFacets() {
    el.facets.textContent = "";
    el.facets.appendChild(chip("All", state.index.count, state.facet === null, function () {
      state.facet = null;
      state.style = null;
      render();
    }));
    state.index.facets.forEach(function (facet) {
      el.facets.appendChild(chip(facet.name, facet.count, state.facet === facet.name, function () {
        state.facet = state.facet === facet.name ? null : facet.name;
        state.style = null;
        render();
      }));
    });

    el.styles.textContent = "";
    var current = state.index.facets.filter(function (f) { return f.name === state.facet; })[0];
    if (!current || !current.styles.length) {
      el.styles.hidden = true;
      return;
    }
    el.styles.hidden = false;
    el.styles.appendChild(chip("All " + current.name, current.count, state.style === null, function () {
      state.style = null;
      render();
    }));
    current.styles.forEach(function (style) {
      el.styles.appendChild(chip(style.name, style.count, state.style === style.name, function () {
        state.style = state.style === style.name ? null : style.name;
        render();
      }));
    });
  }

  function renderActive(shown) {
    var parts = [];
    if (state.facet) parts.push(state.facet);
    if (state.style) parts.push(state.style);
    if (state.query) parts.push("“" + state.query + "”");
    var filtered = parts.length > 0;
    el.active.hidden = !filtered;
    el.activeText.textContent = filtered ? parts.join(" · ") : "";
    el.count.textContent = filtered
      ? shown + " of " + state.index.count + " records"
      : state.index.count + " record" + (state.index.count === 1 ? "" : "s");
  }

  /* ---------- the coverflow ---------- */

  function buildCovers() {
    el.carousel.textContent = "";
    state.list.forEach(function (record, i) {
      var node = h("div", { class: "cover", "data-index": String(i), role: "button", tabindex: "-1", "aria-label": record.title }, [
        h("div", { class: "disc", "aria-hidden": "true" }),
        h("div", { class: "sleeve" }, [coverNode(record, "art", false)]),
        record.thumb || record.cover ? h("img", { class: "reflection", src: record.thumb || record.cover, alt: "", "aria-hidden": "true" }) : null,
      ]);
      node.addEventListener("click", function () {
        if (dragMoved) return;
        if (i === state.active) openDetail(record);
        else setActive(i);
      });
      el.carousel.appendChild(node);
    });
  }

  function layout() {
    var covers = el.carousel.children;
    if (!covers.length) return;
    var coverSize = covers[0].offsetWidth || 300;
    var stage = el.carousel.clientWidth || window.innerWidth;
    var spacing = Math.max(48, Math.min(coverSize * 0.42, stage * 0.14));
    var lift = coverSize * 0.62;
    for (var i = 0; i < covers.length; i++) {
      var node = covers[i];
      var offset = i - state.active;
      var abs = Math.abs(offset);
      var sign = offset < 0 ? -1 : 1;
      var hidden = abs > VISIBLE;
      node.classList.toggle("is-hidden", hidden);
      node.classList.toggle("is-active", offset === 0);
      node.setAttribute("aria-hidden", offset === 0 ? "false" : "true");
      if (hidden) {
        node.style.transform = "translateX(" + sign * (lift + VISIBLE * spacing + 200) + "px) translateZ(-900px) rotateY(" + -sign * 60 + "deg)";
        continue;
      }
      if (offset === 0) {
        node.style.transform = "translateX(0) translateZ(90px) rotateY(0deg)";
      } else {
        var x = sign * (lift + (abs - 1) * spacing);
        var z = -140 - abs * 60;
        node.style.transform = "translateX(" + x + "px) translateZ(" + z + "px) rotateY(" + -sign * 52 + "deg)";
      }
      node.style.zIndex = String(200 - abs);
      var img = node.querySelector(".sleeve img");
      var record = state.list[i];
      if (img && abs <= 2 && record.cover && img.getAttribute("src") !== record.cover) {
        img.setAttribute("src", record.cover);
      }
    }
  }

  function renderNow() {
    var record = state.list[state.active];
    el.now.classList.remove("rise");
    void el.now.offsetWidth; // restart the animation
    el.now.classList.add("rise");
    if (!record) {
      el.nowLabel.textContent = "";
      el.nowTitle.textContent = "";
      el.nowMeta.textContent = "";
      el.nowOpen.hidden = true;
      el.position.textContent = "";
      el.prev.disabled = el.next.disabled = true;
      return;
    }
    el.nowLabel.textContent = labelFor(record);
    el.nowTitle.textContent = record.title;
    el.nowMeta.textContent = [record.artist, record.year].filter(Boolean).join(" · ");
    el.nowOpen.hidden = false;
    el.position.textContent = (state.active + 1) + " / " + state.list.length;
    el.prev.disabled = state.active === 0;
    el.next.disabled = state.active >= state.list.length - 1;
  }

  function setActive(i, quiet) {
    if (!state.list.length) return;
    var next = Math.max(0, Math.min(state.list.length - 1, i));
    if (next === state.active && !quiet) return;
    state.active = next;
    layout();
    renderNow();
  }

  function renderCarousel() {
    var keep = state.list[state.active] ? state.list[state.active].id : null;
    state.list = visibleRecords();
    var idx = -1;
    if (keep !== null) {
      for (var i = 0; i < state.list.length; i++) if (state.list[i].id === keep) { idx = i; break; }
    }
    state.active = idx >= 0 ? idx : 0;
    buildCovers();
    if (!state.list.length) {
      el.empty.hidden = false;
      el.empty.textContent = state.index.count ? "Nothing in that crate." : "No records yet. Run the sync.";
      renderNow();
      return;
    }
    el.empty.hidden = true;
    layout();
    renderNow();
  }

  function render() {
    if (!state.index) return;
    renderFacets();
    renderCarousel();
    renderActive(state.list.length);
  }

  /* ---------- the gatefold ---------- */

  function fact(label, value) {
    if (value == null || value === "" || (Array.isArray(value) && !value.length)) return [];
    return [h("dt", { text: label }), h("dd", { text: Array.isArray(value) ? value.join(", ") : String(value) })];
  }

  function priceNode(price) {
    if (!price || price.lowest == null) return null;
    var amount = price.currency ? price.lowest.toFixed(2) + " " + price.currency : String(price.lowest);
    var listed = price.for_sale ? ", " + price.for_sale + " for sale" : "";
    var when = price.checked_at ? "Lowest listed copy" + listed + ", checked " + price.checked_at.slice(0, 10) : "Lowest listed copy";
    return h("p", { class: "price" }, [document.createTextNode(amount), h("small", { text: when })]);
  }

  function renderDetail(record) {
    var facts = [];
    facts = facts.concat(fact("Label", [record.label, record.catalog_no].filter(Boolean).join(" · ")));
    facts = facts.concat(fact("Format", record.format));
    facts = facts.concat(fact("Country", record.country));
    facts = facts.concat(fact("Released", record.year));
    facts = facts.concat(fact("Added", record.added_at ? record.added_at.slice(0, 10) : null));

    var tags = [].concat(record.genres || [], record.styles || []);
    var tracks = (record.tracks || []).map(function (track) {
      var heading = !track.position;
      return h("li", { class: heading ? "heading" : null }, [
        heading ? null : h("span", { class: "pos", text: track.position }),
        h("span", { class: "name", text: track.title }),
        track.duration ? h("span", { class: "dur", text: track.duration }) : null,
      ]);
    });

    el.detailBody.textContent = "";
    el.detailBody.appendChild(h("div", { class: "cover-wrap" }, [coverNode(record, "big", true)]));
    el.detailBody.appendChild(h("div", { class: "info" }, [
      h("h2", { id: "detail-title", text: record.title }),
      h("p", { class: "artist", text: record.artist }),
      h("div", { class: "section", text: "Section " + record.section }),
      facts.length ? h("dl", { class: "facts" }, facts) : null,
      priceNode(record.price),
      tags.length ? h("div", { class: "tags" }, tags.map(function (t) { return h("span", { class: "tag", text: t }); })) : null,
      tracks.length ? h("p", { class: "tracks-title", text: "Side notes" }) : null,
      tracks.length ? h("ul", { class: "tracks" }, tracks) : null,
    ]));
  }

  function openDetail(record) {
    renderDetail(record);
    if (typeof el.detail.showModal === "function") el.detail.showModal();
    else el.detail.setAttribute("open", "");
    fetch("bundle/detail/" + record.id + ".json", { cache: "no-cache" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (detail) { if (detail && el.detail.open) renderDetail(detail); })
      .catch(function () { /* the index entry is already shown */ });
  }

  function closeDetail() {
    if (el.detail.open) el.detail.close();
  }

  /* ---------- wiring ---------- */

  el.search.addEventListener("input", function () {
    state.query = el.search.value;
    render();
  });
  el.sort.addEventListener("change", function () {
    state.sort = el.sort.value;
    render();
  });
  el.clear.addEventListener("click", function () {
    state.facet = null;
    state.style = null;
    state.query = "";
    el.search.value = "";
    render();
  });
  el.prev.addEventListener("click", function () { setActive(state.active - 1); });
  el.next.addEventListener("click", function () { setActive(state.active + 1); });
  el.nowOpen.addEventListener("click", function () {
    var record = state.list[state.active];
    if (record) openDetail(record);
  });
  el.detailClose.addEventListener("click", closeDetail);
  el.detail.addEventListener("click", function (event) {
    if (event.target === el.detail) closeDetail();
  });

  document.addEventListener("keydown", function (event) {
    if (el.detail.open || event.target === el.search || event.target === el.sort) return;
    if (event.key === "ArrowRight") { setActive(state.active + 1); event.preventDefault(); }
    if (event.key === "ArrowLeft") { setActive(state.active - 1); event.preventDefault(); }
    if (event.key === "Home") { setActive(0); event.preventDefault(); }
    if (event.key === "End") { setActive(state.list.length - 1); event.preventDefault(); }
    if (event.key === "Enter" && document.activeElement === el.carousel) {
      var record = state.list[state.active];
      if (record) openDetail(record);
    }
  });

  // Drag or swipe to flip through the crate.
  var dragStartX = null;
  var dragStartActive = 0;
  var dragMoved = false;
  el.carousel.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) return;
    dragStartX = event.clientX;
    dragStartActive = state.active;
    dragMoved = false;
  });
  el.carousel.addEventListener("pointermove", function (event) {
    if (dragStartX === null) return;
    var dx = event.clientX - dragStartX;
    if (!dragMoved && Math.abs(dx) > 8) {
      dragMoved = true;
      el.carousel.classList.add("dragging");
      try { el.carousel.setPointerCapture(event.pointerId); } catch (e) { /* not needed */ }
    }
    if (!dragMoved) return;
    var step = Math.max(60, (el.carousel.children[0] ? el.carousel.children[0].offsetWidth : 300) * 0.35);
    setActive(dragStartActive - Math.round(dx / step), true);
  });
  function endDrag() {
    if (dragStartX === null) return;
    dragStartX = null;
    el.carousel.classList.remove("dragging");
    layout();
    setTimeout(function () { dragMoved = false; }, 0);
  }
  el.carousel.addEventListener("pointerup", endDrag);
  el.carousel.addEventListener("pointercancel", endDrag);
  el.carousel.addEventListener("pointerleave", endDrag);

  var wheelBusy = false;
  el.carousel.addEventListener("wheel", function (event) {
    var delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : (event.shiftKey ? event.deltaY : 0);
    if (!delta || wheelBusy) return;
    event.preventDefault();
    wheelBusy = true;
    setActive(state.active + (delta > 0 ? 1 : -1));
    setTimeout(function () { wheelBusy = false; }, 260);
  }, { passive: false });

  window.addEventListener("resize", layout);

  fetch("bundle/index.json", { cache: "no-cache" })
    .then(function (response) {
      if (!response.ok) throw new Error("no bundle");
      return response.json();
    })
    .then(function (index) {
      state.index = index;
      render();
    })
    .catch(function () {
      el.count.textContent = "";
      el.empty.hidden = false;
      el.empty.textContent = "No records yet. Run the sync, then reload.";
      renderNow();
    });
})();
