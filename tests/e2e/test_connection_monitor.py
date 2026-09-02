"""E2E coverage for Connection Monitor workflows."""

import re
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect


def test_connection_monitor_uses_shared_page_header_action_layout(demo_page):
    """Connection Monitor range controls should live in the same top header pattern as other views."""
    page = demo_page
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")

    header = page.locator("#view-connection-monitor .view-page-header")
    expect(header).to_be_visible()
    expect(header.locator(".view-page-title")).to_have_text("Connection Monitor")
    expect(header.locator(".view-page-actions .cm-range-picker")).to_be_visible()
    expect(header.locator(".view-page-actions [data-cm-range='3600']")).to_be_visible()
    expect(header.locator(".view-page-actions #cm-capability-info")).to_be_visible()
    expect(page.locator("#view-connection-monitor .cm-control-strip")).to_have_count(0)


def test_connection_monitor_pin_day_action_does_not_shift_range_navigation(demo_page):
    """Showing the 1d-only pin action must not move the time-range navigation."""
    page = demo_page
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")

    range_picker = page.locator("#view-connection-monitor .view-page-actions .cm-range-picker")
    expect(range_picker).to_be_visible()
    before = range_picker.bounding_box()
    assert before is not None

    page.locator("#view-connection-monitor [data-cm-range='86400']").click()
    expect(page.locator("#cm-pin-day-btn")).to_be_visible()

    after = range_picker.bounding_box()
    assert after is not None
    assert abs(before["x"] - after["x"]) <= 1, "1d pin action should not shift the time-range controls horizontally"
    expect(page.locator("#view-connection-monitor .cm-range-picker #cm-pin-day-btn")).to_have_count(0)


def test_connection_monitor_raw_ping_log_panel_is_discoverable(demo_page):
    """The Connection Monitor view should expose the ISP-ready raw ping log export panel."""
    page = demo_page
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")

    panel = page.locator("#cm-raw-log-panel")
    expect(panel).to_be_visible()
    expect(panel.get_by_text("Raw Ping Log")).to_be_visible()
    expect(panel.get_by_text("Download per-ping raw samples")).to_be_visible()


def test_connection_monitor_mobile_surfaces_raw_ping_log_without_deep_scroll(demo_page):
    """Mobile users should see raw-log downloads before the long chart/details stack."""
    page = demo_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")

    first_raw_log_button = page.locator("#cm-raw-log-links .cm-chip-btn").first
    expect(first_raw_log_button).to_be_visible()
    button_box = first_raw_log_button.bounding_box()
    chart_box = page.locator("#cm-charts-section").bounding_box()
    panel_box = page.locator("#cm-raw-log-panel").bounding_box()
    assert button_box is not None
    assert chart_box is not None
    assert panel_box is not None
    assert 0 <= panel_box["y"]
    assert 0 <= button_box["y"]
    assert panel_box["y"] < chart_box["y"], "raw log panel should appear before the long chart stack"
    assert button_box["y"] + button_box["height"] <= 844, "raw log download actions should be fully visible without deep mobile scrolling"


def _requested_window(url):
    """Absolute [start, end] the page asked for, or None when unbounded."""
    params = parse_qs(urlparse(url).query)
    if "start" not in params or "end" not in params:
        return None
    return float(params["start"][0]), float(params["end"][0])


def _stub_connection_monitor_api(page, sample_count=240, outlier_index=5, pinned_days=None,
                                 state=None, interval=60, meta=None, target_metas=None,
                                 band_max=None, loss_index=None, stats=None, outages=None):
    """Serve deterministic Connection Monitor data: one target, one latency outlier.

    Samples are served only inside the requested window, so a fetch that narrows
    the window visibly narrows the chart. Pass ``state`` to collect the sample
    timestamps and every requested window, or ``interval`` to space the samples
    closer than a minute apart. ``meta`` chooses the tiers the samples
    envelope reports - a dict, or a callable taking the request URL when the
    answer depends on the resolution asked for. ``target_metas`` instead serves
    one target per entry, each with its own envelope meta. ``band_max`` adds
    per-bucket min/max so the chart draws a min/max band, and ``loss_index``
    marks one sample as fully lost so the loss markers are drawn. ``stats``
    serves the range-stats payload (keyed by target id as a string) and
    ``outages`` the outage list; both keep their empty defaults when omitted.
    """
    now = int(time.time())
    samples = [
        {
            "timestamp": now - (sample_count - i) * interval,
            "latency_ms": 1000.0 if i == outlier_index else 50.0 + (i % 5),
            "packet_loss_pct": 100 if i == loss_index else 0,
            "sample_count": 1,
            "timeout_count": 1 if i == loss_index else 0,
        }
        for i in range(sample_count)
    ]
    if band_max is not None:
        for sample in samples:
            sample["min_latency_ms"] = sample["latency_ms"] - 5.0
            sample["max_latency_ms"] = float(band_max)
    if state is not None:
        state["timestamps"] = [s["timestamp"] for s in samples]
        state["requests"] = []

    def handler(route):
        url = route.request.url
        if "/pinned-days" in url:
            route.fulfill(json=pinned_days or [])
        elif "/targets" in url:
            route.fulfill(json=[
                {"id": i + 1, "label": f"Router {i + 1}" if target_metas else "Router",
                 "host": f"192.168.1.{i + 1}", "enabled": True}
                for i in range(len(target_metas) if target_metas else 1)
            ])
        elif "/samples/" in url:
            window = _requested_window(url)
            served = samples
            if window is not None:
                served = [s for s in samples if window[0] <= s["timestamp"] <= window[1]]
            if state is not None:
                params = parse_qs(urlparse(url).query)
                state["requests"].append({
                    "start": window[0],
                    "end": window[1],
                    "max_points": int(params["max_points"][0]) if "max_points" in params else None,
                    "url": url,
                })
            if target_metas:
                served_meta = target_metas[int(url.split("/samples/")[1].split("?")[0]) - 1]
            elif callable(meta):
                served_meta = meta(url)
            else:
                served_meta = meta or {"resolution": "raw"}
            route.fulfill(json={"meta": served_meta, "samples": served})
        elif "/stats" in url and stats is not None:
            route.fulfill(json=stats)
        elif "/outages/" in url and outages is not None:
            route.fulfill(json=outages)
        elif "/capability" in url:
            route.fulfill(json={"method": "icmp"})
        else:
            route.fulfill(json=[])

    page.route("**/api/connection-monitor/**", handler)
    return sample_count


