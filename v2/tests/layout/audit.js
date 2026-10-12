// The layout audit, run in the page by test_layout.py: page.evaluate(<this file>, {desktop: true|false}).
//
// It looks at what is actually drawn (text line boxes and controls, after every ancestor that clips them)
// and returns a list of problems:
//
//   page-scroll  the page scrolls sideways (with the elements that stick out past the right edge)
//   cut-off      text or a control cut by an ancestor with overflow hidden or clip (not an ellipsis)
//   overlap      two pieces of text or controls drawn on top of each other
//   off-screen   a control partly or wholly past the edge of the window, with no way to scroll to it
//   wide-table   (laptop widths only) a table or box that scrolls sideways inside the page
//
// Offending elements get data-layout-problem="<kind>" so the screenshot can outline them. Each problem is
// {kind, text, where, top}; similar ones (every row of one column) are merged with a count.
(options) => {
  const vw = document.documentElement.clientWidth;
  const SLACK = 2; // pixels: sub-pixel rounding and anti-aliasing
  const styles = new Map();
  const style = (el) => {
    let s = styles.get(el);
    if (!s) {
      s = getComputedStyle(el);
      styles.set(el, s);
    }
    return s;
  };
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "OPTION", "OPTGROUP", "HEAD", "TITLE"]);
  const CONTROL = "a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab]";

  // -- Where something is, in words a person can find on the page ----------------------------------------
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();
  const short = (t, n = 50) => (t.length > n ? t.slice(0, n - 1) + "…" : t);
  function label(el) {
    const own =
      el.getAttribute("aria-label") ||
      el.getAttribute("title") ||
      (el.tagName === "INPUT" || el.tagName === "TEXTAREA" ? el.getAttribute("placeholder") || el.value : "") ||
      (el.tagName === "SELECT" && el.selectedOptions[0] ? el.selectedOptions[0].textContent : "") ||
      el.textContent;
    return short(clean(own));
  }
  function column(el) {
    const cell = el.closest("td, th");
    if (!cell) return "";
    const table = cell.closest("table");
    const head = table && table.tHead && table.tHead.rows[0];
    if (!head) return cell.dataset.label || "";
    let index = 0;
    for (const c of cell.parentElement.cells) {
      if (c === cell) break;
      index += c.colSpan || 1;
    }
    let at = 0;
    for (const h of head.cells) {
      if (at <= index && index < at + (h.colSpan || 1)) return clean(h.textContent) || "(no heading)";
      at += h.colSpan || 1;
    }
    return cell.dataset.label || "";
  }
  function selector(el) {
    if (!el || el === document.documentElement) return "html";
    let s = el.tagName.toLowerCase();
    if (el.id) return s + "#" + el.id;
    const classes = [...el.classList].filter((c) => !/^(is-|js-)/.test(c)).slice(0, 2);
    if (classes.length) s += "." + classes.join(".");
    return s;
  }
  function where(el) {
    const parts = [];
    const heading = el.closest("section, .card, details, form, main")?.querySelector("h1, h2, h3, summary");
    const col = column(el);
    if (col) parts.push(`column "${short(col, 30)}"`);
    if (heading && !heading.contains(el)) parts.push(`under "${short(clean(heading.textContent), 40)}"`);
    parts.push(selector(el));
    return parts.join(", ");
  }

  // -- What is drawn -------------------------------------------------------------------------------------
  function invisible(el) {
    if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return true;
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      // Inside a closed <details> (a menu, a folded part), apart from its summary: not drawn.
      const p = e.parentElement;
      if (p && p.tagName === "DETAILS" && !p.open && e !== p.querySelector(":scope > summary")) return true;
      const s = style(e);
      if (s.display === "none" || s.visibility === "hidden" || s.visibility === "collapse") return true;
      if (Number(s.opacity) === 0 || e.hasAttribute("inert")) return true;
      if (s.contentVisibility === "hidden") return true;
    }
    return false;
  }
  // Screen-reader text (.sr-only and the like): one pixel, clipped on purpose.
  function srOnly(el) {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const s = style(e);
      if (s.position === "absolute" && (s.clip !== "auto" || s.clipPath !== "none")) return true;
      const r = e.getBoundingClientRect();
      if (s.overflow !== "visible" && r.width <= 1 && r.height <= 1) return true;
    }
    return false;
  }
  const INF = { x0: -Infinity, x1: Infinity, y0: -Infinity, y1: Infinity };
  const meet = (a, b) => ({
    x0: Math.max(a.x0, b.x0),
    x1: Math.min(a.x1, b.x1),
    y0: Math.max(a.y0, b.y0),
    y1: Math.min(a.y1, b.y1),
  });
  function paddingBox(el) {
    const r = el.getBoundingClientRect();
    const x0 = r.left + el.clientLeft;
    const y0 = r.top + el.clientTop;
    return { x0, x1: x0 + el.clientWidth, y0, y1: y0 + el.clientHeight };
  }
  // The element a box is clipped by next: its parent, or for an absolutely positioned box, the nearest
  // positioned ancestor (overflow on the ancestors in between doesn't clip it); fixed boxes only by the window.
  function clipParent(el) {
    const pos = style(el).position;
    if (pos === "fixed") return null;
    let p = el.parentElement;
    if (pos === "absolute") {
      while (p && p !== document.body && style(p).position === "static" && style(p).transform === "none") {
        p = p.parentElement;
      }
    }
    return p;
  }
  // For the content of `el`: `view` is the area left visible by every clipping ancestor (scroll boxes too),
  // `cut` the area outside which content is cut off for good (overflow hidden or clip, up to the nearest box
  // that scrolls in that direction), `by` the element doing the cutting, `scrollX` whether some ancestor
  // scrolls sideways (so what is past the window can be reached).
  const regions = new Map();
  function region(el) {
    if (!el) return { view: INF, cut: INF, byX: null, byY: null, scrollX: false };
    let r = regions.get(el);
    if (r) return r;
    const base = region(clipParent(el));
    const s = style(el);
    r = { ...base };
    if (el !== document.documentElement && el !== document.body) {
      const box = paddingBox(el);
      const scrolls = (o) => o === "auto" || o === "scroll" || o === "overlay";
      const cuts = (o) => o === "hidden" || o === "clip";
      if (scrolls(s.overflowX)) {
        r.view = { ...r.view, x0: Math.max(r.view.x0, box.x0), x1: Math.min(r.view.x1, box.x1) };
        r.cut = { ...r.cut, x0: -Infinity, x1: Infinity };
        r.byX = null;
        r.scrollX = true;
      } else if (cuts(s.overflowX)) {
        r.view = { ...r.view, x0: Math.max(r.view.x0, box.x0), x1: Math.min(r.view.x1, box.x1) };
        r.cut = { ...r.cut, x0: Math.max(r.cut.x0, box.x0), x1: Math.min(r.cut.x1, box.x1) };
        r.byX = el;
      }
      if (scrolls(s.overflowY)) {
        r.view = { ...r.view, y0: Math.max(r.view.y0, box.y0), y1: Math.min(r.view.y1, box.y1) };
        r.cut = { ...r.cut, y0: -Infinity, y1: Infinity };
        r.byY = null;
      } else if (cuts(s.overflowY)) {
        r.view = { ...r.view, y0: Math.max(r.view.y0, box.y0), y1: Math.min(r.view.y1, box.y1) };
        r.cut = { ...r.cut, y0: Math.max(r.cut.y0, box.y0), y1: Math.min(r.cut.y1, box.y1) };
        r.byY = el;
      }
    }
    regions.set(el, r);
    return r;
  }
  // Whether text in `el` that runs past `cutter` ends in an ellipsis (truncated on purpose).
  function ellipsis(el, cutter) {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      if (style(e).textOverflow === "ellipsis") return true;
      if (e === cutter) break;
    }
    return false;
  }

  // Pieces of what is drawn: each line of text, and each control, image or icon as one box.
  const atoms = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
    acceptNode(node) {
      if (node.nodeType === 1) {
        if (SKIP_TAGS.has(node.tagName)) return NodeFilter.FILTER_REJECT;
        if (node.namespaceURI === "http://www.w3.org/2000/svg" && node.tagName !== "svg") {
          return NodeFilter.FILTER_REJECT; // a chart or icon is one box: what's inside it is drawn by design
        }
        if (node.tagName === "IFRAME") return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
      return /\S/.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const range = document.createRange();
  const hiddenCache = new Map();
  const isHidden = (el) => {
    if (!hiddenCache.has(el)) hiddenCache.set(el, invisible(el) || srOnly(el));
    return hiddenCache.get(el);
  };
  const controls = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeType === 1) {
      const el = node;
      const box = el.matches(CONTROL) || ["IMG", "svg", "VIDEO", "CANVAS", "PROGRESS", "METER"].includes(el.tagName);
      if (!box || isHidden(el)) continue;
      // A link that wraps onto a second line is two boxes, not the rectangle around both.
      const rects = style(el).display === "inline" ? [...el.getClientRects()] : [el.getBoundingClientRect()];
      for (const r of rects) {
        if (r.width < 1 || r.height < 1) continue;
        const a = { el, owner: el, x0: r.left, x1: r.right, y0: r.top, y1: r.bottom, control: el.matches(CONTROL) };
        atoms.push(a);
        if (a.control) controls.push(a);
      }
      continue;
    }
    const owner = node.parentElement;
    if (!owner || isHidden(owner)) continue;
    if (owner.closest("svg")) continue;
    if (owner.tagName === "DETAILS" && !owner.open) continue; // text straight inside a closed <details>, not its summary
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      atoms.push({ el: owner, owner, text: node, x0: r.left, x1: r.right, y0: r.top, y1: r.bottom });
    }
  }

  const found = new Map(); // merged problems
  function report(kind, el, text, extra = "") {
    const w = where(el);
    const key = kind + "|" + (extra || w);
    const top = Math.round(el.getBoundingClientRect().top + window.scrollY);
    const seen = found.get(key);
    if (seen) {
      seen.count += 1;
      seen.top = Math.min(seen.top, top);
    } else {
      found.set(key, { kind, text, where: w, top, count: 1 });
    }
    const marks = el.dataset.layoutProblem ? el.dataset.layoutProblem.split(" ") : [];
    if (!marks.includes(kind)) el.dataset.layoutProblem = [...marks, kind].join(" ");
  }

  // -- 1. The page scrolls sideways ----------------------------------------------------------------------
  const pageWidth = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
  if (pageWidth > vw + SLACK) {
    // The elements that stick out: past the right edge and outside any box that scrolls sideways.
    const past = [];
    for (const el of document.body.querySelectorAll("*")) {
      if (SKIP_TAGS.has(el.tagName) || (el.closest("svg") !== null && el.tagName !== "svg")) continue;
      const r = el.getBoundingClientRect();
      // Screen-reader text counts here: left unclipped, it widens the page like anything else.
      if (r.right <= vw + SLACK || r.width < 1 || invisible(el)) continue;
      if (region(clipParent(el)).scrollX) continue;
      past.push(el);
    }
    // The outermost of them. A parent past the edge inside a box that scrolls sideways doesn't count: then
    // the child is what escapes the box (an absolutely positioned label in a scrolling table, say).
    const widening = new Set(past);
    const culprits = past.filter((el) => !widening.has(el.parentElement));
    // Named once each (every row's copy of one control is one name), widest first.
    const widest = new Map();
    for (const el of culprits) {
      const w = where(el);
      widest.set(w, Math.max(widest.get(w) || 0, Math.round(el.getBoundingClientRect().right - vw)));
    }
    const names = [...widest]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4)
      .map(([w, px]) => `${w} (${px}px past)`);
    found.set("page-scroll", {
      kind: "page-scroll",
      text: `The page scrolls sideways: ${pageWidth}px wide in a ${vw}px window`,
      where: names.join("; ") || "html",
      top: culprits.length ? Math.round(Math.min(...culprits.map((c) => c.getBoundingClientRect().top)) + window.scrollY) : 0,
      count: 1,
    });
    culprits.forEach((el) => (el.dataset.layoutProblem = "page-scroll"));
  }

  // -- 2. Cut off by an ancestor -------------------------------------------------------------------------
  for (const a of atoms) {
    const reg = region(a.text ? a.owner : clipParent(a.el));
    const c = reg.cut;
    const outX = a.x0 < c.x0 - SLACK || a.x1 > c.x1 + SLACK;
    const outY = a.y0 < c.y0 - SLACK || a.y1 > c.y1 + SLACK;
    if (!outX && !outY) continue;
    const cutter = outX ? reg.byX : reg.byY;
    if (!cutter || isHidden(cutter)) continue;
    if (a.text && ellipsis(a.owner, cutter)) continue;
    // Wholly outside what's visible: a collapsed or off-canvas part, hidden on purpose.
    const v = meet(a, c);
    if (v.x1 - v.x0 <= 0 || v.y1 - v.y0 <= 0) continue;
    const what = a.text ? `"${short(clean(a.text.nodeValue), 40)}"` : `${a.el.tagName.toLowerCase()} "${label(a.el)}"`;
    const side = outX ? (a.x0 < c.x0 - SLACK ? "left" : "right") : a.y0 < c.y0 - SLACK ? "top" : "bottom";
    // One line per box doing the cutting, with the first thing it cuts: usually one long word is behind all.
    const key = `${where(cutter)}|${side}`;
    if (!found.has("cut-off|" + key)) {
      found.set("cut-off|" + key, {
        kind: "cut-off",
        text: `${a.text ? "Text" : "A control"} is cut off on the ${side} by ${selector(cutter)}, first ${what}`,
        where: where(a.owner),
        top: Math.round(a.y0 + window.scrollY),
        count: 0,
      });
    }
    report("cut-off", a.owner, "", key);
  }

  // -- 3. Drawn on top of each other ---------------------------------------------------------------------
  // Only what is left visible, bucketed by 64px rows so each piece is compared with its neighbours. A bar
  // pinned to the window (fixed, or sticky to the bottom like a form's Save bar) covers what scrolls under
  // it by design, so it's left out.
  const floats = new Map();
  const floating = (el) => {
    if (!el || el.nodeType !== 1) return false;
    if (!floats.has(el)) {
      const s = style(el);
      floats.set(el, s.position === "fixed" || (s.position === "sticky" && s.bottom !== "auto") || floating(el.parentElement));
    }
    return floats.get(el);
  };
  const shown = [];
  for (const a of atoms) {
    if (floating(a.owner)) continue;
    const reg = region(a.text ? a.owner : clipParent(a.el));
    const v = meet(a, reg.view);
    if (v.x1 - v.x0 < 1 || v.y1 - v.y0 < 1) continue;
    shown.push({ ...a, ...v });
  }
  const ROW = 64;
  const rows = new Map();
  shown.forEach((a, i) => {
    for (let r = Math.floor(a.y0 / ROW); r <= Math.floor(a.y1 / ROW); r++) {
      if (!rows.has(r)) rows.set(r, []);
      rows.get(r).push(i);
    }
  });
  const pairs = new Set();
  const FIELD = new Set(["INPUT", "SELECT", "TEXTAREA"]);
  const inside = (a, b) => a.x0 >= b.x0 - SLACK && a.x1 <= b.x1 + SLACK && a.y0 >= b.y0 - SLACK && a.y1 <= b.y1 + SLACK;
  // An icon drawn inside a field (the magnifier in a search box) is there by design.
  const iconInField = (a, b) => !a.text && !a.control && FIELD.has(b.el.tagName) && inside(a, b);
  const related = (a, b) =>
    a.owner === b.owner ||
    a.owner.contains(b.owner) ||
    b.owner.contains(a.owner) ||
    iconInField(a, b) ||
    iconInField(b, a);
  for (const bucket of rows.values()) {
    for (let i = 0; i < bucket.length; i++) {
      for (let j = i + 1; j < bucket.length; j++) {
        const a = shown[bucket[i]];
        const b = shown[bucket[j]];
        const w = Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0);
        const h = Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0);
        if (w <= 3 || h <= 3) continue;
        // A quarter of the smaller piece: lines of text whose boxes touch by a pixel or two are fine.
        const smaller = Math.min((a.x1 - a.x0) * (a.y1 - a.y0), (b.x1 - b.x0) * (b.y1 - b.y0));
        if (w * h < 0.25 * smaller) continue;
        if (related(a, b)) continue;
        const key = bucket[i] < bucket[j] ? bucket[i] + ":" + bucket[j] : bucket[j] + ":" + bucket[i];
        if (pairs.has(key)) continue;
        pairs.add(key);
        const name = (x) => (x.text ? `"${short(clean(x.text.nodeValue), 30)}"` : `${x.el.tagName.toLowerCase()} "${label(x.el)}"`);
        report(
          "overlap",
          a.owner,
          `${name(a)} overlaps ${name(b)} (${selector(b.owner)})`,
          `${where(a.owner)}|${selector(b.owner)}`,
        );
        b.owner.dataset.layoutProblem = "overlap";
      }
    }
  }

  // -- 4. Controls past the edge of the window -----------------------------------------------------------
  if (pageWidth <= vw + SLACK) {
    for (const a of controls) {
      const reg = region(clipParent(a.el));
      if (reg.scrollX) continue; // inside a box that scrolls sideways: it can be reached
      const v = meet(a, reg.view);
      if (v.x1 - v.x0 < 1 || v.y1 - v.y0 < 1) continue; // hidden by its container on purpose
      if (v.x1 <= 0) continue; // parked off the left edge on purpose (a skip link)
      if (v.x0 < -SLACK || v.x1 > vw + SLACK) {
        report(
          "off-screen",
          a.el,
          `${a.el.tagName.toLowerCase()} "${label(a.el)}" is ${v.x0 < -SLACK ? "past the left" : "past the right"} edge of the window`,
        );
      }
    }
  }

  // -- 5. Boxes that scroll sideways inside the page ----------------------------------------------------
  // At laptop widths any table or box; on a phone, a table stacked into one card per row, which should fit.
  {
    for (const el of document.body.querySelectorAll("*")) {
      const s = style(el);
      if (!(s.overflowX === "auto" || s.overflowX === "scroll")) continue;
      if (el.scrollWidth <= el.clientWidth + SLACK || el.clientWidth === 0 || isHidden(el)) continue;
      if (s.direction === "rtl") continue; // a time chart that opens on the newest days scrolls by design
      const stacked = [...el.querySelectorAll("table.stack-sm")].some((t) => t.tHead && style(t.tHead).display === "none");
      if (!options.desktop && !stacked) continue;
      const heads = [...el.querySelectorAll("thead th")]
        .map((th) => [clean(th.textContent) || "(no heading)", Math.round(th.getBoundingClientRect().width)])
        .sort((x, y) => y[1] - x[1])
        .slice(0, 3)
        .map(([t, w]) => `${short(t, 25)} ${w}px`);
      // On a phone the headings are hidden: name the fields whose text runs past the box instead.
      const edge = el.getBoundingClientRect().right + SLACK;
      const wide = [];
      if (stacked && !options.desktop) {
        for (const td of el.querySelectorAll("td")) {
          range.selectNodeContents(td);
          const name = td.dataset.label || column(td) || short(clean(td.textContent), 25);
          if (range.getBoundingClientRect().right > edge && !wide.includes(name)) wide.push(name);
          if (wide.length === 3) break;
        }
      }
      // Would the table fit with every column wrapped as narrow as its longest word? Then the columns are
      // wider than they need to be; if not, the table has more columns than the box can show.
      let fit = "";
      const tables = [...el.querySelectorAll("table")];
      if (options.desktop && tables.length) {
        const before = tables.map((t) => t.style.width);
        const scrolled = el.scrollWidth;
        tables.forEach((t) => (t.style.width = "min-content"));
        const narrowest = el.scrollWidth;
        tables.forEach((t, i) => (t.style.width = before[i]));
        fit =
          narrowest <= el.clientWidth + SLACK
            ? "; it would fit if its columns wrapped"
            : `; even with every column wrapped it needs ${narrowest}px`;
        if (el.scrollWidth !== scrolled) fit = ""; // didn't settle back: say nothing rather than guess
      }
      report(
        "wide-table",
        el,
        (stacked && !options.desktop ? "Stacked for phones, but " : "") +
          `${el.scrollWidth}px of content in a ${el.clientWidth}px box` +
          (heads.length && options.desktop ? ` (widest columns: ${heads.join(", ")})` : "") +
          (wide.length ? ` (too wide: ${wide.map((w) => `"${w}"`).join(", ")})` : "") +
          fit,
      );
    }
  }

  return [...found.values()].sort((a, b) => a.top - b.top);
}
