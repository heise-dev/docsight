/**
 * Connection Monitor Detail View - PingPlotter-style
 * Fetches all targets in parallel, renders combined chart with zones + loss markers.
 */
/* global CMCharts */
(function() {
    'use strict';

    var currentRange = 3600; // 1h default
    var targets = [];
    var refreshTimer = null;
    var lastResolution = 'raw';
    var pinnedDayView = null; // { date: 'YYYY-MM-DD', start: epoch, end: epoch } when viewing a pinned day
    var zoomWindow = null; // { start, end, span, followLive } when the chart is zoomed into a sub-window
    var loadSeq = 0; // guards against a slow response overwriting a newer one

    function updateRefreshInterval() {
        if (refreshTimer) clearInterval(refreshTimer);
        if (pinnedDayView) return; // no auto-refresh for pinned day views
        // A fixed zoom window is a slice of the past — its data cannot change, so
        // stop refetching it. This only stops the browser poll; ping collection
        // keeps running server-side in the collector either way.
        if (zoomWindow && !zoomWindow.followLive) return;
        var interval = currentRange <= 86400 ? 10000 : 60000;
        refreshTimer = setInterval(function() {
            var view = document.getElementById('cm-detail-view');
            if (view && view.closest('.view.active')) {
                loadData();
            }
        }, interval);
    }

    // A user-initiated refetch (zoom, reset, range or pinned-day switch) keeps the
    // old chart on screen until three requests per target resolve, which on a long
    // range looks stuck. Background polls never show it — a periodic refresh must
    // not flash the UI.
    function setChartLoading(busy) {
        var mount = document.getElementById('cm-combined-chart');
        var overlay = document.getElementById('cm-chart-loading');
        if (mount) mount.setAttribute('aria-busy', busy ? 'true' : 'false');
        if (overlay) overlay.hidden = !busy;
    }

    function getMaxPointsForRange(seconds) {
        return seconds >= 86400 ? 1440 : 0;
    }

    window.cmSetRange = function(btn, seconds) {
        pinnedDayView = null;
        zoomWindow = null;
        currentRange = seconds;
        document.querySelectorAll('[data-cm-range]').forEach(function(b) {
            b.classList.remove('active');
        });
        btn.classList.add('active');
        updatePinButton();
        loadData(true);
        updateRefreshInterval();
    };

    // Absolute bounds of the zoom window. A zoom taken at the live edge keeps its
    // span but slides with now, so new pings keep arriving in view.
    function getZoomWindow() {
        if (!zoomWindow.followLive) {
            return { start: zoomWindow.start, end: zoomWindow.end };
        }
        var now = Date.now() / 1000;
        return { start: now - zoomWindow.span, end: now };
    }

    // Called by the chart's zoom plugin: the dragged selection becomes the window
    // that every following fetch asks for, so the view stops drifting with the poll
    // and the refetch returns the best resolution stored for that age.
    window.cmSetZoomWindow = function(start, end, atLastSample) {
        if (start == null || end == null || !(end > start)) return;
        // Only a selection that reaches the newest sample AND actually sits at the
        // live edge follows now; a pinned day or a window into the past stays put.
        // The tolerance never drops below 60s: a narrow selection can be shorter
        // than the age of the newest sample, and misreading it as historical would
        // freeze the chart with the refresh timer stopped.
        var followLive = !!atLastSample && !pinnedDayView &&
            (Date.now() / 1000) - end <= Math.max(end - start, 60);
        zoomWindow = { start: start, end: end, span: end - start, followLive: followLive };
        loadData(true);
        updateRefreshInterval();
    };

    window.cmClearZoomWindow = function() {
        if (!zoomWindow) return;
        zoomWindow = null;
        loadData(true);
        updateRefreshInterval();
    };

    window.cmHasZoomWindow = function() {
        return zoomWindow !== null;
    };

    function getExportWindow() {
        if (zoomWindow) {
            return getZoomWindow();
        }
        if (pinnedDayView) {
            return { start: pinnedDayView.start, end: pinnedDayView.end };
        }
        var now = Date.now() / 1000;
        return { start: now - currentRange, end: now };
    }

    function triggerExport(url) {
        var a = document.createElement('a');
        a.href = url;
        a.download = '';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    window.cmExportCsv = function(targetId) {
        var range = getExportWindow();
        triggerExport(docsightUrl('/api/connection-monitor/export/' + targetId + '?start=' + range.start + '&end=' + range.end + '&resolution=' + lastResolution));
    };

    window.cmExportRawLog = function(targetId) {
        var range = getExportWindow();
        triggerExport(docsightUrl('/api/connection-monitor/export/' + targetId + '?start=' + range.start + '&end=' + range.end + '&resolution=raw&format=pinglog'));
    };

    // --- Pin Button ---

    function pinCurrentDay() {
        var ts = Math.floor(Date.now() / 1000);
        fetch(docsightUrl('/api/connection-monitor/pinned-days'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ timestamp: ts })
        }).then(function() {
            loadPinnedDays();
        });
    }

    function updatePinButton() {
        var btn = document.getElementById('cm-pin-day-btn');
        var bar = document.getElementById('cm-pinned-days-bar');
        var daysContainer = document.getElementById('cm-pinned-days');
        var label = document.getElementById('cm-pinned-label');
        if (!btn || !bar || !daysContainer) return;

        var showPinAction = currentRange === 86400 && !pinnedDayView;
        var hasPinnedDays = daysContainer.children.length > 0;

        btn.hidden = !showPinAction;
        if (label) label.hidden = !hasPinnedDays;
        bar.hidden = !(showPinAction || hasPinnedDays);
    }

    // --- Pinned Days Bar ---

    function loadPinnedDays() {
        fetch(docsightUrl('/api/connection-monitor/pinned-days'))
            .then(function(r) { return r.json(); })
            .then(function(days) {
                renderPinnedDays(days);
            })
            .catch(function() {});
    }

    function renderPinnedDays(days) {
        var bar = document.getElementById('cm-pinned-days-bar');
        var container = document.getElementById('cm-pinned-days');
        if (!bar || !container) return;

        container.textContent = '';
        if (days.length === 0) {
            updatePinButton();
            return;
        }

        days.forEach(function(day) {
            var chip = document.createElement('span');
            chip.className = 'cm-pinned-chip';

            var viewBtn = document.createElement('button');
            viewBtn.type = 'button';
            viewBtn.className = 'cm-chip-btn';
            viewBtn.setAttribute('aria-pressed', pinnedDayView && pinnedDayView.date === day.date ? 'true' : 'false');
            if (pinnedDayView && pinnedDayView.date === day.date) {
                viewBtn.classList.add('active');
            }

            var label = day.label ? day.date + ' (' + day.label + ')' : day.date;
            var textSpan = document.createElement('span');
            textSpan.textContent = label;
            viewBtn.appendChild(textSpan);
            viewBtn.onclick = function() {
                viewPinnedDay(day.date, day.utc_start, day.utc_end);
            };

            var removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.textContent = '\u00d7';
            removeBtn.className = 'cm-chip-remove';
            var removeLabelRoot = document.getElementById('cm-detail-view');
            var removeLabel = removeLabelRoot ? (removeLabelRoot.dataset.lRemove || 'Remove') : 'Remove';
            removeBtn.setAttribute('aria-label', removeLabel + ' ' + label);
            removeBtn.onclick = function(e) {
                e.stopPropagation();
                var removedActiveDay = pinnedDayView && pinnedDayView.date === day.date;
                fetch(docsightUrl('/api/connection-monitor/pinned-days/' + day.date), { method: 'DELETE' })
                    .then(function() {
                        if (removedActiveDay) {
                            pinnedDayView = null;
                            zoomWindow = null;
                            document.querySelectorAll('[data-cm-range]').forEach(function(b) {
                                b.classList.toggle('active', Number(b.dataset.cmRange) === currentRange);
                            });
                            updatePinButton();
                            loadData(true);
                            updateRefreshInterval();
                        }
                        loadPinnedDays();
                    });
            };

            chip.appendChild(viewBtn);
            chip.appendChild(removeBtn);
            container.appendChild(chip);
        });
        updatePinButton();
    }

    function viewPinnedDay(dateStr, utcStart, utcEnd) {
        zoomWindow = null;
        pinnedDayView = {
            date: dateStr,
            start: utcStart,
            end: utcEnd
        };

        // Deactivate range buttons
        document.querySelectorAll('[data-cm-range]').forEach(function(b) {
            b.classList.remove('active');
        });
        updatePinButton();

        if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }

        loadData(true);
        loadPinnedDays(); // refresh to highlight active
    }

    function init() {
        var pinBtn = document.getElementById('cm-pin-day-btn');
        if (pinBtn) {
            pinBtn.onclick = pinCurrentDay;
        }

        fetch(docsightUrl('/api/connection-monitor/capability'))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var el = document.getElementById('cm-capability-info');
                if (!el) return;
                var isTcp = data.method === 'tcp';
                var label = isTcp
                    ? (el.dataset.methodTcp || 'TCP')
                    : (el.dataset.methodIcmp || 'ICMP');

                // Badge
                var badge = document.createElement('span');
                badge.className = 'cm-mode-badge ' + (isTcp ? 'tcp' : 'icmp');
                badge.textContent = label + ' mode';
                el.appendChild(badge);

                // Glossary hint with popover (only for TCP)
                if (isTcp && el.dataset.hintTcp) {
                    var hint = document.createElement('span');
                    hint.className = 'glossary-hint';
                    var icon = document.createElement('i');
                    icon.setAttribute('data-lucide', 'info');
                    var popover = document.createElement('div');
                    popover.className = 'glossary-popover';
                    popover.textContent = el.dataset.hintTcp;
                    hint.appendChild(icon);
                    hint.appendChild(popover);
                    el.appendChild(hint);
                    if (window.lucide) lucide.createIcons();
                }
            })
            .catch(function() {});

        loadTargets();
        loadPinnedDays();

        updateRefreshInterval();
    }

    function loadTargets() {
        fetch(docsightUrl('/api/connection-monitor/targets'))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                targets = data.filter(function(t) { return t.enabled; });
                if (targets.length > 0) {
                    loadData();
                    updatePinButton();
                    initTracerouteTargetSelect();
                } else {
                    showNoData();
                }
            })
            .catch(function() {});
    }

    function loadData(userInitiated) {
        if (targets.length === 0) { showNoData(); return; }

        var seq = ++loadSeq;
        if (userInitiated) setChartLoading(true);
        var now = Date.now() / 1000;
        var start, end;
        if (zoomWindow) {
            var win = getZoomWindow();
            start = win.start;
            end = win.end;
        } else if (pinnedDayView) {
            start = pinnedDayView.start;
            end = pinnedDayView.end;
        } else {
            start = now - currentRange;
            end = now;
        }
        var maxPoints;
        if (zoomWindow) {
            // Always cap a zoom window: below 24h getMaxPointsForRange asks for
            // everything, which for a sub-day window of raw pings is far heavier
            // than the capped range it was zoomed out of. The chart is never wider
            // than ~1440px and the server leaves anything under the cap untouched.
            maxPoints = Math.max(getMaxPointsForRange(end - start), 1440);
        } else {
            maxPoints = pinnedDayView ? 0 : getMaxPointsForRange(currentRange);
        }

        // Fetch samples for ALL targets in parallel
        var samplePromises = targets.map(function(t) {
            var url = docsightUrl('/api/connection-monitor/samples/' + t.id + '?start=' + start + '&end=' + end + '&limit=0');
            // A zoom window keeps the default auto resolution: it is served by data
            // age, so an old window still yields the stored aggregates (raw would
            // return nothing past the raw retention window).
            if (pinnedDayView && !zoomWindow) {
                url += '&resolution=raw';
            } else if (maxPoints > 0) {
                url += '&max_points=' + maxPoints;
            }
            return fetch(url)
                .then(function(r) { return r.json(); })
                .then(function(data) { return { target: t, samples: data.samples, meta: data.meta }; });
        });

        var statsPromise = fetch(docsightUrl('/api/connection-monitor/stats?start=' + start + '&end=' + end))
            .then(function(r) { return r.json(); });

        // Fetch outages for ALL targets in parallel
        var outagePromises = targets.map(function(t) {
            return fetch(docsightUrl('/api/connection-monitor/outages/' + t.id + '?start=' + start + '&end=' + end))
                .then(function(r) { return r.json(); })
                .then(function(outages) { return { target: t, outages: outages }; });
        });

        Promise.all([Promise.all(samplePromises), Promise.all(outagePromises), statsPromise])
            .then(function(results) {
                // A second zoom can be dragged while this one is still in flight;
                // the newest load owns the view, so drop anything overtaken
                if (seq !== loadSeq) return;
                var allTargetData = results[0];
                var allOutageData = results[1];
                var statsByTarget = results[2] || {};

                allTargetData.forEach(function(td) {
                    td.stats = statsByTarget[String(td.target.id)] || statsByTarget[td.target.id] || null;
                });

                var hasSamples = allTargetData.some(function(td) {
                    return td.samples && td.samples.length > 0;
                });
                if (!hasSamples) {
                    if (zoomWindow) {
                        // Nothing stored in the zoomed window — drop back to the
                        // parent range instead of leaving a blank chart behind.
                        // The follow-up load keeps the loading state up.
                        zoomWindow = null;
                        loadData(userInitiated);
                        updateRefreshInterval();
                        return;
                    }
                    setChartLoading(false);
                    showNoData();
                    return;
                }

                var meta = allTargetData.length > 0 ? allTargetData[0].meta : null;
                if (meta && meta.resolution) lastResolution = meta.resolution;
                hideNoData();
                CMCharts.renderStatsCards('cm-stats-cards', allTargetData);
                CMCharts.renderPerTargetStats('cm-per-target-stats', allTargetData);
                var chartRange = zoomWindow ? Math.max(Math.round(end - start), 1)
                    : (pinnedDayView ? 86400 : currentRange);
                // A pinned day shares the 1d range but is fetched at raw resolution, so
                // fold its date into the x-domain key to keep the zoom windows separate.
                // A zoom window is its own domain, keyed by its absolute bounds.
                var chartDomainKey = null;
                if (zoomWindow) {
                    chartDomainKey = 'zoom@' + start + '-' + end;
                } else if (pinnedDayView) {
                    chartDomainKey = '86400s@' + pinnedDayView.date;
                }
                CMCharts.renderCombinedChart('cm-combined-chart', allTargetData, chartRange, chartDomainKey);
                CMCharts.renderAvailabilityBand('cm-availability', allTargetData);
                renderOutages(allOutageData);
                renderExportLinks();
                renderRawLogLinks();
                renderResolutionIndicator(meta);
                renderZoomWindowLabel(start, end);
                setChartLoading(false);
            })
            .catch(function() {
                // Only the current batch clears the state: an overtaken response
                // must not uncover a newer pending one, and a failed fetch must
                // not leave the spinner up forever.
                if (seq === loadSeq) setChartLoading(false);
            });
    }

    function renderExportLinks() {
        var container = document.getElementById('cm-export-links');
        if (!container) return;
        container.textContent = '';
        targets.forEach(function(t) {
            var btn = document.createElement('button');
            btn.className = 'cm-chip-btn';
            btn.textContent = t.label;
            btn.onclick = function() { window.cmExportCsv(t.id); };
            container.appendChild(btn);
        });
    }

    function renderRawLogLinks() {
        var container = document.getElementById('cm-raw-log-links');
        if (!container) return;
        var root = document.getElementById('cm-detail-view');
        var downloadLabel = root ? (root.dataset.lRawLogDownload || 'Download raw log') : 'Download raw log';
        container.textContent = '';
        targets.forEach(function(t) {
            var btn = document.createElement('button');
            btn.className = 'cm-chip-btn';
            btn.textContent = t.label;
            btn.setAttribute('aria-label', downloadLabel + ': ' + t.label);
            btn.onclick = function() { window.cmExportRawLog(t.id); };
            container.appendChild(btn);
        });
    }

    function renderOutages(allOutageData) {
        var tbody = document.getElementById('cm-outage-tbody');
        if (!tbody) return;

        // Flatten all outages, then group overlapping ones across targets
        var flat = [];
        allOutageData.forEach(function(od) {
            if (!od.outages) return;
            od.outages.forEach(function(o) {
                flat.push({ target: od.target, outage: o });
            });
        });
        flat.sort(function(a, b) { return (a.outage.start || 0) - (b.outage.start || 0); });

        // Merge outages that overlap in time (same root cause across targets)
        var grouped = [];
        flat.forEach(function(item) {
            var o = item.outage;
            var merged = false;
            for (var i = grouped.length - 1; i >= 0; i--) {
                var g = grouped[i];
                // Overlap if starts are within 60s of each other
                if (Math.abs((g.start || 0) - (o.start || 0)) < 60) {
                    if (g.targets.indexOf(item.target.label) === -1) {
                        g.targets.push(item.target.label);
                    }
                    // Use widest window
                    if (o.end && g.end && o.end > g.end) g.end = o.end;
                    if (!o.end) g.end = null;
                    g.duration = Math.max(g.duration || 0, o.duration_seconds || 0);
                    merged = true;
                    break;
                }
            }
            if (!merged) {
                grouped.push({
                    targets: [item.target.label],
                    start: o.start,
                    end: o.end,
                    duration: o.duration_seconds || 0,
                    ongoing: !o.end
                });
            }
        });
        grouped.sort(function(a, b) { return (b.start || 0) - (a.start || 0); });

        tbody.textContent = '';

        if (grouped.length === 0) {
            var emptyRow = document.createElement('tr');
            var emptyCell = document.createElement('td');
            emptyCell.colSpan = 4;
            emptyCell.style.cssText = 'text-align:center;color:var(--text-muted);padding:20px;';
            emptyCell.textContent = '\u2014';
            emptyRow.appendChild(emptyCell);
            tbody.appendChild(emptyRow);
            return;
        }

        grouped.forEach(function(g) {
            var tr = document.createElement('tr');

            var tdTarget = document.createElement('td');
            tdTarget.textContent = g.targets.join(', ');

            var tdStart = document.createElement('td');
            tdStart.textContent = new Date(g.start * 1000).toLocaleString();

            var tdEnd = document.createElement('td');
            if (g.ongoing) {
                var span = document.createElement('span');
                span.style.color = 'var(--crit)';
                span.textContent = 'Ongoing';
                tdEnd.appendChild(span);
            } else if (g.end) {
                tdEnd.textContent = new Date(g.end * 1000).toLocaleString();
            }

            var tdDur = document.createElement('td');
            tdDur.style.textAlign = 'right';
            tdDur.textContent = g.duration ? formatDuration(g.duration) : '\u2014';

            tr.appendChild(tdTarget);
            tr.appendChild(tdStart);
            tr.appendChild(tdEnd);
            tr.appendChild(tdDur);
            tbody.appendChild(tr);
        });
    }

    function formatDuration(seconds) {
        if (seconds < 60) return seconds + 's';
        if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's';
        var h = Math.floor(seconds / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        return h + 'h ' + m + 'm';
    }

    function renderResolutionIndicator(meta) {
        var el = document.getElementById('cm-resolution-indicator');
        if (!el || !meta) return;
        var labels = {
            'raw': el.dataset.labelRaw || 'Raw samples',
            '1min': el.dataset.label1min || '1-min averages',
            '5min': el.dataset.label5min || '5-min averages',
            '1hr': el.dataset.label1hr || '1-hour averages'
        };
        if (pinnedDayView) {
            el.textContent = 'Pinned: ' + pinnedDayView.date + ' (full resolution)';
            el.style.display = 'block';
            return;
        }
        // tiers_used names what was actually served; meta.resolution is derived from
        // the window span, which mislabels a short window over older, aggregated data
        if (Array.isArray(meta.tiers_used) && meta.tiers_used.length > 0) {
            el.textContent = meta.tiers_used.map(function(tier) {
                return labels[tier] || tier;
            }).join(' + ');
        } else {
            el.textContent = labels[meta.resolution] || meta.resolution;
        }
        el.style.display = 'block';
    }

    function formatZoomBound(ts, withDate) {
        var d = new Date(ts * 1000);
        function pad(n) { return String(n).padStart(2, '0'); }
        var hhmm = pad(d.getHours()) + ':' + pad(d.getMinutes());
        return withDate ? pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '. ' + hhmm : hhmm;
    }

    // The x axis of a zoom window only carries clock times, so a 3-minute slice of
    // last week looks exactly like one from today — name the window above the chart.
    // Takes the bounds the fetch actually used: a followLive window moves with now,
    // so asking getZoomWindow() again could label the chart ahead of its own data.
    function renderZoomWindowLabel(start, end) {
        var el = document.getElementById('cm-zoom-window');
        if (!el) return;
        if (!zoomWindow) {
            el.textContent = '';
            el.style.display = 'none';
            return;
        }
        var sameDay = new Date(start * 1000).toDateString() === new Date(end * 1000).toDateString();
        el.textContent = formatZoomBound(start, true) + ' – ' + formatZoomBound(end, !sameDay);
        el.style.display = 'block';
    }

    function showNoData() {
        var noData = document.getElementById('cm-no-data');
        var chartsEl = document.getElementById('cm-charts-section');
        var statsEl = document.getElementById('cm-stats-cards');
        var perTargetEl = document.getElementById('cm-per-target-stats');
        var availabilityEl = document.getElementById('cm-availability');
        var outagePanel = document.getElementById('cm-outage-panel');
        var outageBody = document.getElementById('cm-outage-tbody');
        var exportLinks = document.getElementById('cm-export-links');
        var rawLogLinks = document.getElementById('cm-raw-log-links');
        var rawLogPanel = document.getElementById('cm-raw-log-panel');
        var resolutionEl = document.getElementById('cm-resolution-indicator');
        var zoomWindowEl = document.getElementById('cm-zoom-window');
        if (noData) noData.style.display = 'flex';
        if (chartsEl) chartsEl.style.display = 'none';
        if (outagePanel) outagePanel.style.display = 'none';
        if (rawLogPanel) rawLogPanel.style.display = 'none';
        [statsEl, perTargetEl, outageBody, exportLinks, rawLogLinks, resolutionEl, zoomWindowEl].forEach(function(el) {
            if (el) el.textContent = '';
        });
        if (availabilityEl) {
            availabilityEl.textContent = '';
            availabilityEl.removeAttribute('role');
            availabilityEl.removeAttribute('aria-label');
        }
        if (resolutionEl) resolutionEl.style.display = 'none';
        if (zoomWindowEl) zoomWindowEl.style.display = 'none';
    }

    function hideNoData() {
        var noData = document.getElementById('cm-no-data');
        var chartsEl = document.getElementById('cm-charts-section');
        var outagePanel = document.getElementById('cm-outage-panel');
        var rawLogPanel = document.getElementById('cm-raw-log-panel');
        if (noData) noData.style.display = 'none';
        if (chartsEl) chartsEl.style.display = '';
        if (outagePanel) outagePanel.style.display = '';
        if (rawLogPanel) rawLogPanel.style.display = '';
    }

    // --- Traceroute ---

    var trSelectedTargetId = null;

    function initTracerouteTargetSelect() {
        var container = document.getElementById('cm-traceroute-target-select');
        if (!container || targets.length === 0) return;
        container.textContent = '';

        targets.forEach(function(t, i) {
            var btn = document.createElement('button');
            btn.className = 'cm-chip-btn' + (i === 0 ? ' active' : '');
            btn.textContent = t.label;
            btn.dataset.targetId = t.id;
            btn.onclick = function() {
                container.querySelectorAll('.cm-chip-btn').forEach(function(b) { b.classList.remove('active'); });
                btn.classList.add('active');
                trSelectedTargetId = t.id;
                loadTraceHistory();
            };
            container.appendChild(btn);
        });

        trSelectedTargetId = targets[0].id;
        loadTraceHistory();
    }

    function loadTraceHistory() {
        if (!trSelectedTargetId) return;
        fetch(docsightUrl('/api/connection-monitor/traces/' + trSelectedTargetId))
            .then(function(r) { return r.json(); })
            .then(function(traces) {
                renderTraceHistory(traces);
            })
            .catch(function() {});
    }

    function renderTraceHistory(traces) {
        var tbody = document.getElementById('cm-traceroute-tbody');
        var table = document.getElementById('cm-traceroute-table');
        var noTraces = document.getElementById('cm-traceroute-no-traces');
        if (!tbody || !table) return;

        tbody.textContent = '';

        if (!traces || traces.length === 0) {
            table.style.display = 'none';
            if (noTraces) noTraces.style.display = 'block';
            return;
        }

        table.style.display = '';
        if (noTraces) noTraces.style.display = 'none';

        // Find target label
        var targetLabel = '';
        targets.forEach(function(t) {
            if (t.id === trSelectedTargetId) targetLabel = t.label;
        });

        traces.forEach(function(trace) {
            var tr = document.createElement('tr');
            tr.className = 'cm-trace-row';

            var detailId = 'cm-trace-detail-' + String(trace.id).replace(/[^a-zA-Z0-9_-]/g, '-');
            var tdTarget = document.createElement('td');
            var toggleBtn = document.createElement('button');
            toggleBtn.type = 'button';
            toggleBtn.className = 'cm-trace-toggle';
            toggleBtn.textContent = targetLabel;
            toggleBtn.setAttribute('aria-expanded', 'false');
            toggleBtn.setAttribute('aria-controls', detailId);
            toggleBtn.onclick = function() { toggleTraceDetail(tr, trace.id, toggleBtn, detailId); };
            tdTarget.appendChild(toggleBtn);

            var tdTime = document.createElement('td');
            tdTime.textContent = new Date(trace.timestamp).toLocaleString();

            var tdHops = document.createElement('td');
            tdHops.textContent = trace.hop_count;

            var tdFp = document.createElement('td');
            tdFp.className = 'cm-fingerprint';
            tdFp.textContent = trace.route_fingerprint ? trace.route_fingerprint.substring(0, 12) : '\u2014';

            var tdReached = document.createElement('td');
            var reachedSpan = document.createElement('span');
            if (trace.reached_target) {
                reachedSpan.className = 'cm-reached-badge yes';
                reachedSpan.textContent = '\u2713';
            } else {
                reachedSpan.className = 'cm-reached-badge no';
                reachedSpan.textContent = '\u2717';
            }
            tdReached.appendChild(reachedSpan);

            var tdTrigger = document.createElement('td');
            tdTrigger.textContent = trace.trigger_reason || '\u2014';

            tr.appendChild(tdTarget);
            tr.appendChild(tdTime);
            tr.appendChild(tdHops);
            tr.appendChild(tdFp);
            tr.appendChild(tdReached);
            tr.appendChild(tdTrigger);
            tbody.appendChild(tr);
        });
    }

    function toggleTraceDetail(row, traceId, toggleButton, detailId) {
        var control = toggleButton || row.querySelector('.cm-trace-toggle');
        // If already expanded, collapse
        var existing = row.nextElementSibling;
        if (existing && existing.classList.contains('trace-detail-row')) {
            existing.remove();
            if (control) control.setAttribute('aria-expanded', 'false');
            return;
        }
        // Remove any other expanded detail row
        var table = row.closest('table');
        if (table) {
            table.querySelectorAll('.trace-detail-row').forEach(function(r) { r.remove(); });
            table.querySelectorAll('.cm-trace-toggle[aria-expanded="true"]').forEach(function(btn) {
                btn.setAttribute('aria-expanded', 'false');
            });
        }
        if (control) control.setAttribute('aria-expanded', 'true');

        // Fetch detail
        fetch(docsightUrl('/api/connection-monitor/trace/' + traceId))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var detailRow = document.createElement('tr');
                detailRow.className = 'trace-detail-row';
                if (detailId) detailRow.id = detailId;
                var td = document.createElement('td');
                td.colSpan = 6;

                var hops = data.hops || [];
                if (hops.length === 0) {
                    td.textContent = '\u2014';
                } else {
                    var hopTable = document.createElement('table');
                    hopTable.className = 'cm-hop-table';
                    var thead = document.createElement('thead');
                    var headRow = document.createElement('tr');
                    ['#', 'IP', 'Hostname', 'Latency', 'Probes'].forEach(function(label) {
                        var th = document.createElement('th');
                        th.textContent = label;
                        headRow.appendChild(th);
                    });
                    thead.appendChild(headRow);
                    hopTable.appendChild(thead);

                    var hopsBody = document.createElement('tbody');
                    hops.forEach(function(hop) {
                        var htr = document.createElement('tr');
                        var tdIdx = document.createElement('td');
                        tdIdx.textContent = hop.hop_index;

                        var tdIp = document.createElement('td');
                        tdIp.className = 'cm-hop-ip';
                        tdIp.textContent = hop.hop_ip || '*';

                        var tdHost = document.createElement('td');
                        tdHost.textContent = hop.hop_host || '\u2014';

                        var tdLat = document.createElement('td');
                        if (hop.latency_ms !== null && hop.latency_ms !== undefined) {
                            tdLat.textContent = hop.latency_ms.toFixed(2) + ' ms';
                        } else {
                            tdLat.textContent = '*';
                            tdLat.className = 'cm-muted';
                        }

                        var tdProbes = document.createElement('td');
                        tdProbes.textContent = hop.probes_responded !== undefined ? hop.probes_responded + '/3' : '\u2014';

                        htr.appendChild(tdIdx);
                        htr.appendChild(tdIp);
                        htr.appendChild(tdHost);
                        htr.appendChild(tdLat);
                        htr.appendChild(tdProbes);
                        hopsBody.appendChild(htr);
                    });
                    hopTable.appendChild(hopsBody);
                    td.appendChild(hopTable);
                }

                detailRow.appendChild(td);
                row.parentNode.insertBefore(detailRow, row.nextSibling);
            })
            .catch(function() {});
    }

    function renderTracerouteResult(data) {
        var resultDiv = document.getElementById('cm-traceroute-result');
        if (!resultDiv) return;
        resultDiv.textContent = '';

        if (data.error) {
            resultDiv.style.display = 'block';
            var errBox = document.createElement('div');
            errBox.className = 'cm-traceroute-alert error';
            errBox.textContent = data.error;
            resultDiv.appendChild(errBox);
            return;
        }

        resultDiv.style.display = 'block';
        var reached = data.reached_target;
        var statusText = reached ? 'Target reached' : 'Target not reached';

        var box = document.createElement('div');
        box.className = 'cm-traceroute-alert ' + (reached ? 'ok' : 'error');

        var statusSpan = document.createElement('span');
        statusSpan.textContent = statusText;
        box.appendChild(statusSpan);

        var infoText = document.createTextNode(' \u2014 ' + data.hop_count + ' hops');
        box.appendChild(infoText);

        if (data.route_fingerprint) {
            var fpText = document.createTextNode(', fingerprint: ');
            box.appendChild(fpText);
            var fpCode = document.createElement('code');
            fpCode.className = 'cm-fingerprint';
            fpCode.textContent = data.route_fingerprint.substring(0, 12);
            box.appendChild(fpCode);
        }

        resultDiv.appendChild(box);
        // Auto-hide after 8s
        setTimeout(function() { resultDiv.style.display = 'none'; }, 8000);
    }

    window.cmRunTraceroute = function() {
        if (!trSelectedTargetId) return;
        var btn = document.getElementById('cm-traceroute-btn');
        if (!btn) return;

        var label = btn.querySelector('span');
        var origText = label ? label.textContent : btn.textContent;
        btn.disabled = true;
        btn.classList.add('is-running');
        if (label) label.textContent = btn.dataset.running || 'Running traceroute...';
        else btn.textContent = btn.dataset.running || 'Running traceroute...';

        fetch(docsightUrl('/api/connection-monitor/traceroute/' + trSelectedTargetId), { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                btn.disabled = false;
                btn.classList.remove('is-running');
                if (label) label.textContent = origText;
                else btn.textContent = origText;
                renderTracerouteResult(data);
                if (!data.error) {
                    loadTraceHistory();
                }
            })
            .catch(function() {
                btn.disabled = false;
                btn.classList.remove('is-running');
                if (label) label.textContent = origText;
                else btn.textContent = origText;
            });
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