def _open_connection_monitor_chart(page):
    """Reload with stubbed data so the combined chart is built from known samples."""
    page.reload()
    page.wait_for_load_state("networkidle")
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")
    page.wait_for_function("() => window.charts && window.charts['cm-combined-chart']")
    # Showing the view refetches, so let that land before anything waits on a rebuild
    page.wait_for_load_state("networkidle")


def _reload_cm_range(page, seconds):
    """Click a range button and wait for a genuinely new uPlot instance."""
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.locator(f"#view-connection-monitor [data-cm-range='{seconds}']").click()
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )


def _zoom_cm_chart(page, first_index, last_index):
    """Drag-select the given index window (fires the plugin's setSelect hook).

    Returns the instant preview window (_zoomRange), read in the same synchronous
    turn as the drag so the refetch of the selected window cannot have landed yet.
    """
    return page.evaluate(
        """([i0, i1]) => {
            const u = window.charts['cm-combined-chart'];
            const left = u.valToPos(i0, 'x');
            const width = u.valToPos(i1, 'x') - left;
            u.setSelect({ left: left, width: width, top: 0, height: u.bbox.height }, true);
            return u._zoomRange || null;
        }""",
        [first_index, last_index],
    )


def _hold_next_cm_samples_response(page, ms):
    """Hold the next /samples/ response back, so the state before it lands is observable."""
    page.evaluate(
        """(ms) => {
            const original = window.fetch;
            let armed = true;
            window.fetch = function() {
                const pending = original.apply(this, arguments);
                if (armed && String(arguments[0]).includes('/samples/')) {
                    armed = false;
                    return pending.then(r => new Promise(done => setTimeout(() => done(r), ms)));
                }
                return pending;
            };
        }""",
        ms,
    )


def _rezoom_cm_chart(page, first_index, last_index):
    """Drag-select a window and wait for the refetch it triggers to rebuild the chart.

    Returns the instant pre-refetch preview state.
    """
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    preview = _zoom_cm_chart(page, first_index, last_index)
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )
    return preview


def _cm_x_axis_labels(page):
    """The tick labels the x axis renders (uPlot draws them on canvas, not into the DOM)."""
    return page.evaluate(
        """() => {
            const u = window.charts['cm-combined-chart'];
            const ax = u.axes[0];
            return ax.values(u, ax.splits(u));
        }"""
    )


def _cm_series(page):
    """Label and visibility of every series the combined chart holds."""
    return page.evaluate(
        """() => window.charts['cm-combined-chart'].series.slice(1).map(
            (s) => ({label: s.label, show: !!s.show}))"""
    )


def _cm_chart_flags(page):
    """Which optional chart-controls features the live instance was built with."""
    return page.evaluate(
        """() => {
            const u = window.charts['cm-combined-chart'];
            const plugins = u._docsightParams.opts.plugins;
            return {
                lossMarkers: !!(plugins[0] && plugins[0]._cmLossMarkers),
                clipHints: u._cmClipHints || 0,
                yMaxStrict: !!u._docsightParams.opts.yMaxStrict,
            };
        }"""
    )


def _cm_toggle(page, selector):
    """Click a controls-strip toggle and wait for the re-render it triggers."""
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.locator(selector).click()
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )


def _cm_scales(page):
    return page.evaluate(
        """() => {
            const u = window.charts['cm-combined-chart'];
            return {
                yMin: u.scales.y.min,
                yMax: u.scales.y.max,
                xMin: u.scales.x.min,
                xMax: u.scales.x.max,
                zoom: u._zoomRange || null,
                points: u.data[0].length,
            };
        }"""
    )


def test_connection_monitor_zoom_rescales_y_axis(demo_page):
    """Zooming into a low-latency window must drop the y-max held by an outlier."""
    page = demo_page
    sample_count = _stub_connection_monitor_api(page)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 604800)

    full = _cm_scales(page)
    assert full["yMax"] > 900, "unzoomed y-axis should still cover the 1000 ms outlier"

    # Hold the refetch back so the instant preview can be observed on its own
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    _hold_next_cm_samples_response(page, 3000)
    preview = _zoom_cm_chart(page, sample_count // 2, sample_count - 1)
    assert preview["yMax"] < 100, "the drag should pick a low y window immediately, before the refetch"
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'].scales.y.max < 100", timeout=2500
    )
    assert page.evaluate("() => !!window.charts['cm-combined-chart']._e2eStale"), (
        "the preview y window must be applied to the live chart before the refetch lands"
    )
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )

    zoomed = _cm_scales(page)
    assert zoomed["yMax"] < 100, "zoomed y-axis should rescale to the visible ~50 ms samples"
    assert zoomed["yMax"] >= 54, "zoomed y-axis must still cover the visible data"
    assert zoomed["yMin"] <= 50, "zoomed y-axis must still cover the visible data"

    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.locator("#cm-combined-chart button", has_text="Reset Zoom").click()
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )
    reset = _cm_scales(page)
    assert reset["yMax"] == full["yMax"], "resetting the zoom should restore the full y range"


