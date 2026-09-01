"""E2E coverage for Connection Monitor workflows."""

import time

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


def _stub_connection_monitor_api(page, sample_count=240, outlier_index=5):
    """Serve deterministic Connection Monitor data: one target, one latency outlier."""
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

    def handler(route):
        url = route.request.url
        if "/targets" in url:
            route.fulfill(json=[{"id": 1, "label": "Router", "host": "192.168.1.1", "enabled": True}])
        elif "/samples/" in url:
            route.fulfill(json={"meta": {"resolution": "raw"}, "samples": samples})
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
    """Drag-select the given index window (fires the plugin's setSelect hook)."""
    page.evaluate(
        """([i0, i1]) => {
            const u = window.charts['cm-combined-chart'];
            const left = u.valToPos(i0, 'x');
            const width = u.valToPos(i1, 'x') - left;
            u.setSelect({ left: left, width: width, top: 0, height: u.bbox.height }, true);
        }""",
        [first_index, last_index],
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

    _zoom_cm_chart(page, sample_count // 2, sample_count - 1)
    zoomed = _cm_scales(page)
    assert zoomed["yMax"] < 100, "zoomed y-axis should rescale to the visible ~50 ms samples"
    assert zoomed["yMax"] >= 54, "zoomed y-axis must still cover the visible data"
    assert zoomed["yMin"] <= 50, "zoomed y-axis must still cover the visible data"

    page.locator("#cm-combined-chart button", has_text="Reset Zoom").click()
    reset = _cm_scales(page)
    assert reset["yMax"] == full["yMax"], "resetting the zoom should restore the full y range"


def test_connection_monitor_switching_range_clears_zoom(demo_page):
    """Selecting another timespan must not inherit the previous range's zoom window."""
    page = demo_page
    sample_count = _stub_connection_monitor_api(page)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 604800)

    _zoom_cm_chart(page, sample_count // 2, sample_count - 1)
    assert _cm_scales(page)["zoom"] is not None

    _reload_cm_range(page, 86400)
    after = _cm_scales(page)
    assert after["zoom"] is None, "switching the timespan should clear the zoom state"
    assert after["xMin"] <= 0, "new timespan should show the full x range"
    assert after["xMax"] >= after["points"] - 1, "new timespan should show the full x range"


def test_connection_monitor_zoom_and_reset_button_survive_same_range_refresh(demo_page):
    """A refresh of the same timespan keeps the zoom — including its y window and reset button."""
    page = demo_page
    sample_count = _stub_connection_monitor_api(page)
    _open_connection_monitor_chart(page)
    _reload_cm_range(page, 604800)

    _zoom_cm_chart(page, sample_count // 2, sample_count - 1)
    zoomed = _cm_scales(page)
    assert zoomed["yMax"] < 100, "zoom should have rescaled the y-axis before the refresh"

    _reload_cm_range(page, 604800)
    refreshed = _cm_scales(page)
    assert refreshed["zoom"] is not None, "same-range refresh should keep the zoom"
    assert refreshed["yMax"] == zoomed["yMax"], "same-range refresh should keep the zoomed y window"
    assert refreshed["xMax"] == zoomed["xMax"], "same-range refresh should keep the zoomed x window"
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
