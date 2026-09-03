'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

/* chart-engine.js is a plain browser script: run it in a bare context and pick the
   one global under test out of it. Nothing at its top level touches the DOM. */
const sandbox = {window: {}};
vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, '../../app/static/js/chart-engine.js'), 'utf8'),
    sandbox
);
const smooth = sandbox.docsightSmoothSeries;
/* The filtered array is built inside the vm realm, so copy it back for deepEqual */
const smoothed = (values, opts) => Array.from(smooth(values, opts));

test('the jitter filter flattens small wobble around the local baseline', () => {
    const raw = [14, 15, 14, 12, 15, 13, 15, 14];
    const out = smoothed(raw);
    assert.notDeepEqual(out, raw);
    for (const v of out) {
        assert.ok(v >= 13 && v <= 15, `${v} should sit on the baseline`);
    }
    const spread = (arr) => Math.max(...arr) - Math.min(...arr);
    assert.ok(spread(out) < spread(raw), 'the filtered line must vary less than the raw one');
});

test('a real spike keeps its height and its index', () => {
    const raw = [15, 14, 15, 60, 15, 14, 15];
    const out = smooth(raw);
    assert.equal(out[3], 60, 'the spike must survive the filter');
    assert.equal(out.length, raw.length);
});

test('nulls stay nulls and loss gaps are never bridged', () => {
    const raw = [15, 14, null, null, 15, 14, 15];
    const out = smooth(raw);
    assert.equal(out[2], null);
    assert.equal(out[3], null);
});

test('window edges are filtered from the neighbours that exist', () => {
    const raw = [10, 20, 12, 11, 12];
    const out = smooth(raw);
    assert.equal(out[0], 12, 'the first sample uses the three-sample half window');
    assert.equal(out.length, raw.length);
});

test('a sample with fewer than two non-null neighbours is left alone', () => {
    assert.deepEqual(smoothed([15, null, 42, null, 15]), [15, null, 42, null, 15]);
    assert.deepEqual(smoothed([7, 99]), [7, 99]);
    assert.deepEqual(smoothed([]), []);
});

test('thresholds are per-call tunable for the percentage charts', () => {
    const raw = [40, 41, 55, 40, 41];
    assert.equal(smooth(raw)[2], 41, 'the ms defaults treat 55 % as jitter around 41 %');
    assert.equal(smooth(raw, {spikeAbs: 5, spikeRel: 0})[2], 55, 'a tighter guard keeps it');
});