def test_connection_monitor_switching_range_clears_zoom(demo_page):
    """Selecting another timespan must not inherit the previous range's zoom window."""
    page = demo_page
    sample_count = _stub_connection_monitor_api(page)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 604800)

    _rezoom_cm_chart(page, sample_count // 2, sample_count - 1)
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_be_visible()

    _reload_cm_range(page, 86400)
    after = _cm_scales(page)
    assert after["zoom"] is None, "switching the timespan should clear the zoom state"
    assert after["xMin"] <= 0, "new timespan should show the full x range"
    assert after["xMax"] >= after["points"] - 1, "new timespan should show the full x range"
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_have_count(0)


def test_connection_monitor_zoomed_refetch_rescales_y_and_keeps_the_reset_button(demo_page):
    """The refetched window rescales y from its own samples, not from the zoom-time snapshot."""
    page = demo_page
    sample_count = _stub_connection_monitor_api(page)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 604800)

    preview = _rezoom_cm_chart(page, sample_count // 2, sample_count - 1)
    assert preview["yMin"] > 0, "the instant preview pins y to the samples inside the drag"

    refetched = _cm_scales(page)
    assert refetched["zoom"] is None, "the refetched window replaces the index-space zoom"
    assert refetched["yMin"] == 0, "y is rebuilt from the window's own data, not the zoom snapshot"
    assert refetched["yMax"] < 100, "the rebuilt y range must still exclude the outlier"
    assert refetched["yMax"] >= 54, "the rebuilt y range must still cover the window's data"
    # The reset button belongs to the fresh instance, which has no _zoomRange at all
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_be_visible()


def test_connection_monitor_controls_strip_replaces_the_builtin_legend(demo_page):
    """The strip lists one row per target; the internal band helpers never surface."""
    page = demo_page
    metas = [{"resolution": "raw"} for _ in range(4)]
    _stub_connection_monitor_api(page, outlier_index=-1, band_max=400, target_metas=metas)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    expect(page.locator("#cm-combined-chart .u-legend")).to_have_count(0)
    target_rows = page.locator(
        "#cm-chart-controls .cm-chart-control-row:not(.cm-chart-control-globals)"
    )
    expect(target_rows).to_have_count(4)
    expect(page.locator("#cm-chart-controls .cm-chart-control-globals .cm-chart-toggle")).to_have_count(2)
    expect(page.locator("#cm-chart-controls [data-toggle='line']")).to_have_count(4)
    expect(page.locator("#cm-chart-controls [data-toggle='band']")).to_have_count(4)

    labels = [s["label"] for s in _cm_series(page)]
    assert not [label for label in labels if label.startswith(("_min_", "_max_"))], (
        f"internal band helper names must be gone: {labels}"
    )
    assert "Router 1 (192.168.1.1) max" in labels, f"band helpers follow their target: {labels}"


def test_connection_monitor_hidden_source_survives_data_refresh(demo_page):
    """Hiding a source via the controls strip must persist across the live data refresh."""
    page = demo_page
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")
    page.wait_for_selector("#cm-chart-controls [data-toggle='line']", state="visible")
    # Showing the view refetches; let it land before stamping or reading the instance
    page.wait_for_load_state("networkidle")

    line_toggles = page.locator("#cm-chart-controls [data-toggle='line']")
    assert line_toggles.count() >= 2, "combined chart should plot at least two monitored targets"
    hidden_label = _cm_series(page)[1]["label"]

    _cm_toggle(page, "#cm-chart-controls [data-toggle='line'] >> nth=1")
    expect(line_toggles.nth(1)).to_have_attribute("aria-pressed", "false")

    # Mark the live instance so we can wait for the refresh to rebuild the chart
    page.evaluate("charts['cm-combined-chart']._e2eStale = true")
    page.locator("#view-connection-monitor [data-cm-range='3600']").click()
    page.wait_for_function("() => charts['cm-combined-chart'] && !charts['cm-combined-chart']._e2eStale")

    refreshed = _cm_series(page)
    assert refreshed[1]["label"] == hidden_label
    assert refreshed[1]["show"] is False, "the hidden source must stay hidden across the refresh"
    assert refreshed[0]["show"] is True, "the other sources must stay visible"
    expect(line_toggles.nth(1)).to_have_attribute("aria-pressed", "false")


def test_connection_monitor_switch_refetches_without_waiting_for_the_poll(demo_page):
    """Showing the view must reload at once, not on the next 10s/60s refresh tick."""
    page = demo_page
    # The view builds its chart at load time while still hidden; its refresh timer
    # only ticks while the view is active, so this instance is the stale one.
    page.wait_for_function("() => !!charts['cm-combined-chart']")
    # Silence the 10s poll first, so only the switch itself can replace the instance
    # (otherwise a tick landing inside the timeout would pass the unfixed code too).
    page.evaluate(
        """() => {
            const probe = setInterval(() => {}, 1e6);
            for (let i = 1; i <= probe; i++) clearInterval(i);
            charts['cm-combined-chart']._e2eStale = true;
        }"""
    )

    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")

    # Bounded by the fetch alone now; without the fix nothing replaces the instance
    page.wait_for_function(
        "() => charts['cm-combined-chart'] && !charts['cm-combined-chart']._e2eStale",
        timeout=5000,
    )


def test_connection_monitor_hidden_source_stays_hidden_in_zoom_modal(demo_page):
    """A source hidden via the controls strip must stay hidden in the fullscreen zoom modal."""
    page = demo_page
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")
    page.wait_for_selector("#cm-chart-controls [data-toggle='line']", state="visible")
    # Showing the view refetches; let it land before stamping or reading the instance
    page.wait_for_load_state("networkidle")

    line_toggles = page.locator("#cm-chart-controls [data-toggle='line']")
    assert line_toggles.count() >= 2, "combined chart should plot at least two monitored targets"
    hidden_label = _cm_series(page)[1]["label"]

    _cm_toggle(page, "#cm-chart-controls [data-toggle='line'] >> nth=1")
    expect(line_toggles.nth(1)).to_have_attribute("aria-pressed", "false")

    page.evaluate("openChartZoom('cm-combined-chart')")
    page.wait_for_function("() => window.zoomChart && window.zoomChart.series.length > 2")

    zoom_series = page.evaluate(
        """() => window.zoomChart.series.slice(1).map((s) => ({label: s.label, show: !!s.show}))"""
    )
    assert zoom_series[1]["label"] == hidden_label
    assert zoom_series[1]["show"] is False, "the modal must inherit the hidden source"
    assert zoom_series[0]["show"] is True
    page.evaluate("closeChartZoom()")


def test_connection_monitor_band_toggle_drops_the_envelope_and_the_y_ceiling(demo_page):
    """Switching a band off removes its helper series and the ceiling they held."""
    page = demo_page
    _stub_connection_monitor_api(page, outlier_index=-1, band_max=400)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    with_band = _cm_scales(page)
    assert with_band["yMax"] > 400, "the band max should still drive the ceiling while it is shown"
    assert len(_cm_series(page)) == 3, "line plus the two band helpers"
    tooltip = page.evaluate(
        """() => window.charts['cm-combined-chart']._docsightParams.opts.tooltipLabelCallback(
            {parsed: {y: 50.0}, dataset: {label: 'Router (192.168.1.1)'}, dataIndex: 10})"""
    )
    assert "min 45.0" in tooltip and "max 400.0" in tooltip, tooltip

    _cm_toggle(page, "#cm-chart-controls [data-toggle='band']")
    without_band = _cm_scales(page)
    assert len(_cm_series(page)) == 1, "the helper series must not be pushed at all"
    assert without_band["yMax"] < 100, "the ceiling must follow the visible line, not the band"

    _cm_toggle(page, "#cm-chart-controls [data-toggle='band']")
    assert _cm_scales(page)["yMax"] == with_band["yMax"], "switching the band back restores the ceiling"

    # A clipped ceiling cuts the envelope too, so the band must be hinted as well
    _cm_toggle(page, "#cm-chart-controls [data-toggle='clip']")
    assert _cm_scales(page)["yMax"] < 100, "the band max must not survive the clipped ceiling"
    assert _cm_chart_flags(page)["clipHints"] > 0, "a clipped band envelope must be hinted too"


def test_connection_monitor_loss_marker_toggle_removes_the_markers(demo_page):
    """The packet-loss markers must be switchable off and stay off across a reload."""
    page = demo_page
    _stub_connection_monitor_api(page, loss_index=7)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    assert _cm_chart_flags(page)["lossMarkers"] is True, "loss markers are on by default"
    _cm_toggle(page, "#cm-chart-controls [data-toggle='loss']")
    assert _cm_chart_flags(page)["lossMarkers"] is False

    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)
    assert _cm_chart_flags(page)["lossMarkers"] is False, "the loss toggle must survive a reload"
    expect(page.locator("#cm-chart-controls [data-toggle='loss']")).to_have_attribute(
        "aria-pressed", "false"
    )


