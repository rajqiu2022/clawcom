const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../web/templates/panorama.html'), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
for (const [, code] of scripts) new vm.Script(code); // Include the detail template literals.
const source = html.slice(html.indexOf('function testMetricState('), html.indexOf('function normalizeCrossModuleRelation('));
const context = vm.createContext({});
vm.runInContext(source, context);
const zero = {subtree_case_count: 0, linked_library_count: 0, bug_risk_score: 0, bug_risk_level: 'low', source: 'sync'};
assert.equal(context.testMetricState(zero).assessed, false);
assert.equal(context.testMetricState(zero).linked, false);
assert.equal(context.testMetricColor(zero), '#64748b');
assert.equal(context.testMetricColor(null), '#64748b');
assert.equal(context.testMetricState({...zero, linked_library_count: 1}).linked, true);
assert.equal(context.testMetricColor({...zero, metrics_payload: {risk_assessed: true}}), '#22c55e');
assert.equal(context.testMetricColor({...zero, bug_risk_score: 85}), '#ef4444');
assert.equal(context.testMetricColor({...zero, bug_count: 12}), '#64748b');
assert.equal(context.testMetricState({...zero, bug_risk_score: null, metrics_payload: {risk_assessed: true}}).assessed, false);
assert.equal(context.testMetricState({...zero, bug_risk_score: 'invalid'}).assessed, false);
const badges = metric => JSON.parse(JSON.stringify(context.testMetricBadges(metric)));
assert.deepEqual(badges(null), []);
assert.deepEqual(badges(zero), []);
assert.deepEqual(badges({...zero, linked_library_count: 1, metrics_payload: {risk_assessed: true}}), []);
assert.deepEqual(badges({...zero, subtree_case_count: 10}), [{text: '10', color: '#2563eb'}]);
assert.deepEqual(badges({...zero, bug_risk_score: 20}), [{text: '20', color: '#f97316'}]);
assert.deepEqual(badges({...zero, subtree_case_count: 8, bug_risk_score: 85}), [
    {text: '8', color: '#2563eb'}, {text: '85', color: '#dc2626'},
]);
assert.deepEqual(badges({subtree_case_count: -1, bug_risk_score: 'invalid'}), []);
console.log('PASS: metric states; numeric-only badges; zero/missing hidden; one/two badges; template JS syntax');
