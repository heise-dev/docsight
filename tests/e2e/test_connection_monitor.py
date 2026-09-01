"""E2E coverage for Connection Monitor workflows."""

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