def test_connection_monitor_clip_spikes_caps_the_axis_and_marks_the_clipped_samples(demo_page):
    """Clipping pins the ceiling near the baseline and hints at every cut-off sample."""
    page = demo_page
    _stub_connection_monitor_api(page)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    full = _cm_scales(page)
    assert full["yMax"] > 900, "the unclipped ceiling still covers the 1000 ms outlier"
    assert _cm_chart_flags(page)["clipHints"] == 0

    _cm_toggle(page, "#cm-chart-controls [data-toggle='clip']")
    clipped = _cm_scales(page)
    assert clipped["yMax"] < 200, "the ceiling must drop to the 99th percentile of the visible line"
    assert clipped["yMax"] >= 54, "the ceiling must still cover the baseline samples"
    flags = _cm_chart_flags(page)
    assert flags["yMaxStrict"] is True, "the engine must not widen back to the clipped outlier"
    assert flags["clipHints"] == 1, "the clipped outlier must be hinted at the top edge"

    _cm_toggle(page, "#cm-chart-controls [data-toggle='clip']")
    assert _cm_scales(page)["yMax"] == full["yMax"], "switching clipping off restores the full ceiling"


def test_connection_monitor_chart_controls_persist_and_hold_the_zoom(demo_page):
    """Toggles keep the zoom - both the instant window and the refetched one - and survive a reload."""
    page = demo_page
    sample_count = _stub_connection_monitor_api(page)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 604800)

    # Instant preview zoom: the refetch is held back, so _zoomRange is what must survive
    _hold_next_cm_samples_response(page, 3000)
    _zoom_cm_chart(page, sample_count // 2, sample_count - 1)
    _cm_toggle(page, "#cm-chart-controls [data-toggle='clip']")
    preview = _cm_scales(page)
    assert preview["zoom"] is not None, "a toggle must not drop the instant zoom window"
    assert preview["xMin"] > 0, "the instant zoom window must still be applied"
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._zoomRange"
    )

    # Refetched zoom window: the chart's x-domain IS the window, so it cannot be lost
    zoomed = _cm_scales(page)
    _cm_toggle(page, "#cm-chart-controls [data-toggle='line']")
    after = _cm_scales(page)
    assert after["points"] == zoomed["points"], "a toggle must not refetch a wider window"
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_be_visible()

    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)
    reloaded = _cm_series(page)
    assert reloaded[0]["show"] is False, "the hidden line must survive a page reload"
    expect(page.locator("#cm-chart-controls [data-toggle='clip']")).to_have_attribute(
        "aria-pressed", "true"
    )


