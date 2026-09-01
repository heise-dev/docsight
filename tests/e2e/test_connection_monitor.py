"""E2E coverage for Connection Monitor workflows."""

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


def _stub_connection_monitor_api(page, sample_count=240, outlier_index=5, pinned_days=None, state=None):
    """Serve deterministic Connection Monitor data: one target, one latency outlier.

    Samples are served only inside the requested window, so a fetch that narrows
    the window visibly narrows the chart. Pass ``state`` to collect the sample
    timestamps and every requested window.
    """
    now = int(time.time())
    samples = [
        {
            "timestamp": now - (sample_count - i) * 60,
            "latency_ms": 1000.0 if i == outlier_index else 50.0 + (i % 5),
            "packet_loss_pct": 0,
            "sample_count": 1,
            "timeout_count": 0,
        }
        for i in range(sample_count)
    ]
    if state is not None:
        state["timestamps"] = [s["timestamp"] for s in samples]
        state["requests"] = []

    def handler(route):
        url = route.request.url
        if "/pinned-days" in url:
            route.fulfill(json=pinned_days or [])
        elif "/targets" in url:
            route.fulfill(json=[{"id": 1, "label": "Router", "host": "192.168.1.1", "enabled": True}])
        elif "/samples/" in url:
            window = _requested_window(url)
            served = samples
            if window is not None:
                served = [s for s in samples if window[0] <= s["timestamp"] <= window[1]]
            if state is not None:
                state["requests"].append({"start": window[0], "end": window[1], "url": url})
            route.fulfill(json={"meta": {"resolution": "raw"}, "samples": served})
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

    preview = _rezoom_cm_chart(page, sample_count // 2, sample_count - 1)
    assert preview["yMax"] < 100, "the drag should pick a low y window immediately, before the refetch"

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


def test_connection_monitor_hidden_source_survives_data_refresh(demo_page):
    """Hiding a source via the chart legend must persist across the live data refresh."""
    page = demo_page
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")
    page.wait_for_selector("#cm-combined-chart .u-legend tr.u-series", state="visible")

    legend_rows = page.locator("#cm-combined-chart .u-legend tr.u-series")
    assert legend_rows.count() >= 2, "combined chart should plot at least two monitored targets"
    hidden_row = legend_rows.nth(1)
    hidden_label = hidden_row.inner_text().strip()

    hidden_row.locator("th").click()
    expect(hidden_row).to_have_class("u-series u-off")

    # Mark the live instance so we can wait for the refresh to rebuild the chart
    page.evaluate("charts['cm-combined-chart']._e2eStale = true")
    page.locator("#view-connection-monitor [data-cm-range='3600']").click()
    page.wait_for_function("() => charts['cm-combined-chart'] && !charts['cm-combined-chart']._e2eStale")

    refreshed_rows = page.locator("#cm-combined-chart .u-legend tr.u-series")
    expect(refreshed_rows.nth(1)).to_have_text(hidden_label)
    expect(refreshed_rows.nth(1)).to_have_class("u-series u-off")
    expect(refreshed_rows.nth(0)).to_have_class("u-series")


def test_connection_monitor_hidden_source_stays_hidden_in_zoom_modal(demo_page):
    """A source hidden via the chart legend must stay hidden in the fullscreen zoom modal."""
    page = demo_page
    page.evaluate("switchView('connection-monitor')")
    page.wait_for_selector("#view-connection-monitor.active", state="visible")
    page.wait_for_selector("#cm-combined-chart .u-legend tr.u-series", state="visible")

    legend_rows = page.locator("#cm-combined-chart .u-legend tr.u-series")
    assert legend_rows.count() >= 2, "combined chart should plot at least two monitored targets"
    hidden_row = legend_rows.nth(1)
    hidden_label = hidden_row.inner_text().strip()

    hidden_row.locator("th").click()
    expect(hidden_row).to_have_class("u-series u-off")

    page.evaluate("openChartZoom('cm-combined-chart')")
    page.wait_for_selector("#chart-zoom-canvas .u-legend tr.u-series", state="visible")

    zoom_rows = page.locator("#chart-zoom-canvas .u-legend tr.u-series")
    expect(zoom_rows.nth(1)).to_have_text(hidden_label)
    expect(zoom_rows.nth(1)).to_have_class("u-series u-off")
    expect(zoom_rows.nth(0)).to_have_class("u-series")
    page.evaluate("closeChartZoom()")


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
