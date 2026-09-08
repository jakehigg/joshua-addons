/* The browse interface. Reads bundle/index.json once, then filters, searches,
   and sorts in the browser. Nothing here talks to Discogs. */
(function () {
  "use strict";

  var state = {
    index: null,
    facet: null,
    style: null,
    query: "",
    sort: "shelf",
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
    empty: document.getElementById("empty"),
    detail: document.getElementById("detail"),
    detailBody: document.getElementById("detail-body"),
    detailClose: document.getElementById("detail-close"),
  };

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
        else if (key === "html") node.innerHTML = attrs[key];
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
    if (!src) {
      return h("div", { class: className + " missing", text: "No cover" });
    }
    return h("img", {
      class: className,
      src: src,
      alt: "",
      loading: "lazy",
      decoding: "async",
    });
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
        return [record.added_at ? "" : "￿", "", artist, title];
      default:
        return [sectionIndex(record), artist, year, title];
    }
  }

  function dividerFor(record, previous) {
    var label = null;
    switch (state.sort) {
      case "facet":
        label = record.facet || "Other";
        break;
      case "year":
        label = record.decade ? record.decade + "s" : "Year unknown";
        break;
      case "added":
        return null;
      default:
        label = record.section;
    }
    if (previous === label) return null;
    return label;
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

  /* ---------- rendering ---------- */

  function renderFacets() {
    el.facets.textContent = "";
    var total = state.index.count;
    el.facets.appendChild(chip("All", total, state.facet === null, function () {
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

  function chip(label, count, pressed, onClick) {
    var node = h("button", { type: "button", class: "chip", "aria-pressed": pressed ? "true" : "false" }, [
      document.createTextNode(label),
      h("small", { text: String(count) }),
    ]);
    node.addEventListener("click", onClick);
    return node;
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

  function renderCarousel(records) {
    el.carousel.textContent = "";
    if (!records.length) {
      el.empty.hidden = false;
      el.empty.textContent = state.index.count ? "Nothing matches." : "No records yet. Run the sync.";
      return;
    }
    el.empty.hidden = true;
    var previous = null;
    records.forEach(function (record) {
      var label = dividerFor(record, previous);
      if (label !== null) {
        el.carousel.appendChild(h("div", { class: "divider", "aria-hidden": "true" }, [h("span", { text: label })]));
        previous = label;
      }
      var meta = [record.artist, record.year].filter(Boolean).join(" · ");
      var card = h("button", { type: "button", class: "card", "data-id": record.id }, [
        coverNode(record, "cover", false),
        h("p", { class: "title", text: record.title }),
        h("p", { class: "meta", text: meta }),
      ]);
      card.addEventListener("click", function () { openDetail(record); });
      el.carousel.appendChild(card);
    });
  }

  function render() {
    if (!state.index) return;
    var records = visibleRecords();
    renderFacets();
    renderActive(records.length);
    renderCarousel(records);
  }

  /* ---------- detail ---------- */

  function fact(label, value) {
    if (value == null || value === "" || (Array.isArray(value) && !value.length)) return [];
    return [h("dt", { text: label }), h("dd", { text: Array.isArray(value) ? value.join(", ") : String(value) })];
  }

  function priceNode(price) {
    if (!price || price.lowest == null) return null;
    var amount = price.currency ? price.lowest + " " + price.currency : String(price.lowest);
    var when = price.checked_at ? "Lowest listed copy, checked " + price.checked_at.slice(0, 10) : "Lowest listed copy";
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
      return h("li", null, [
        h("span", { class: "pos", text: track.position || "" }),
        h("span", { class: "name", text: track.title }),
        track.duration ? h("span", { class: "dur", text: track.duration }) : null,
      ]);
    });

    el.detailBody.textContent = "";
    el.detailBody.appendChild(coverNode(record, "cover", true));
    el.detailBody.appendChild(h("div", { class: "info" }, [
      h("h2", { id: "detail-title", text: record.title }),
      h("p", { class: "artist", text: record.artist }),
      h("div", { class: "section", text: "Section " + record.section }),
      facts.length ? h("dl", { class: "facts" }, facts) : null,
      priceNode(record.price),
      tags.length ? h("div", { class: "tags" }, tags.map(function (t) { return h("span", { class: "tag", text: t }); })) : null,
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
  el.detailClose.addEventListener("click", closeDetail);
  el.detail.addEventListener("click", function (event) {
    if (event.target === el.detail) closeDetail();
  });
  el.carousel.addEventListener("keydown", function (event) {
    var step = el.carousel.clientWidth * 0.6;
    if (event.key === "ArrowRight") el.carousel.scrollBy({ left: step, behavior: "smooth" });
    if (event.key === "ArrowLeft") el.carousel.scrollBy({ left: -step, behavior: "smooth" });
  });

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
    });
})();