def test_connection_monitor_pinned_day_does_not_share_zoom_with_live_1d(demo_page):
    """A pinned day and the live 1d range are different x-domains — zoom must not carry over."""
    page = demo_page
    now = int(time.time())
    pinned = [{"date": "2026-08-31", "label": "", "utc_start": now - 86400, "utc_end": now}]
    sample_count = _stub_connection_monitor_api(page, pinned_days=pinned)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    _rezoom_cm_chart(page, sample_count // 2, sample_count - 1)
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_be_visible()

    pinned_btn = page.locator("#cm-pinned-days .cm-chip-btn", has_text="2026-08-31")
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    pinned_btn.click()
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )
    expect(pinned_btn).to_have_attribute("aria-pressed", "true")

    pinned_view = _cm_scales(page)
    assert pinned_view["zoom"] is None, "the pinned day should not inherit the live 1d zoom"
    assert pinned_view["xMin"] <= 0, "the pinned day should show its full x range"
    assert pinned_view["xMax"] >= pinned_view["points"] - 1, "the pinned day should show its full x range"

    # ...and back: a zoom taken on the pinned day must not leak into the live 1d view
    _rezoom_cm_chart(page, sample_count // 2, sample_count - 1)
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_be_visible()
    _reload_cm_range(page, 86400)
    assert _cm_scales(page)["zoom"] is None, "the live 1d view should not inherit the pinned day zoom"
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_have_count(0)


def test_connection_monitor_live_update_does_not_cancel_an_in_progress_zoom_drag(demo_page):
    """A poll landing mid-drag must not destroy the chart and wipe the zoom selection."""
    page = demo_page
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")
    page.wait_for_function(
        "() => window.charts && charts['cm-combined-chart'] && charts['cm-combined-chart'].data[0].length > 20",
        timeout=15000,
    )
    # Showing the view refetches; drain it before the render counter goes in
    page.wait_for_load_state("networkidle")

    # Silence the 10s auto-refresh and count renders so reloads can be awaited deterministically.
    page.evaluate(
        """() => {
            const probe = setInterval(() => {}, 1e6);
            for (let i = 1; i <= probe; i++) clearInterval(i);
            window._e2eRenders = 0;
            const orig = window.renderChart;
            window.renderChart = function(canvasId) {
                if (canvasId === 'cm-combined-chart') window._e2eRenders++;
                return orig.apply(null, arguments);
            };
        }"""
    )

    def reload_chart():
        renders = page.evaluate("() => window._e2eRenders")
        page.evaluate(
            """() => {
                const btn = document.querySelector('#view-connection-monitor [data-cm-range].active');
                window.cmSetRange(btn, Number(btn.dataset.cmRange));
            }"""
        )
        page.wait_for_function("(n) => window._e2eRenders > n", arg=renders, timeout=15000)

    geom = page.evaluate(
        """() => {
            const u = charts['cm-combined-chart'];
            const rect = u.over.getBoundingClientRect();
            const last = u.data[0].length - 1;
            return {
                x1: rect.left + u.valToPos(Math.round(last * 0.2), 'x'),
                x2: rect.left + u.valToPos(Math.round(last * 0.6), 'x'),
                y: rect.top + rect.height / 2
            };
        }"""
    )

    page.mouse.move(geom["x1"], geom["y"])
    page.mouse.down()
    page.mouse.move(geom["x2"], geom["y"], steps=5)
    page.evaluate("() => { charts['cm-combined-chart']._e2eStale = true; }")
    assert page.evaluate("() => charts['cm-combined-chart'].select.width") > 0

    reload_chart()
    assert page.evaluate("() => charts['cm-combined-chart']._e2eStale === true") is True, (
        "live update rebuilt the chart while a zoom drag was in flight"
    )
    assert page.evaluate("() => charts['cm-combined-chart'].select.width") > 0

    page.mouse.up()
    assert page.evaluate("() => charts['cm-combined-chart']._zoomRange != null") is True

    # Recovery: with no drag in flight the very next update rebuilds as before.
    reload_chart()
    assert page.evaluate("() => charts['cm-combined-chart']._e2eStale === undefined") is True

    # Recovery bound: a drag that never ends may delay a rebuild, never block it forever.
    page.evaluate("() => { charts['cm-combined-chart']._e2eStale = true; }")
    page.mouse.move(geom["x1"], geom["y"])
    page.mouse.down()
    page.mouse.move(geom["x2"], geom["y"], steps=5)
    assert page.evaluate("() => charts['cm-combined-chart'].select.width") > 0
    for _ in range(3):
        reload_chart()
    page.mouse.up()
    assert page.evaluate("() => charts['cm-combined-chart']._e2eStale === undefined") is True, (
        "a stuck drag selection must not block chart rebuilds indefinitely"
    )


def test_connection_monitor_zoom_refetches_the_selected_window(demo_page):
    """A drag must refetch exactly the selected absolute window, and nest into it."""
    page = demo_page
    state = {}
    sample_count = _stub_connection_monitor_api(page, state=state)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 604800)

    stamps = state["timestamps"]
    before = len(state["requests"])
    _rezoom_cm_chart(page, 60, 120)

    assert len(state["requests"]) > before, "zooming should refetch the selected window"
    window = state["requests"][-1]
    assert abs(window["start"] - stamps[60]) <= 10, "refetch should start at the selected sample"
    assert abs(window["end"] - stamps[120]) <= 10, "refetch should end at the selected sample"
    # A sub-24h window must still be capped: uncapped raw pings over hours are a
    # far heavier payload than the range the user zoomed out of
    assert window["max_points"] is not None, "a zoom window must ask for a bounded payload"
    assert window["max_points"] <= 1440, "a zoom window must ask for a bounded payload"

    zoomed = _cm_scales(page)
    assert zoomed["points"] < sample_count, "the refetched chart should only hold the window's samples"
    assert zoomed["zoom"] is None, "the window IS the x-domain now — no index-space zoom left"
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_be_visible()

    # Nested zoom: the new window is simply a narrower slice of the current one
    _rezoom_cm_chart(page, 10, 30)
    nested = state["requests"][-1]
    assert abs(nested["start"] - stamps[70]) <= 10, "a nested zoom should narrow from the current window"
    assert abs(nested["end"] - stamps[90]) <= 10, "a nested zoom should narrow from the current window"
    assert nested["start"] > window["start"] and nested["end"] < window["end"]


