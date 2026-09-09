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
    /* Travelling fast: the full-size art upgrade and the caption animation
       are both waste at twenty records a second. */
    fast: false,
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
    crate: document.getElementById("crate"),
    carousel: document.getElementById("carousel"),
    marker: document.getElementById("marker"),
    markerText: document.getElementById("marker-text"),
    scrubLeft: document.getElementById("scrub-left"),
    scrubRight: document.getElementById("scrub-right"),
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

  var VISIBLE = 7;   // covers laid out on each side of the active one
  var REST_MS = 850; // how long after the last step the dividers stay up

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

  /* The shelf divider a record starts, or null. Only shelf order has
     sections; the other orders run on year or genre and a letter tab there
     would say nothing true. A section is one letter, "#", or a configured
     name such as "Compilations & Soundtracks". */
  function dividerAt(i) {
    if (state.sort !== "shelf") return null;
    var record = state.list[i];
    if (!record || !record.section) return null;
    var previous = state.list[i - 1];
    if (previous && previous.section === record.section) return null;
    return record.section;
  }

  function dividerNode(section) {
    var classes = "divider" + (section.length > 2 ? " is-word" : "");
    return h("div", { class: classes, "aria-hidden": "true" }, [h("b", { text: section })]);
  }

  function buildCovers() {
    el.carousel.textContent = "";
    state.list.forEach(function (record, i) {
      var section = dividerAt(i);
      var node = h("div", { class: "cover", "data-index": String(i), role: "button", tabindex: "-1", "aria-label": record.title }, [
        section ? dividerNode(section) : null,
        h("div", { class: "disc", "aria-hidden": "true" }),
        h("div", { class: "sleeve" }, [coverNode(record, "art", false)]),
        record.thumb || record.cover ? h("img", { class: "reflection", src: record.thumb || record.cover, alt: "", "aria-hidden": "true" }) : null,
      ]);
      node.addEventListener("click", function () {
        if (drag.moved) return;
        if (i === state.active) openDetail(record);
        else setActive(i);
      });
      el.carousel.appendChild(node);
    });
  }

  function layout() {
    var covers = el.carousel.children;
    if (!covers.length) return;
    var count = state.list.length;
    var coverSize = covers[0].offsetWidth || 300;
    var stage = el.carousel.clientWidth || window.innerWidth;
    var spacing = Math.max(48, Math.min(coverSize * 0.42, stage * 0.14));
    var lift = coverSize * 0.62;
    el.crate.style.setProperty("--scrub-zone", scrubZone().toFixed(1) + "px");
    for (var i = 0; i < covers.length; i++) {
      var node = covers[i];
      /* The shortest way round the ring from the active cover to this one,
         which may run forwards past the end of the list. That is what puts
         A one step past Z instead of a wall. */
      var offset = i - state.active;
      if (count > 1) {
        offset = ((offset % count) + count) % count;
        if (offset > count / 2) offset -= count;
      }
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
      if (!state.fast && img && abs <= 2 && record.cover && img.getAttribute("src") !== record.cover) {
        img.setAttribute("src", record.cover);
      }
    }
  }

  /* The section letter behind the covers. Shelf order only, for the same
     reason the divider tabs are. */
  function renderMarker(record) {
    var section = record && state.sort === "shelf" ? record.section : null;
    el.marker.hidden = !section;
    if (!section) return;
    el.marker.classList.toggle("is-word", section.length > 2);
    el.markerText.textContent = section;
  }

  var restTimer = null;

  /* "Moving" is what raises the dividers and the letter. It lasts a beat
     past the last step so a slow flip through still shows them. */
  function markMoving() {
    document.body.classList.add("is-moving");
    clearTimeout(restTimer);
    restTimer = setTimeout(function () {
      document.body.classList.remove("is-moving");
    }, REST_MS);
  }

  function renderNow() {
    var record = state.list[state.active];
    if (!state.fast) {
      el.now.classList.remove("rise");
      void el.now.offsetWidth; // restart the animation
      el.now.classList.add("rise");
    }
    renderMarker(record);
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
    // The ring has no ends, so neither arrow is ever the last one.
    el.prev.disabled = el.next.disabled = state.list.length < 2;
  }

  /* Any index, wrapped into the list. Z runs into A and A back into Z. */
  function wrapIndex(i) {
    var count = state.list.length;
    if (count < 2) return 0;
    return ((i % count) + count) % count;
  }

  function setActive(i, quiet) {
    if (!state.list.length) return;
    var next = wrapIndex(i);
    if (next === state.active && !quiet) return;
    state.active = next;
    markMoving();
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

  /* ---------- one finger, two jobs ----------
     Moving it drags the row a cover at a time. Parking it in either edge
     gutter travels the crate on its own, faster the nearer the edge, which
     is the only way two hundred records is a short trip. Both feed the same
     target, so dragging out to the edge and holding there keeps going. */

  var HOLD_MS = 190;    // a tap on an edge cover still just picks that cover
  var SCRUB_SLOW = 2.5; // records a second at the inner lip of the gutter
  var SCRUB_FAST = 26;  // records a second hard against the edge
  var DEAD = 0.06;      // the innermost sliver of the gutter does nothing

  var drag = {
    id: null,      // the pointer being followed, or null between gestures
    startX: 0,
    x: 0,
    dx: 0,
    from: 0,       // the active index when the finger went down
    downAt: 0,
    moved: false,  // suppresses the click that would open the gatefold
    scrubbing: false,
    accum: 0,      // records travelled by the gutter, fractional
    raf: null,
    last: 0,
  };

  /* Wide enough to hit with a thumb, never so wide it swallows the middle. */
  function scrubZone() {
    var width = el.carousel.clientWidth || 0;
    return Math.max(52, Math.min(width * 0.2, 148));
  }

  /* How hard a gutter is being pushed: -1 hard left, +1 hard right, 0 in the
     middle. Past the edge of the element counts as all the way. */
  function edgePush(clientX) {
    var rect = el.carousel.getBoundingClientRect();
    if (!rect.width) return 0;
    var zone = Math.min(scrubZone(), rect.width / 2.2);
    var x = clientX - rect.left;
    var push = 0;
    if (x < zone) push = -(1 - x / zone);
    else if (x > rect.width - zone) push = 1 - (rect.width - x) / zone;
    return Math.max(-1, Math.min(1, push));
  }

  function setHeat(push) {
    el.scrubLeft.style.setProperty("--heat", push < 0 ? (-push).toFixed(3) : "0");
    el.scrubRight.style.setProperty("--heat", push > 0 ? push.toFixed(3) : "0");
  }

  function beginFast() {
    if (state.fast) return;
    state.fast = true;
    el.carousel.classList.add("scrubbing");
  }

  function endFast() {
    if (!state.fast) return;
    state.fast = false;
    el.carousel.classList.remove("scrubbing");
    layout(); // the covers beside this one have earned their full-size art
  }

  function applyDrag() {
    var first = el.carousel.children[0];
    var step = Math.max(60, (first ? first.offsetWidth : 300) * 0.35);
    var target = drag.from - Math.round(drag.dx / step) + Math.round(drag.accum);
    var next = wrapIndex(target);
    if (next !== state.active) setActive(next);
  }

  function scrubTick(now) {
    if (drag.id === null) {
      drag.raf = null;
      setHeat(0);
      return;
    }
    drag.raf = requestAnimationFrame(scrubTick);
    var seconds = Math.min(0.05, (now - drag.last) / 1000);
    drag.last = now;
    var push = edgePush(drag.x);
    setHeat(push);
    var force = Math.abs(push);
    if (force < DEAD || now - drag.downAt < HOLD_MS) {
      if (drag.scrubbing) {
        drag.scrubbing = false;
        endFast();
      }
      return;
    }
    if (!drag.scrubbing) {
      drag.scrubbing = true;
      drag.moved = true;
      beginFast();
      // Now the gesture belongs to the crate even if the finger slides off it.
      try { el.carousel.setPointerCapture(drag.id); } catch (e) { /* not needed */ }
    }
    var eased = Math.pow((force - DEAD) / (1 - DEAD), 2.1);
    var rate = SCRUB_SLOW + (SCRUB_FAST - SCRUB_SLOW) * eased;
    drag.accum += rate * (push < 0 ? -1 : 1) * seconds;
    applyDrag();
  }

  el.carousel.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) return;
    drag.id = event.pointerId;
    drag.startX = drag.x = event.clientX;
    drag.dx = 0;
    drag.accum = 0;
    drag.from = state.active;
    drag.downAt = drag.last = performance.now();
    drag.moved = false;
    drag.scrubbing = false;
    if (drag.raf === null) drag.raf = requestAnimationFrame(scrubTick);
  });

  el.carousel.addEventListener("pointermove", function (event) {
    if (drag.id === null || event.pointerId !== drag.id) return;
    drag.x = event.clientX;
    drag.dx = event.clientX - drag.startX;
    if (!drag.moved && Math.abs(drag.dx) > 8) {
      drag.moved = true;
      el.carousel.classList.add("dragging");
      try { el.carousel.setPointerCapture(event.pointerId); } catch (e) { /* not needed */ }
    }
    if (drag.moved) applyDrag();
  });

  function endDrag(event) {
    if (drag.id === null) return;
    if (event && event.pointerId != null && event.pointerId !== drag.id) return;
    drag.id = null;
    drag.scrubbing = false;
    endFast();
    el.carousel.classList.remove("dragging");
    if (drag.raf !== null) {
      cancelAnimationFrame(drag.raf);
      drag.raf = null;
    }
    setHeat(0);
    layout();
    setTimeout(function () { drag.moved = false; }, 0);
  }
  el.carousel.addEventListener("pointerup", endDrag);
  el.carousel.addEventListener("pointercancel", endDrag);
  el.carousel.addEventListener("pointerleave", endDrag);
  // A long press in a gutter is a scrub, not a request for the system menu.
  el.carousel.addEventListener("contextmenu", function (event) { event.preventDefault(); });

  /* An arrow held down does what a finger in a gutter does, for anyone on a
     mouse: one step, then a run that speeds up. */
  function holdToRepeat(button, direction) {
    var raf = null;
    var downAt = 0;
    var last = 0;
    var accum = 0;
    var repeated = false;

    function step(now) {
      if (!downAt) {
        raf = null;
        return;
      }
      raf = requestAnimationFrame(step);
      var held = now - downAt;
      if (held < 380) { // a plain click is not a run
        last = now;
        return;
      }
      var eased = Math.min(1, (held - 380) / 1400);
      accum += (3 + 21 * eased * eased) * ((now - last) / 1000);
      last = now;
      var whole = Math.floor(accum);
      if (whole < 1) return;
      accum -= whole;
      repeated = true;
      beginFast();
      setActive(state.active + direction * whole);
    }

    function release() {
      if (!downAt) return;
      downAt = 0;
      if (raf !== null) {
        cancelAnimationFrame(raf);
        raf = null;
      }
      endFast();
    }

    button.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      downAt = last = performance.now();
      accum = 0;
      repeated = false;
      if (raf === null) raf = requestAnimationFrame(step);
    });
    button.addEventListener("pointerup", release);
    button.addEventListener("pointerleave", release);
    button.addEventListener("pointercancel", release);
    // The click that ends a run would add one step too many.
    button.addEventListener("click", function () {
      if (repeated) {
        repeated = false;
        return;
      }
      setActive(state.active + direction);
    });
  }
  holdToRepeat(el.prev, -1);
  holdToRepeat(el.next, 1);

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
      // Stand the dividers up once on arrival, so they are not a secret.
      markMoving();
    })
    .catch(function () {
      el.count.textContent = "";
      el.empty.hidden = false;
      el.empty.textContent = "No records yet. Run the sync, then reload.";
      renderNow();
    });
})();
