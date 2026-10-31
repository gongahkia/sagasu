const test=require('node:test');const assert=require('node:assert/strict');const {parseSearch}=require('../web/search.js');
const days=['2026-10-31','2026-11-01','2026-11-02'];
test('parses location, relative date and shared pm suffix while flagging equipment',()=>{const r=parseSearch('SOA tomorrow 2-4pm with a projector',days,'2026-10-31');assert.equal(r.query,'SOA');assert.equal(r.date,'2026-11-01');assert.equal(r.start,'14:00');assert.equal(r.end,'16:00');assert.equal(r.error,null);assert.match(r.warnings[0],/do not verify/);});
test('ISO dates and room numbers are not mistaken for time ranges',()=>{const r=parseSearch('SCIS1 GSR 2-1 on 2026-11-02 10am to 12pm',days,'2026-10-31');assert.equal(r.error,null);assert.equal(r.start,'10:00');assert.equal(r.end,'12:00');assert.equal(r.query,'SCIS1 GSR 2-1');});
test('rejects invalid dates and backwards time rather than claiming matches',()=>{assert.ok(parseSearch('SOA 8pm-2pm',days,'2026-10-31').error);assert.ok(parseSearch('SOA 2027-01-01 2-4pm',days,'2026-10-31').error);});
test('parses start with duration',()=>{const r=parseSearch('business from 2pm for 2 hours',days,'2026-10-31');assert.equal(r.start,'14:00');assert.equal(r.end,'16:00');assert.equal(r.error,null);});

test('interprets an implied morning start before an afternoon end',()=>{const r=parseSearch('SOA 10-2pm',days,'2026-10-31');assert.equal(r.start,'10:00');assert.equal(r.end,'14:00');assert.equal(r.error,null);});

test('an unpublished weekday cannot silently become today',()=>{assert.ok(parseSearch('SOA Wednesday 2-4pm',days,'2026-10-31').error);});

test('understands common short dates and weekday abbreviations',()=>{assert.equal(parseSearch('SOA tmr 2-4pm',days,'2026-10-31').date,'2026-11-01');assert.equal(parseSearch('SOA mon 2-4pm',days,'2026-10-31').date,'2026-11-02');});