def test_connection_monitor_second_zoom_wins_over_a_slower_first_response(demo_page):
    """A drag while the previous drag's fetch is in flight must not be overwritten by it."""
    page = demo_page
    state = {}
    _stub_connection_monitor_api(page, state=state)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    stamps = state["timestamps"]
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    _hold_next_cm_samples_response(page, 3000)
    _zoom_cm_chart(page, 60, 120)  # first drag: its response is held back
    _zoom_cm_chart(page, 10, 40)   # second drag while the first is still in flight

    # The second window resolves first and owns the view
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )
    second = state["requests"][-1]
    assert abs(second["start"] - stamps[10]) <= 10, "the second drag should be the one fetched last"
    assert abs(second["end"] - stamps[40]) <= 10, "the second drag should be the one fetched last"

    # ...and the first, slower response must be discarded rather than rebuild the chart.
    # Nothing else can correct it: a fixed zoom window stops the refresh timer.
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.wait_for_timeout(4000)
    assert page.evaluate("() => !!window.charts['cm-combined-chart']._e2eStale"), (
        "the overtaken response must not rebuild the chart onto the abandoned window"
    )


def test_connection_monitor_zoom_at_the_live_edge_follows_new_samples(demo_page):
    """A zoom whose right edge is the newest sample keeps its span but tracks now."""
    page = demo_page
    state = {}
    sample_count = _stub_connection_monitor_api(page, state=state)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    stamps = state["timestamps"]
    span = stamps[sample_count - 1] - stamps[sample_count - 40]
    _rezoom_cm_chart(page, sample_count - 40, sample_count - 1)

    window = state["requests"][-1]
    assert abs((window["end"] - window["start"]) - span) <= 10, "a live-edge zoom keeps its span"
    assert abs(window["end"] - time.time()) <= 5, "a live-edge zoom must end at now, not at the last sample"

    # ...and it keeps polling, so new pings keep arriving in the window
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale",
        timeout=20000,
    )
    polled = state["requests"][-1]
    assert polled["end"] > window["end"], "the live-edge window should have moved forward"
    assert abs((polled["end"] - polled["start"]) - span) <= 10, "the span must stay the same"


def test_connection_monitor_fixed_zoom_window_is_frozen(demo_page):
    """A zoom into past data cannot change, so the poll must leave it completely alone."""
    page = demo_page
    state = {}
    _stub_connection_monitor_api(page, state=state)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    _rezoom_cm_chart(page, 60, 120)
    window = state["requests"][-1]
    requests_after_zoom = len(state["requests"])

    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.wait_for_timeout(11000)  # one full 10s poll interval for a <=1d range

    assert len(state["requests"]) == requests_after_zoom, "a fixed zoom window must not be refetched"
    assert page.evaluate("() => !!window.charts['cm-combined-chart']._e2eStale"), (
        "the zoomed chart must not be rebuilt underneath the user"
    )
    assert state["requests"][-1] == window, "the requested window must not have moved"


def test_connection_monitor_reset_zoom_restores_the_rolling_window(demo_page):
    """Reset Zoom must refetch the rolling range again, not just rescale the view."""
    page = demo_page
    state = {}
    _stub_connection_monitor_api(page, state=state)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 604800)

    _rezoom_cm_chart(page, 60, 120)
    requests_after_zoom = len(state["requests"])

    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.locator("#cm-combined-chart button", has_text="Reset Zoom").click()
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )

    assert len(state["requests"]) > requests_after_zoom, "resetting the zoom should refetch the range"
    restored = state["requests"][-1]
    assert abs((restored["end"] - restored["start"]) - 604800) <= 5, "reset should restore the 7d window"
    assert abs(restored["end"] - time.time()) <= 5, "reset should restore the rolling window"
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_have_count(0)

    # The same has to hold for a double-click, the other reset gesture
    _rezoom_cm_chart(page, 60, 120)
    requests_after_zoom = len(state["requests"])
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.locator("#cm-combined-chart .u-over").dblclick()
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )
    assert len(state["requests"]) > requests_after_zoom, "a double-click should refetch the range"
    reset_again = state["requests"][-1]
    assert abs((reset_again["end"] - reset_again["start"]) - 604800) <= 5, "dblclick should restore the 7d window"
    expect(page.locator("#cm-combined-chart button", has_text="Reset Zoom")).to_have_count(0)


