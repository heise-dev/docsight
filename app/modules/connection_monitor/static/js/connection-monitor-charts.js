/**
 * Connection Monitor Charts - PingPlotter-style combined latency view
 * All targets overlaid in one chart with threshold zones and packet loss markers.
 * Uses renderChart() from chart-engine.js with custom loss markers plugin.
 */
/* global renderChart, charts, bandPlugin */
var CMCharts = (function() {
    'use strict';

    var TARGET_COLORS = [
        'rgba(156,163,175,0.9)',  // gray (gateway/local)
        'rgba(96,165,250,0.9)',   // blue
        'rgba(251,146,60,0.9)',   // orange
        'rgba(168,85,247,0.9)',   // purple
        'rgba(52,211,153,0.9)',   // teal
        'rgba(251,113,133,0.9)'   // pink
    ];

    var CONTROLS_ID = 'cm-chart-controls';
    var READOUT_ID = 'cm-chart-readout';
    var CONTROLS_STORAGE_KEY = 'docsight-cm-chart-controls';
    /* Clip the y ceiling to this percentile of the visible lines, but only when the
       plain maximum sits more than CLIP_EXCESS above it - i.e. a real outlier. */
    var CLIP_PERCENTILE = 0.99;
    var CLIP_EXCESS = 1.25;

    /* A one-finger move shorter than this is still undecided, so the browser keeps
       its chance to turn the gesture into a vertical page scroll. */
    var TOUCH_SLOP = 8;
    /* Two fingers closer together than this make the pinch ratio explode. */
    var MIN_PINCH_PX = 24;

    /* Controls strip state; the single source of truth for series visibility. */
    var controls = loadControls();
    /* Last renderCombinedChart() arguments, so a toggle can re-render without refetching. */
    var lastRenderArgs = null;

    function loadControls() {
        var state = { loss: true, clip: false, smooth: false, targets: {} };
        try {
            var stored = JSON.parse(localStorage.getItem(CONTROLS_STORAGE_KEY));
            if (stored) {
                if (stored.loss === false) state.loss = false;
                if (stored.clip === true) state.clip = true;
                if (stored.smooth === true) state.smooth = true;
                if (typeof stored.targets === 'object' && stored.targets !== null &&
                    !Array.isArray(stored.targets)) {
                    Object.keys(stored.targets).forEach(function(id) {
                        var t = stored.targets[id];
                        if (!t) return;
                        state.targets[id] = { line: t.line !== false, band: t.band !== false };
                    });
                }
            }
        } catch (err) {}
        return state;
    }

    function saveControls() {
        try {
            localStorage.setItem(CONTROLS_STORAGE_KEY, JSON.stringify(controls));
        } catch (err) {}
    }

    /* Unknown target ids fall back to the defaults (line and band both on). */
    function targetControls(targetId) {
        var key = String(targetId);
        if (!controls.targets[key]) controls.targets[key] = { line: true, band: true };
        return controls.targets[key];
    }

    function toggleControl(toggle, targetId) {
        if (toggle === 'loss') controls.loss = !controls.loss;
        else if (toggle === 'clip') controls.clip = !controls.clip;
        else if (toggle === 'smooth') controls.smooth = !controls.smooth;
        else {
            var t = targetControls(targetId);
            t[toggle] = !t[toggle];
        }
        saveControls();
        rerender();
    }

    /**
     * Re-render the combined chart from the cached arguments. No refetch, and the
     * xDomainKey is unchanged, so the engine carries the zoom across the rebuild.
     */
    function rerender() {
        if (!lastRenderArgs) return;
        renderCombinedChart(lastRenderArgs.containerId, lastRenderArgs.allTargetData,
            lastRenderArgs.range, lastRenderArgs.domainKey);
    }

    function controlButton(text, toggle, targetId, targetName) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cm-chart-toggle';
        btn.dataset.toggle = toggle;
        if (targetId != null) btn.dataset.target = String(targetId);
        btn.textContent = text;
        // The visible text is just "line"/"band"; screen readers need the target too
        if (targetName) btn.setAttribute('aria-label', targetName + ' ' + text);
        btn.onclick = function() { toggleControl(toggle, targetId); };
        return btn;
    }

    /* The target name itself is the line toggle, so the whole row is one big hit area. */
    function targetButton(row) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cm-chart-target';
        btn.dataset.toggle = 'line';
        btn.dataset.target = String(row.id);
        var dot = document.createElement('span');
        dot.className = 'cm-target-dot';
        dot.style.background = row.color;
        btn.appendChild(dot);
        var name = document.createElement('span');
        name.className = 'cm-chart-control-label';
        name.textContent = row.label;
        btn.appendChild(name);
        if (row.host) {
            var hostSpan = document.createElement('span');
            hostSpan.className = 'cm-target-host';
            hostSpan.textContent = '(' + row.host + ')';
            btn.appendChild(hostSpan);
        }
        btn.onclick = function() { toggleControl('line', row.id); };
        return btn;
    }

    function buildControlsStrip(container, rows) {
        var lBand = container.dataset.lBand || 'band';
        var lLoss = container.dataset.lLossMarkers || 'loss markers';
        var lClip = container.dataset.lClipSpikes || 'clip spikes';
        var lSmooth = container.dataset.lSmooth || 'smooth';
        var lNoBand = container.dataset.lNoBand || 'No min/max values in this view';

        container.textContent = '';
        var list = document.createElement('ul');
        list.className = 'cm-chart-target-list';
        rows.forEach(function(row) {
            var line = document.createElement('li');
            line.className = 'cm-chart-control-row';
            line.appendChild(targetButton(row));
            var targetName = row.label + (row.host ? ' (' + row.host + ')' : '');
            var band = controlButton(lBand, 'band', row.id, targetName);
            if (!row.hasBand) {
                // Disabled rather than hidden, so the row layout does not jump per range
                band.disabled = true;
                band.title = lNoBand;
            }
            line.appendChild(band);
            list.appendChild(line);
        });
        container.appendChild(list);

        var globals = document.createElement('div');
        globals.className = 'cm-chart-control-globals';
        globals.appendChild(controlButton(lLoss, 'loss', null));
        globals.appendChild(controlButton(lClip, 'clip', null));
        globals.appendChild(controlButton(lSmooth, 'smooth', null));
        container.appendChild(globals);
    }

    function syncControlsStrip(container) {
        var buttons = container.querySelectorAll('button[data-toggle]');
        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            var toggle = btn.dataset.toggle;
            var on;
            if (toggle === 'loss') on = controls.loss;
            else if (toggle === 'clip') on = controls.clip;
            else if (toggle === 'smooth') on = controls.smooth;
            else on = !btn.disabled && targetControls(btn.dataset.target)[toggle];
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        }
    }

    /**
     * Render the controls strip below the chart: a list with one row per target
     * (the name is the line toggle, the band toggle sits in the right column) plus
     * the global toggles. The DOM is rebuilt only when the target signature changes,
     * so the 10s refresh never steals focus from a button the user is on.
     * @param {Array} rows - [{id, label, host, color, hasBand}]
     */
    function renderControlsStrip(rows) {
        var container = document.getElementById(CONTROLS_ID);
        if (!container) return;
        var signature = rows.map(function(r) {
            return r.id + ':' + (r.hasBand ? 1 : 0) + ':' + r.label + '@' + (r.host || '');
        }).join('|');
        if (container._cmSignature !== signature) {
            buildControlsStrip(container, rows);
            container._cmSignature = signature;
        }
        syncControlsStrip(container);
    }

    function percentileOf(values, p) {
        var sorted = values.slice().sort(function(a, b) { return a - b; });
        var idx = Math.floor(sorted.length * p);
        if (idx >= sorted.length) idx = sorted.length - 1;
        return sorted[idx];
    }

    /**
     * uPlot plugin: drag-to-zoom on X-axis, double-click to reset.
     * Requires zoomable:true in renderChart opts (disables fixed x-scale range).
     * @param {Array<number>} [timestamps] - Epoch seconds per x index. When given, a
     *   drag hands the selected absolute window to the detail view, which refetches
     *   exactly that window so the zoom cannot drift away on the next poll.
     */
    function zoomPlugin(timestamps) {
        // Fractional x index -> epoch seconds (linear between neighbouring samples)
        function indexToTime(idx) {
            var last = timestamps.length - 1;
            if (idx <= 0) return timestamps[0];
            if (idx >= last) return timestamps[last];
            var i = Math.floor(idx);
            return timestamps[i] + (timestamps[i + 1] - timestamps[i]) * (idx - i);
        }
        // The reset button must also show on a fresh instance built from a zoomed
        // refetch, where the x-domain key changed and _zoomRange was dropped.
        function isZoomed(u) {
            if (u._zoomRange) return true;
            return typeof window.cmHasZoomWindow === 'function' && window.cmHasZoomWindow();
        }
        function clearZoom(u) {
            u._zoomRange = null;
            if (typeof window.cmClearZoomWindow === 'function') window.cmClearZoomWindow();
            u.setScale('x', { min: 0, max: u.data[0].length - 1 });
        }
        function showResetBtn(u) {
            if (u._resetBtn) { u._resetBtn.style.display = ''; return; }
            var btn = document.createElement('button');
            btn.textContent = '\u2715 Reset Zoom';
            btn.style.cssText = 'position:absolute;top:8px;right:8px;z-index:10;' +
                'font-size:0.7rem;padding:3px 8px;border:1px solid rgba(255,255,255,0.2);' +
                'border-radius:4px;background:rgba(30,30,30,0.85);color:#ccc;cursor:pointer;' +
                'backdrop-filter:blur(4px);transition:opacity 0.15s;';
            btn.onmouseenter = function() { btn.style.color = '#fff'; };
            btn.onmouseleave = function() { btn.style.color = '#ccc'; };
            btn.onclick = function() {
                clearZoom(u);
                btn.style.display = 'none';
            };
            u.root.style.position = 'relative';
            u.root.appendChild(btn);
            u._resetBtn = btn;
        }
        function hideResetBtn(u) {
            if (u._resetBtn) u._resetBtn.style.display = 'none';
        }
        // Y window for the zoomed x range: scan every series (incl. the hidden
        // _min_/_max_ band helpers, which are still drawn) for finite samples.
        function setYZoom(u, min, max) {
            var i0 = Math.max(0, Math.ceil(min));
            var i1 = Math.min(u.data[0].length - 1, Math.floor(max));
            var lo = null;
            var hi = null;
            for (var s = 1; s < u.data.length; s++) {
                for (var i = i0; i <= i1; i++) {
                    var v = u.data[s][i];
                    if (v == null || !isFinite(v)) continue;
                    if (lo === null || v < lo) lo = v;
                    if (hi === null || v > hi) hi = v;
                }
            }
            // No samples in the window: leave y alone rather than pin a degenerate range
            if (lo === null) return;
            var pad = (hi - lo) * 0.1;
            if (pad < 1) pad = 1; // flat window: keep a readable band
            u._zoomRange.yMin = Math.max(0, lo - pad);
            u._zoomRange.yMax = hi + pad;
        }
        return {
            hooks: {
                init: [function(u) {
                    u.over.style.cursor = 'crosshair';
                    // Hint: show drag-to-zoom tooltip on first hover
                    u.over.title = 'Drag to zoom, double-click to reset';
                    // Commit an index window as the zoom: instant preview plus the
                    // absolute window every following fetch asks for. The touch
                    // plugin's pinch commits through this too, so a drag and a
                    // pinch can never drift apart.
                    u._cmZoomTo = function(min, max) {
                        u._zoomRange = { min: min, max: max };
                        setYZoom(u, min, max);
                        u.setScale('x', u._zoomRange);
                        showResetBtn(u);
                        if (timestamps && timestamps.length > 1 &&
                            typeof window.cmSetZoomWindow === 'function') {
                            // Third arg: the right edge is on (or within one sample
                            // of) the newest sample the chart holds.
                            window.cmSetZoomWindow(indexToTime(min), indexToTime(max),
                                max >= timestamps.length - 2);
                        }
                    };
                    // Undo the zoom, window and all. A phone has no double-click, so
                    // the touch plugin's pinch-out goes through the same path.
                    u._cmZoomClear = function() {
                        clearZoom(u);
                        hideResetBtn(u);
                    };
                }],
                ready: [function(u) {
                    u.over.addEventListener('dblclick', function() {
                        u._cmZoomClear();
                    });
                    // A zoomed refetch builds a chart whose x-domain IS the zoom
                    // window, so no setScale follows to raise the button.
                    if (isZoomed(u)) showResetBtn(u);
                }],
                setSelect: [function(u) {
                    var min = u.posToVal(u.select.left, 'x');
                    var max = u.posToVal(u.select.left + u.select.width, 'x');
                    // Zoom the current render straight away for instant feedback;
                    // the refetch of the same window then replaces it.
                    if (max - min > 1) u._cmZoomTo(min, max);
                    u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
                }],
                // Covers the restored zoom of a re-rendered chart, where the
                // reset button would otherwise be gone with the old instance.
                setScale: [function(u) {
                    if (isZoomed(u)) showResetBtn(u);
                    else hideResetBtn(u);
                }]
            }
        };
    }

    /**
     * uPlot plugin: one-finger scrub and two-finger pinch zoom.
     * uPlot 1.6 is mouse-only, so a phone gets no cursor and no zoom at all. Only
     * pushed on a coarse pointer (see renderCombinedChart). The listeners sit on
     * u.over and die with the instance, like the dblclick binding in zoomPlugin.
     */
    function touchPlugin() {
        var rect = null;    // u.over's viewport rect, taken at gesture start
        var mode = null;    // 'pending' | 'scrub' | 'scroll' | 'pinch'
        var startX = 0;
        var startY = 0;
        var pinch = null;   // x values under the two fingers when the pinch started
        var zoomTo = null;  // last window the pinch applied; null = nothing to commit
        var preZoom = null; // _zoomRange before the pinch, to fall back on if it is abandoned

        function moveCursor(u, touch) {
            // Drives u.cursor and every setCursor hook - readout included
            u.setCursor({ left: touch.clientX - rect.left, top: touch.clientY - rect.top });
        }

        // The two finger positions in plot pixels, left one first
        function fingers(e) {
            var a = e.touches[0].clientX - rect.left;
            var b = e.touches[1].clientX - rect.left;
            return a <= b ? { a: a, b: b } : { a: b, b: a };
        }

        // Keep the value under each finger where the finger is: the span scales with
        // the distance between them, so the same gesture zooms and pans in one move.
        function scaleTo(u, e) {
            var f = fingers(e);
            if (f.b - f.a < MIN_PINCH_PX || rect.width <= 0) return;
            var span = rect.width * (pinch.b - pinch.a) / (f.b - f.a);
            var min = pinch.a - f.a * span / rect.width;
            var max = min + span;
            // Clamped to the loaded data: pinching out past it has nothing to show
            var last = u.data[0].length - 1;
            if (min < 0) min = 0;
            if (max > last) max = last;
            if (max - min <= 1) return;
            zoomTo = { min: min, max: max };
            // The engine's x range fn reads _zoomRange, so the live preview needs it
            // set - _cmZoomTo then commits the same window on touchend.
            u._zoomRange = zoomTo;
            u.setScale('x', zoomTo);
        }

        // A poll rebuild during the gesture destroys the instance while these
        // listeners live on in the old u.over; nothing may be written to it then.
        function alive(u) {
            return u.root && u.root.isConnected;
        }

        // Put an abandoned gesture back: only a commit may keep the preview's
        // _zoomRange, which the engine would otherwise re-apply on every poll.
        function restorePreview(u) {
            if (!zoomTo) return;
            zoomTo = null;
            if (!alive(u)) return;
            u._zoomRange = preZoom;
            u.setScale('x', preZoom || { min: 0, max: u.data[0].length - 1 });
        }

        function commitPinch(u) {
            if (!zoomTo) return;  // a pinch that never moved has nothing to commit
            if (!alive(u)) { zoomTo = null; return; }
            var last = u.data[0].length - 1;
            // Pinched back out to the full view: on a phone this is the undo
            // gesture, so it clears a committed window like the desktop dblclick.
            if (zoomTo.min <= 0 && zoomTo.max >= last) {
                if (typeof u._cmZoomClear === 'function') u._cmZoomClear();
            } else if (typeof u._cmZoomTo === 'function') {
                u._cmZoomTo(zoomTo.min, zoomTo.max);
            }
            zoomTo = null;
        }

        return {
            hooks: {
                init: [function(u) {
                    // Vertical page scrolling stays the browser's, horizontal moves and
                    // pinches are ours - and it never page-zooms on the plot.
                    u.over.style.touchAction = 'pan-y';
                }],
                ready: [function(u) {
                    u.over.addEventListener('touchstart', function(e) {
                        rect = u.over.getBoundingClientRect();
                        if (e.touches.length === 1) {
                            mode = 'pending';
                            startX = e.touches[0].clientX;
                            startY = e.touches[0].clientY;
                            moveCursor(u, e.touches[0]);  // a plain tap already reads out
                        } else if (e.touches.length > 1) {
                            // A further finger only re-seeds a running pinch: the
                            // preview and the window to fall back on both stand
                            if (!zoomTo) preZoom = u._zoomRange || null;
                            mode = 'pinch';
                            var f = fingers(e);
                            pinch = f.b - f.a < MIN_PINCH_PX ? null :
                                { a: u.posToVal(f.a, 'x'), b: u.posToVal(f.b, 'x') };
                        }
                    }, { passive: true });
                    u.over.addEventListener('touchmove', function(e) {
                        if (mode === 'pinch') {
                            if (!pinch || e.touches.length < 2) return;
                            scaleTo(u, e);
                            if (e.cancelable) e.preventDefault();
                            return;
                        }
                        if (mode === 'scroll' || mode === null || e.touches.length !== 1) return;
                        var touch = e.touches[0];
                        if (mode === 'pending') {
                            var dx = Math.abs(touch.clientX - startX);
                            var dy = Math.abs(touch.clientY - startY);
                            if (dx < TOUCH_SLOP && dy < TOUCH_SLOP) return;
                            // A mostly vertical move belongs to the page, not the chart,
                            // and stays that way until the finger comes off again
                            mode = dy > dx ? 'scroll' : 'scrub';
                            if (mode === 'scroll') return;
                        }
                        moveCursor(u, touch);
                        // Only the horizontal scrub is ours to swallow; touch-action
                        // already leaves the vertical pan to the browser
                        if (e.cancelable) e.preventDefault();
                    }, { passive: false });
                    u.over.addEventListener('touchend', function(e) {
                        if (e.touches.length > 0) {
                            // Down to one finger: carry on as a scrub instead of
                            // staying stuck in pinch mode. The pinched window is
                            // still pending and commits when that finger lifts.
                            if (mode === 'pinch' && e.touches.length === 1) {
                                mode = 'pending';
                                startX = e.touches[0].clientX;
                                startY = e.touches[0].clientY;
                                pinch = null;
                            }
                            return;
                        }
                        commitPinch(u);
                        mode = null;
                        pinch = null;
                    });
                    u.over.addEventListener('touchcancel', function() {
                        // The browser took the gesture (a page scroll, usually)
                        restorePreview(u);
                        mode = null;
                        pinch = null;
                    });
                }]
            }
        };
    }

    /**
     * uPlot plugin: fill the fixed readout above the plot from the cursor.
     * A floating tooltip sits under the finger and flips off a ~260px plot, so on a
     * coarse pointer the values live in their own strip instead - which is also why
     * renderCombinedChart drops the tooltip there (tooltip:false).
     * @param {Array<string>} labels - X axis label per index (the sample time).
     * @param {Object} bandByLabel - Line label -> {min, max} arrays, as the tooltip uses.
     * @param {Array<number>} lossIndices - Indices with packet loss.
     */
    function readoutPlugin(labels, bandByLabel, rawByLabel, lossIndices) {
        var readout = null;  // looked up once per render, not per cursor frame
        var lossSet = {};
        lossIndices.forEach(function(idx) { lossSet[idx] = true; });

        function showHint(node) {
            node.textContent = '';
            var hint = document.createElement('span');
            hint.className = 'cm-chart-readout-hint';
            hint.textContent = node.dataset.lHint || 'Touch the chart to read values';
            node.appendChild(hint);
        }

        function chip(color, text) {
            var span = document.createElement('span');
            span.className = 'cm-chart-readout-chip';
            var dot = document.createElement('span');
            dot.className = 'cm-target-dot';
            dot.style.background = color;
            span.appendChild(dot);
            var label = document.createElement('span');
            label.textContent = text;
            span.appendChild(label);
            return span;
        }

        return {
            hooks: {
                ready: [function() {
                    readout = document.getElementById(READOUT_ID);
                    if (!readout) return;
                    // Shown from the first render on, so the first touch cannot push
                    // the chart down; the hint doubles as the cue that scrubbing exists.
                    readout.hidden = false;
                    showHint(readout);
                }],
                setCursor: [function(u) {
                    var node = readout;
                    if (!node) return;
                    var idx = u.cursor.idx;
                    if (idx == null) { showHint(node); return; }
                    node.textContent = '';
                    var time = document.createElement('span');
                    time.className = 'cm-chart-readout-time';
                    time.textContent = labels[idx] || '';
                    node.appendChild(time);
                    // Band helpers carry show:false, so only the visible lines chip up
                    for (var i = 1; i < u.series.length; i++) {
                        var s = u.series[i];
                        if (!s.show) continue;
                        // Raw sample, like the desktop tooltip; the line may be filtered
                        var raw = rawByLabel[s.label];
                        var val = raw ? raw[idx] : u.data[i][idx];
                        if (val == null) continue;
                        var text = s.label + ' ' + val.toFixed(1) + ' ms';
                        var band = bandByLabel[s.label];
                        if (band && band.min[idx] != null && band.max[idx] != null) {
                            text += ' (min ' + band.min[idx].toFixed(1) +
                                ' \u00b7 max ' + band.max[idx].toFixed(1) + ')';
                        }
                        var color = s._stroke || s.stroke;
                        if (typeof color === 'function') color = color(u, i);
                        node.appendChild(chip(color, text));
                    }
                    if (lossSet[idx]) {
                        var loss = document.createElement('span');
                        loss.className = 'cm-chart-readout-loss';
                        loss.textContent = node.dataset.lLoss || 'loss';
                        node.appendChild(loss);
                    }
                }]
            }
        };
    }

    /**
     * uPlot plugin: draw red vertical lines at packet loss indices.
     * Uses 'draw' hook so lines render ON TOP of series (like PingPlotter).
     * `_cmLossMarkers` reports the controls-strip toggle, so a switched-off marker
     * layer stays distinguishable from a window that simply carries no loss;
     * `_cmLossCount` reports how many indices are marked.
     */
    function lossMarkersPlugin(lossIndices, enabled) {
        if (!enabled || !lossIndices || lossIndices.length === 0) {
            return { _cmLossMarkers: !!enabled, _cmLossCount: lossIndices ? lossIndices.length : 0 };
        }
        return {
            _cmLossMarkers: true,
            _cmLossCount: lossIndices.length,
            hooks: {
                draw: [function(u) {
                    var ctx = u.ctx;
                    var dpr = window.devicePixelRatio || 1;
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
                    ctx.clip();
                    ctx.strokeStyle = 'rgba(239,68,68,0.7)';
                    ctx.lineWidth = 1.5 * dpr;
                    for (var i = 0; i < lossIndices.length; i++) {
                        var x = u.valToPos(lossIndices[i], 'x', true);
                        if (x >= u.bbox.left && x <= u.bbox.left + u.bbox.width) {
                            ctx.beginPath();
                            ctx.moveTo(x, u.bbox.top);
                            ctx.lineTo(x, u.bbox.top + u.bbox.height);
                            ctx.stroke();
                        }
                    }
                    ctx.restore();
                }]
            }
        };
    }

    /**
     * uPlot plugin: mark samples cut off by the "clip spikes" ceiling with a small
     * triangle at the top edge, so a clipped outlier is never silently invisible.
     */
    function clipHintsPlugin(clipIndices) {
        if (!clipIndices || clipIndices.length === 0) return {};
        return {
            hooks: {
                draw: [function(u) {
                    var ctx = u.ctx;
                    var dpr = window.devicePixelRatio || 1;
                    var size = 4 * dpr;
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
                    ctx.clip();
                    ctx.fillStyle = 'rgba(239,68,68,0.85)';
                    for (var i = 0; i < clipIndices.length; i++) {
                        var x = u.valToPos(clipIndices[i], 'x', true);
                        if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
                        ctx.beginPath();
                        ctx.moveTo(x, u.bbox.top + dpr);
                        ctx.lineTo(x - size, u.bbox.top + size * 2);
                        ctx.lineTo(x + size, u.bbox.top + size * 2);
                        ctx.closePath();
                        ctx.fill();
                    }
                    ctx.restore();
                    u._cmClipHints = clipIndices.length;
                }]
            }
        };
    }

    function sampleCountOf(sample) {
        return sample && sample.sample_count ? sample.sample_count : 1;
    }

    function lossPctOf(sample) {
        if (!sample) return 0;
        if (sample.packet_loss_pct != null) return sample.packet_loss_pct;
        var sampleCount = sampleCountOf(sample);
        var timeoutCount = sample.timeout_count != null ? sample.timeout_count : (sample.timeout ? sampleCount : 0);
        return sampleCount > 0 ? (timeoutCount / sampleCount * 100) : 0;
    }

    /**
     * Render combined PingPlotter-style chart with all targets overlaid.
     * @param {string} containerId - DOM element ID
     * @param {Array} allTargetData - [{target: {id, label, host}, samples: [...]}]
     * @param {number|string} range - Selected range in seconds or a normalized range key.
     * @param {string} [domainKey] - Optional x-domain identity for the zoom binding;
     *   defaults to the normalized range.
     */
    function renderCombinedChart(containerId, allTargetData, range, domainKey) {
        if (!allTargetData || allTargetData.length === 0) return;

        // Build unified timeline from all targets' samples
        var timeMap = {};
        allTargetData.forEach(function(td) {
            td.samples.forEach(function(s) { timeMap[s.timestamp] = true; });
        });
        var timestamps = Object.keys(timeMap).map(Number).sort(function(a, b) { return a - b; });
        if (timestamps.length === 0) return;

        // Cached so a controls-strip toggle can re-render the same data (see rerender)
        lastRenderArgs = { containerId: containerId, allTargetData: allTargetData,
            range: range, domainKey: domainKey };

        // Build index lookup
        var tsIndex = {};
        for (var i = 0; i < timestamps.length; i++) tsIndex[timestamps[i]] = i;

        var rangeSeconds = timestamps[timestamps.length - 1] - timestamps[0];
        var axisRange;
        if (range !== undefined && range !== null) {
            axisRange = /^\d+$/.test(String(range)) ? String(range) + 's' : range;
        } else {
            axisRange = String(Math.max(Math.round(rangeSeconds), 0)) + 's';
        }
        var labels = docsightFormatXAxisLabels(timestamps, axisRange);

        // Build datasets (one per target) and collect loss indices
        var datasets = [];
        var lossSet = {};
        var bandPlugins = [];
        var controlRows = [];
        // Only the series the controls strip actually shows drive the y ceiling
        var shownLines = [];
        var shownBandMax = [];
        // Line label -> its min/max arrays, for the band values in the tooltip
        var bandByLabel = {};
        // Line label -> its unfiltered samples, so the tooltip and the readout stay exact
        // while smoothing plots a filtered series
        var rawByLabel = {};

        allTargetData.forEach(function(td, tIdx) {
            var shown = targetControls(td.target.id);
            var sampleMap = {};
            td.samples.forEach(function(s) {
                sampleMap[s.timestamp] = s;
                // Loss markers follow the line toggles: a switched-off target's loss is not marked
                if (shown.line && lossPctOf(s) > 0) lossSet[tsIndex[s.timestamp]] = true;
            });
            var data = new Array(timestamps.length);
            var minData = new Array(timestamps.length);
            var maxData = new Array(timestamps.length);
            var hasAggregated = false;
            for (var i = 0; i < timestamps.length; i++) {
                var s = sampleMap[timestamps[i]];
                if (s && s.latency_ms != null) {
                    data[i] = s.latency_ms;
                    minData[i] = s.min_latency_ms;
                    maxData[i] = s.max_latency_ms;
                    if (s.min_latency_ms != null) hasAggregated = true;
                } else {
                    data[i] = null;
                    minData[i] = null;
                    maxData[i] = null;
                }
            }
            var color = TARGET_COLORS[tIdx % TARGET_COLORS.length];
            var label = td.target.label + (td.target.host ? ' (' + td.target.host + ')' : '');
            controlRows.push({
                id: td.target.id,
                label: td.target.label,
                host: td.target.host,
                color: color,
                hasBand: hasAggregated
            });
            rawByLabel[label] = data;
            // Smoothing filters the plotted line only; everything else keeps reading raw
            // (the ceiling, clip hints, loss markers, the band and the tooltip)
            // Explicit show: the strip, not the engine's carried-over legend map, decides
            datasets.push({
                label: label,
                data: controls.smooth ? docsightSmoothSeries(data) : data,
                color: color,
                spanGaps: false,
                dashed: hasAggregated ? true : undefined,
                show: shown.line
            });
            if (shown.line) shownLines.push(data);
            // A switched-off band is not pushed at all, so it leaves the y ceiling,
            // the tooltip, the zoom y scan and the zoom modal in one move.
            if (hasAggregated && shown.band) {
                // The envelope stays raw: it is already a min/max hull, and filtering it
                // would narrow the very outliers it exists to show
                datasets.push({ data: minData, color: 'transparent', label: label + ' min',
                    show: false });
                datasets.push({ data: maxData, color: 'transparent', label: label + ' max',
                    show: false });
                // uPlot series[0] is x-axis, so data indices are offset by +1
                var bandColor = color.replace(/[\d.]+\)$/, '0.12)');
                bandPlugins.push(bandPlugin(datasets.length - 1, datasets.length, bandColor));
                bandByLabel[label] = { min: minData, max: maxData };
                shownBandMax.push(maxData);
            }
        });

        renderControlsStrip(controlRows);

        var lossIndices = Object.keys(lossSet).map(Number).sort(function(a, b) { return a - b; });

        // Compute dynamic Y-max from the visible data with headroom: a hidden line and
        // the helpers of a switched-off band must not dictate the scale
        var dataMax = 0;
        var lineSamples = [];
        shownLines.forEach(function(arr) {
            arr.forEach(function(v) {
                if (v == null) return;
                // Only the clip ceiling needs the individual samples, so skip collecting
                // (and sorting) them on every poll while clipping is switched off
                if (controls.clip) lineSamples.push(v);
                if (v > dataMax) dataMax = v;
            });
        });
        shownBandMax.forEach(function(arr) {
            arr.forEach(function(v) { if (v != null && v > dataMax) dataMax = v; });
        });
        // "Clip spikes": pin the ceiling to the 99th percentile of the visible lines when
        // a rare outlier dominates the scale, so the baseline stops being a flat line
        var ceiling = dataMax;
        if (controls.clip && lineSamples.length > 0) {
            var p99 = percentileOf(lineSamples, CLIP_PERCENTILE);
            if (p99 > 0 && dataMax > p99 * CLIP_EXCESS) ceiling = p99;
        }
        // 40ms floor ensures green zone is always visible with breathing room.
        // Above 30ms: moderate headroom. Above 100ms: tighter headroom.
        var yMax;
        if (ceiling <= 30) yMax = 40;
        else if (ceiling <= 100) yMax = Math.ceil(ceiling * 1.2);
        else yMax = Math.ceil(ceiling * 1.15);

        // Every visible sample the clipped ceiling cuts off gets a hint marker at the
        // top edge - the band envelope included, since it is clipped by the same ceiling
        var clipIndices = [];
        if (ceiling < dataMax) {
            var clipSet = {};
            shownLines.concat(shownBandMax).forEach(function(arr) {
                for (var ci = 0; ci < arr.length; ci++) {
                    if (arr[ci] != null && arr[ci] > yMax) clipSet[ci] = true;
                }
            });
            clipIndices = Object.keys(clipSet).map(Number).sort(function(a, b) { return a - b; });
        }

        // PingPlotter-style threshold zones (vertically scaled backgrounds)
        // lineColor: transparent suppresses the dashed boundary lines
        var zones = [
            { min: 0, max: 30, color: 'rgba(34,197,94,0.12)', lineColor: 'transparent' },
            { min: 30, max: 100, color: 'rgba(234,179,8,0.10)', lineColor: 'transparent' },
            { min: 100, max: 10000, color: 'rgba(239,68,68,0.08)', lineColor: 'transparent' },
            { yMin: 0, yMax: yMax }
        ];

        // uPlot 1.6 has no touch handling, so a coarse pointer gets the scrub/pinch
        // plugin and reads the values from the fixed strip above the plot instead of
        // a tooltip the finger covers. Desktop keeps drag-zoom and the tooltip.
        var touchUi = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
        var touchPlugins = touchUi ?
            [touchPlugin(), readoutPlugin(labels, bandByLabel, rawByLabel, lossIndices)] : [];

        renderChart(containerId, labels, datasets, 'line', zones, {
            yMin: 0,
            zoomable: true,
            legend: false,
            tooltip: !touchUi,
            // Only a clipped ceiling may stay below the data; otherwise widen as before
            yMaxStrict: ceiling < dataMax,
            xDomainKey: domainKey !== undefined && domainKey !== null ? domainKey : axisRange,
            // The one-line labels need ~19px, not uPlot's default 50px reserve
            xAxisSize: 24,
            minHeight: 260,
            maxHeight: 440,
            heightRatio: 0.42,
            tooltipLabelCallback: function(ctx) {
                // The raw sample, not the plotted one - a filtered line is a reading aid
                var raw = rawByLabel[ctx.dataset.label];
                var val = raw ? raw[ctx.dataIndex] : ctx.parsed.y;
                if (val == null) return '';
                var text = ctx.dataset.label + ': ' + val.toFixed(1) + ' ms';
                var band = bandByLabel[ctx.dataset.label];
                if (band) {
                    var lo = band.min[ctx.dataIndex];
                    var hi = band.max[ctx.dataIndex];
                    if (lo != null && hi != null) {
                        text += ' (min ' + lo.toFixed(1) + ' \u00b7 max ' + hi.toFixed(1) + ')';
                    }
                }
                return text;
            },
            plugins: [lossMarkersPlugin(lossIndices, controls.loss),
                clipHintsPlugin(clipIndices), zoomPlugin(timestamps)]
                .concat(bandPlugins).concat(touchPlugins)
        });
    }

    /**
     * Render combined availability band across all targets.
     * Green = all OK, orange = some loss, red = all down.
     */
    function renderAvailabilityBand(containerId, allTargetData) {
        var container = document.getElementById(containerId);
        if (!container) return;

        if (!allTargetData || allTargetData.length === 0) {
            container.textContent = '';
            container.removeAttribute('role');
            container.removeAttribute('aria-label');
            return;
        }

        // Build unified timeline with weighted loss counts
        var timeMap = {};
        allTargetData.forEach(function(td) {
            td.samples.forEach(function(s) {
                var sampleCount = sampleCountOf(s);
                if (!timeMap[s.timestamp]) timeMap[s.timestamp] = { total: 0, lossWeight: 0 };
                timeMap[s.timestamp].total += sampleCount;
                timeMap[s.timestamp].lossWeight += lossPctOf(s) * sampleCount;
            });
        });
        var timestamps = Object.keys(timeMap).map(Number).sort(function(a, b) { return a - b; });
        if (timestamps.length === 0) { container.textContent = ''; return; }

        // Build segments of consecutive same-state
        var segments = [];
        var prevState = stateOf(timeMap[timestamps[0]]);
        var segStart = 0;

        for (var i = 1; i < timestamps.length; i++) {
            var state = stateOf(timeMap[timestamps[i]]);
            if (state !== prevState) {
                segments.push({ state: prevState, start: segStart, end: i });
                prevState = state;
                segStart = i;
            }
        }
        segments.push({ state: prevState, start: segStart, end: timestamps.length });

        container.textContent = '';
        var total = timestamps.length;
        var stateCounts = { ok: 0, degraded: 0, down: 0 };
        segments.forEach(function(seg) {
            stateCounts[seg.state] += seg.end - seg.start;
        });
        var availabilityLabel = container.dataset.lAvailability || 'Availability';
        var okLabel = container.dataset.lOk || 'OK';
        var degradedLabel = container.dataset.lDegraded || 'degraded';
        var downLabel = container.dataset.lDown || 'down';
        container.setAttribute('role', 'img');
        container.setAttribute('aria-label', availabilityLabel + ': ' +
            ((stateCounts.ok / total * 100).toFixed(0)) + '% ' + okLabel + ', ' +
            ((stateCounts.degraded / total * 100).toFixed(0)) + '% ' + degradedLabel + ', ' +
            ((stateCounts.down / total * 100).toFixed(0)) + '% ' + downLabel);
        segments.forEach(function(seg) {
            var pct = ((seg.end - seg.start) / total * 100).toFixed(2);
            var div = document.createElement('div');
            div.className = 'cm-availability-segment ' + seg.state;
            div.style.width = pct + '%';
            div.title = seg.state + ' (' + pct + '%)';
            container.appendChild(div);
        });
    }

    function stateOf(entry) {
        if (!entry || entry.total === 0) return 'ok';
        var lossPct = entry.lossWeight / entry.total;
        if (lossPct >= 100) return 'down';
        if (lossPct > 0) return 'degraded';
        return 'ok';
    }

    // Range stats served from the 60s/300s/3600s buckets are derived, not exact -
    // p95 in particular is biased HIGH (see storage.get_range_stats). tiers_used
    // names the tiers that actually contributed, so anything but pure raw is marked.
    function isApproximateStats(stats) {
        var tiers = stats && Array.isArray(stats.tiers_used) ? stats.tiers_used : [];
        return tiers.some(function(tier) { return tier !== 'raw'; });
    }

    function appendApproxMarker(el, title) {
        var mark = document.createElement('span');
        mark.className = 'cm-approx';
        mark.textContent = '≈';
        mark.title = title;
        el.appendChild(mark);
    }

    /**
     * Render stats cards (min/max/avg latency, packet loss) from sample data.
     */
    function renderStatsCards(containerId, allTargetData) {
        var container = document.getElementById(containerId);
        if (!container) return;
        container.textContent = '';

        if (!allTargetData || allTargetData.length === 0) return;

        // Prefer exact range stats from the backend when present.
        var statsAvailable = allTargetData.every(function(td) { return !!td.stats; });

        var min = null;
        var max = null;
        var avg = null;
        var p95 = null;
        var totalSamples = 0;
        var totalTimeouts = 0;

        if (statsAvailable) {
            var weightedLatencySum = 0;
            var weightedLatencyCount = 0;
            var p95Values = [];

            allTargetData.forEach(function(td) {
                var stats = td.stats;
                var sampleCount = stats.sample_count || 0;
                var latencyCount = stats.latency_count || 0;
                var packetLoss = stats.packet_loss_pct || 0;
                var timeouts = Math.round(sampleCount * packetLoss / 100);

                totalSamples += sampleCount;
                totalTimeouts += timeouts;
                weightedLatencyCount += latencyCount;
                weightedLatencySum += (stats.avg_latency_ms || 0) * latencyCount;

                if (stats.min_latency_ms != null) {
                    min = min == null ? stats.min_latency_ms : Math.min(min, stats.min_latency_ms);
                }
                if (stats.max_latency_ms != null) {
                    max = max == null ? stats.max_latency_ms : Math.max(max, stats.max_latency_ms);
                }
                if (stats.p95_latency_ms != null) {
                    p95Values.push(stats.p95_latency_ms);
                }
            });

            avg = weightedLatencyCount > 0 ? (weightedLatencySum / weightedLatencyCount) : null;
            if (p95Values.length > 0) {
                p95Values.sort(function(a, b) { return a - b; });
                p95 = p95Values[Math.floor(p95Values.length * 0.95)];
            }
        } else {
            var weightedLatencySum = 0;
            var weightedLatencyCount = 0;
            var p95Values = [];
            allTargetData.forEach(function(td) {
                if (!td.samples) return;
                td.samples.forEach(function(s) {
                    var sampleCount = sampleCountOf(s);
                    totalSamples += sampleCount;
                    totalTimeouts += sampleCount * lossPctOf(s) / 100;
                    if (s.latency_ms != null) {
                        weightedLatencySum += s.latency_ms * sampleCount;
                        weightedLatencyCount += sampleCount;
                        var minLatency = s.min_latency_ms != null ? s.min_latency_ms : s.latency_ms;
                        var maxLatency = s.max_latency_ms != null ? s.max_latency_ms : s.latency_ms;
                        min = min == null ? minLatency : Math.min(min, minLatency);
                        max = max == null ? maxLatency : Math.max(max, maxLatency);
                        p95Values.push(s.p95_latency_ms != null ? s.p95_latency_ms : s.latency_ms);
                    }
                });
            });

            if (weightedLatencyCount > 0) {
                avg = weightedLatencySum / weightedLatencyCount;
            }
            if (p95Values.length > 0) {
                p95Values.sort(function(a, b) { return a - b; });
                p95 = p95Values[Math.floor(p95Values.length * 0.95)];
            }
        }

        if (avg == null || min == null || max == null) return;
        var lossPct = totalSamples > 0 ? (totalTimeouts / totalSamples * 100) : 0;
        // The cards blend every target, so one approximate target taints them all
        var approximate = allTargetData.some(function(td) { return isApproximateStats(td.stats); });
        var approxTitle = container.dataset.lApprox || 'Approximate: derived from aggregated buckets, not raw samples. P95 is biased high.';

        var cards = [
            { label: 'Avg Latency', value: avg.toFixed(1) + ' ms', color: avg < 30 ? 'var(--good)' : avg < 100 ? 'var(--warn, orange)' : 'var(--crit)', approximate: approximate },
            { label: 'Min', value: min.toFixed(1) + ' ms', color: 'var(--text-muted)' },
            { label: 'Max', value: max.toFixed(1) + ' ms', color: max > 100 ? 'var(--crit)' : 'var(--text-muted)' },
            { label: 'P95', value: p95 != null ? p95.toFixed(1) + ' ms' : '-', color: p95 != null && p95 > 100 ? 'var(--warn, orange)' : 'var(--text-muted)', approximate: approximate },
            { label: 'Packet Loss', value: lossPct.toFixed(2) + '%', color: lossPct > 2 ? 'var(--crit)' : lossPct > 0 ? 'var(--warn, orange)' : 'var(--good)' },
            { label: 'Samples', value: totalSamples.toLocaleString(), color: 'var(--text-muted)' }
        ];

        cards.forEach(function(c) {
            var card = document.createElement('div');
            card.className = 'cm-kpi-card';
            var val = document.createElement('div');
            val.className = 'cm-kpi-value';
            val.style.setProperty('--cm-kpi-color', c.color);
            val.textContent = c.value;
            if (c.approximate) appendApproxMarker(val, approxTitle);
            var lbl = document.createElement('div');
            lbl.className = 'cm-kpi-label';
            lbl.textContent = c.label;
            card.appendChild(val);
            card.appendChild(lbl);
            container.appendChild(card);
        });
    }

    /**
     * Detect if a host is a private/local IP (gateway, router, LAN device).
     */
    function isPrivateIP(host) {
        if (!host) return false;
        return /^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|127\.)/.test(host);
    }

    /**
     * Render per-target stats comparison table with fault diagnosis.
     * Shows each target's metrics side-by-side so the user can see
     * "Gateway 0% loss, Cloudflare 2% loss = external problem".
     */
    function renderPerTargetStats(containerId, allTargetData) {
        var container = document.getElementById(containerId);
        if (!container) return;
        container.textContent = '';

        if (!allTargetData || allTargetData.length === 0) return;

        // Read i18n labels from data attributes
        var lTarget = container.dataset.lTarget || 'Target';
        var lAvg = container.dataset.lAvg || 'Avg';
        var lP95 = container.dataset.lP95 || 'P95';
        var lLoss = container.dataset.lLoss || 'Packet Loss';
        var lSamples = container.dataset.lSamples || 'Samples';
        var lDiagExt = container.dataset.diagExternal || 'External issue - gateway OK but external targets show packet loss';
        var lDiagInt = container.dataset.diagInternal || 'Internal/ISP issue - gateway also affected';
        var lApprox = container.dataset.lApprox || 'Approximate: derived from aggregated buckets, not raw samples. P95 is biased high.';

        // Calculate per-target stats using weighted computation
        var stats = allTargetData.map(function(td, tIdx) {
            var totalSamples = 0;
            var avg = null;
            var p95 = null;
            var loss = 0;

            if (td.stats) {
                totalSamples = td.stats.sample_count || 0;
                avg = td.stats.avg_latency_ms;
                p95 = td.stats.p95_latency_ms;
                loss = td.stats.packet_loss_pct || 0;
            } else {
                var latencies = [];
                var timeouts = 0;

                if (td.samples) {
                    td.samples.forEach(function(s) {
                        var sampleCount = sampleCountOf(s);
                        totalSamples += sampleCount;
                        timeouts += sampleCount * lossPctOf(s) / 100;
                        if (s.latency_ms != null) {
                            latencies.push(s.p95_latency_ms != null ? s.p95_latency_ms : s.latency_ms);
                        }
                    });
                }

                latencies.sort(function(a, b) { return a - b; });
                avg = latencies.length > 0 ? (latencies.reduce(function(a, b) { return a + b; }, 0) / latencies.length) : null;
                p95 = latencies.length > 0 ? latencies[Math.floor(latencies.length * 0.95)] : null;
                loss = totalSamples > 0 ? (timeouts / totalSamples * 100) : 0;
            }
            return {
                label: td.target.label,
                host: td.target.host,
                color: TARGET_COLORS[tIdx % TARGET_COLORS.length],
                avg: avg,
                p95: p95,
                loss: loss,
                samples: totalSamples,
                isLocal: isPrivateIP(td.target.host),
                approximate: isApproximateStats(td.stats)
            };
        });

        // Build table
        var table = document.createElement('table');
        table.className = 'data-table cm-target-table';

        var thead = document.createElement('thead');
        var headerRow = document.createElement('tr');
        [lTarget, lAvg, lP95, lLoss, lSamples].forEach(function(text, i) {
            var th = document.createElement('th');
            th.textContent = text;
            if (i >= 3) th.className = 'text-right';
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        var tbody = document.createElement('tbody');
        stats.forEach(function(s) {
            var tr = document.createElement('tr');

            // Target with color dot
            var tdTarget = document.createElement('td');
            var dot = document.createElement('span');
            dot.className = 'cm-target-dot';
            dot.style.background = s.color;
            tdTarget.appendChild(dot);
            var nameSpan = document.createElement('span');
            nameSpan.textContent = s.label;
            tdTarget.appendChild(nameSpan);
            if (s.host) {
                var hostSpan = document.createElement('span');
                hostSpan.className = 'cm-target-host';
                hostSpan.textContent = '(' + s.host + ')';
                tdTarget.appendChild(hostSpan);
            }

            var tdAvg = document.createElement('td');
            tdAvg.dataset.label = lAvg;
            tdAvg.textContent = s.avg != null ? s.avg.toFixed(1) + ' ms' : '-';
            if (s.approximate) appendApproxMarker(tdAvg, lApprox);

            var tdP95 = document.createElement('td');
            tdP95.dataset.label = lP95;
            tdP95.textContent = s.p95 != null ? s.p95.toFixed(1) + ' ms' : '-';
            if (s.approximate) appendApproxMarker(tdP95, lApprox);

            // Packet Loss with color
            var tdLoss = document.createElement('td');
            tdLoss.className = 'cm-loss-cell';
            tdLoss.dataset.label = lLoss;
            tdLoss.style.color = s.loss > 2 ? 'var(--crit)' : s.loss > 0 ? 'var(--warn, orange)' : 'var(--good)';
            tdLoss.textContent = s.loss.toFixed(2) + '%';

            var tdSamples = document.createElement('td');
            tdSamples.className = 'cm-samples-cell';
            tdSamples.dataset.label = lSamples;
            tdSamples.textContent = s.samples.toLocaleString();

            tr.appendChild(tdTarget);
            tr.appendChild(tdAvg);
            tr.appendChild(tdP95);
            tr.appendChild(tdLoss);
            tr.appendChild(tdSamples);
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        var tableWrap = document.createElement('div');
        tableWrap.className = 'cm-table-wrap cm-target-table-wrap';
        tableWrap.appendChild(table);
        container.appendChild(tableWrap);

        // Fault diagnosis: compare local vs external targets
        var localStats = stats.filter(function(s) { return s.isLocal; });
        var externalStats = stats.filter(function(s) { return !s.isLocal; });

        var hasExternalLoss = externalStats.some(function(s) { return s.loss > 1; });
        var hasLocalLoss = localStats.some(function(s) { return s.loss > 1; });

        if (hasExternalLoss && !hasLocalLoss && localStats.length > 0) {
            var diag = document.createElement('div');
            diag.className = 'cm-diagnosis external';
            var icon = document.createElement('i');
            icon.setAttribute('data-lucide', 'alert-triangle');
            diag.appendChild(icon);
            var txt = document.createElement('span');
            txt.textContent = lDiagExt;
            diag.appendChild(txt);
            container.appendChild(diag);
            if (window.lucide) lucide.createIcons();
        } else if (hasLocalLoss && hasExternalLoss) {
            var diag = document.createElement('div');
            diag.className = 'cm-diagnosis internal';
            var icon = document.createElement('i');
            icon.setAttribute('data-lucide', 'wifi-off');
            diag.appendChild(icon);
            var txt = document.createElement('span');
            txt.textContent = lDiagInt;
            diag.appendChild(txt);
            container.appendChild(diag);
            if (window.lucide) lucide.createIcons();
        }
    }

    return {
        renderCombinedChart: renderCombinedChart,
        rerender: rerender,
        renderAvailabilityBand: renderAvailabilityBand,
        renderStatsCards: renderStatsCards,
        renderPerTargetStats: renderPerTargetStats,
        TARGET_COLORS: TARGET_COLORS
    };
})();