def test_connection_monitor_user_refetch_shows_a_loading_state_but_a_poll_does_not(demo_page):
    """A user-triggered refetch must be visibly loading; a background poll must not flash."""
    page = demo_page
    state = {}
    _stub_connection_monitor_api(page, state=state)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    _rezoom_cm_chart(page, 60, 120)
    overlay = page.locator("#cm-chart-loading")
    mount = page.locator("#cm-combined-chart")
    expect(overlay).to_be_hidden()

    # Reset Zoom refetches the whole range — three requests per target, which is
    # slow enough on a long range to look stuck without any feedback
    _hold_next_cm_samples_response(page, 3000)
    page.locator("#cm-combined-chart button", has_text="Reset Zoom").click()
    expect(overlay).to_be_visible()
    expect(mount).to_have_attribute("aria-busy", "true")

    # ...and it clears again once that batch has actually rendered
    expect(overlay).to_be_hidden(timeout=15000)
    expect(mount).to_have_attribute("aria-busy", "false")

    # The 10s poll must leave the chart alone, even while its response is held back
    page.evaluate(
        """() => {
            window._cmLoadingShown = 0;
            const el = document.getElementById('cm-chart-loading');
            new MutationObserver(() => { if (!el.hidden) window._cmLoadingShown++; })
                .observe(el, { attributes: true, attributeFilter: ['hidden'] });
        }"""
    )
    _hold_next_cm_samples_response(page, 2000)
    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale",
        timeout=25000,
    )
    assert page.evaluate("() => window._cmLoadingShown") == 0, (
        "a background poll must not flash the loading state"
    )


def test_connection_monitor_sub_minute_zoom_labels_the_axis_with_seconds(demo_page):
    """A zoom window shorter than a minute must not print the same hh:mm on every tick."""
    page = demo_page
    _stub_connection_monitor_api(page, interval=5)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    full = _cm_x_axis_labels(page)
    assert all(re.fullmatch(r"\d\d:\d\d", label) for label in full), full

    _rezoom_cm_chart(page, 100, 108)  # ~40 s of samples
    zoomed = _cm_x_axis_labels(page)
    assert len(zoomed) > 1, zoomed
    assert all(re.fullmatch(r"\d\d:\d\d:\d\d", label) for label in zoomed), zoomed
    assert len(set(zoomed)) == len(zoomed), "every tick of a sub-minute window must read differently"


def test_connection_monitor_zoom_window_header_names_the_zoomed_window(demo_page):
    """A zoom drops the date from the axis, so the window itself has to be labelled."""
    page = demo_page
    _stub_connection_monitor_api(page, interval=5)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 86400)

    header = page.locator("#cm-zoom-window")
    expect(header).to_be_hidden()

    _rezoom_cm_chart(page, 100, 108)
    expect(header).to_be_visible()
    # The end bound repeats the date only when the window straddles midnight
    assert re.fullmatch(
        r"\d\d\.\d\d\. \d\d:\d\d \u2013 (\d\d\.\d\d\. )?\d\d:\d\d", header.inner_text().strip()
    ), header.inner_text()

    page.evaluate("() => { window.charts['cm-combined-chart']._e2eStale = true; }")
    page.locator("#cm-combined-chart button", has_text="Reset Zoom").click()
    page.wait_for_function(
        "() => window.charts['cm-combined-chart'] && !window.charts['cm-combined-chart']._e2eStale"
    )
    expect(header).to_be_hidden()


def _capture_export_links(page):
    """Record the export download URLs instead of letting the browser fetch them."""
    page.evaluate(
        """() => {
            window.__cmExportUrls = [];
            document.addEventListener('click', (e) => {
                const a = e.target.closest && e.target.closest('a[download]');
                if (a) { window.__cmExportUrls.push(a.getAttribute('href')); e.preventDefault(); }
            }, true);
        }"""
    )


def test_connection_monitor_csv_export_asks_for_the_served_tier(demo_page):
    """A window served from the 1-min tier must not export resolution=raw (an empty file)."""
    page = demo_page
    _stub_connection_monitor_api(page, meta={
        "resolution": "raw", "bucket_seconds": None, "blended": True,
        "mixed": False, "tiers_used": ["1min"],
    })
    _open_connection_monitor_chart(page)
    _capture_export_links(page)

    page.locator("#cm-export-links .cm-chip-btn").first.click()
    page.wait_for_function("() => window.__cmExportUrls.length === 1")

    params = parse_qs(urlparse(page.evaluate("() => window.__cmExportUrls[0]")).query)
    assert params["resolution"] == ["1min"], "export must request the tier the chart was served from"


def test_connection_monitor_csv_export_of_a_blended_window_asks_for_auto(demo_page):
    """The tiers are exclusive, so a mixed window can only be exported tier by tier."""
    page = demo_page
    _stub_connection_monitor_api(page, meta={
        "resolution": "1min", "bucket_seconds": 60, "blended": True,
        "mixed": True, "tiers_used": ["raw", "1min"],
    })
    _open_connection_monitor_chart(page)
    _capture_export_links(page)

    page.locator("#cm-export-links .cm-chip-btn").first.click()
    page.wait_for_function("() => window.__cmExportUrls.length === 1")

    params = parse_qs(urlparse(page.evaluate("() => window.__cmExportUrls[0]")).query)
    assert params["resolution"] == ["auto"]


def test_connection_monitor_csv_export_follows_each_targets_own_tier(demo_page):
    """tiers_used is per target, so one target's tier must not decide another's export."""
    page = demo_page
    _stub_connection_monitor_api(page, target_metas=[
        {"resolution": "raw", "bucket_seconds": None, "blended": True,
         "mixed": False, "tiers_used": ["1min"]},
        {"resolution": "raw", "bucket_seconds": None, "blended": True,
         "mixed": False, "tiers_used": ["raw"]},
    ])
    _open_connection_monitor_chart(page)
    _capture_export_links(page)

    buttons = page.locator("#cm-export-links .cm-chip-btn")
    expect(buttons).to_have_count(2)
    buttons.nth(0).click()
    buttons.nth(1).click()
    page.wait_for_function("() => window.__cmExportUrls.length === 2")

    exported = page.evaluate("() => window.__cmExportUrls")
    resolutions = [parse_qs(urlparse(url).query)["resolution"][0] for url in exported]
    assert resolutions == ["1min", "raw"], "each target must export the tier it was served"


def test_connection_monitor_pinned_day_indicator_reports_the_served_tier(demo_page):
    """A zoom inside a pinned day drops resolution=raw, so the label must drop it too."""
    page = demo_page
    now = int(time.time())
    pinned_date = time.strftime("%Y-%m-%d", time.localtime(now))

    def served_meta(url):
        # The pinned day itself is read raw; a zoom inside it refetches by data
        # age, which past the raw retention window yields stored aggregates
        if "resolution=raw" in url:
            return {"resolution": "raw", "bucket_seconds": None, "blended": False,
                    "mixed": False, "tiers_used": ["raw"]}
        return {"resolution": "raw", "bucket_seconds": 60, "blended": True,
                "mixed": False, "tiers_used": ["1min"]}

    _stub_connection_monitor_api(
        page,
        pinned_days=[{"date": pinned_date, "label": "",
                      "utc_start": now - 86400, "utc_end": now + 60}],
        meta=served_meta,
    )
    _open_connection_monitor_chart(page)

    page.locator("#cm-pinned-days .cm-chip-btn").first.click()
    indicator = page.locator("#cm-resolution-indicator")
    expect(indicator).to_have_text(f"Pinned: {pinned_date} (Raw samples)")

    _rezoom_cm_chart(page, 60, 120)
    expect(indicator).to_have_text(f"Pinned: {pinned_date} (1-min averages)")


def test_connection_monitor_stats_mark_bucket_derived_targets(demo_page):
    """Stats served from aggregated buckets must be marked approximate, per target."""
    page = demo_page
    raw_meta = {"resolution": "raw", "bucket_seconds": None, "blended": False,
                "mixed": False, "tiers_used": ["raw"]}
    _stub_connection_monitor_api(
        page,
        target_metas=[raw_meta, raw_meta],
        stats={
            "1": {"sample_count": 100, "latency_count": 100, "min_latency_ms": 10.0,
                  "max_latency_ms": 90.0, "avg_latency_ms": 40.0, "p95_latency_ms": 80.0,
                  "packet_loss_pct": 0.0, "tiers_used": ["1hr"]},
            "2": {"sample_count": 100, "latency_count": 100, "min_latency_ms": 12.0,
                  "max_latency_ms": 92.0, "avg_latency_ms": 42.0, "p95_latency_ms": 82.0,
                  "packet_loss_pct": 0.0, "tiers_used": ["raw"]},
        },
    )
    _open_connection_monitor_chart(page)

    # The combined cards blend both targets, so the bucket-derived one taints them
    marked_cards = page.locator("#cm-stats-cards .cm-kpi-card:has(.cm-approx) .cm-kpi-label")
    expect(marked_cards).to_have_text(["Avg Latency", "P95"])
    assert "biased high" in page.locator(
        "#cm-stats-cards .cm-approx"
    ).first.get_attribute("title").lower()

    rows = page.locator("#cm-per-target-stats tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".cm-approx")).to_have_count(2)
    expect(rows.nth(1).locator(".cm-approx")).to_have_count(0)


def test_connection_monitor_outage_rows_mark_bucket_derived_windows(demo_page):
    """A bucket-derived outage is minute-snapped, so its row carries the marker."""
    page = demo_page
    now = int(time.time())
    _stub_connection_monitor_api(
        page,
        outages=[
            {"start": now - 3600, "end": now - 3300, "duration_seconds": 300.0,
             "timeout_count": 60, "approximate": True},
            {"start": now - 900, "end": now - 880, "duration_seconds": 20.0,
             "timeout_count": 20},
        ],
    )
    _open_connection_monitor_chart(page)

    rows = page.locator("#cm-outage-tbody tr")
    expect(rows).to_have_count(2)
    # Newest first: the exact raw outage leads, the bucket-derived one follows
    expect(rows.nth(0).locator(".cm-approx")).to_have_count(0)
    marker = rows.nth(1).locator("td").nth(1).locator(".cm-approx")
    expect(marker).to_have_count(1)
    assert "minute-snapped" in marker.get_attribute("title").lower()
