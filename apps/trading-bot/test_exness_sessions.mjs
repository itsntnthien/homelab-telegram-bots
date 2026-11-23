import test from 'node:test';
import assert from 'node:assert/strict';
import {sessionSecond, enabledSessions} from './exness_sessions.mjs';

const session = (start, end, mode='enabled') => ({
  trade_mode: mode, week_start_time: start, week_end_time: end,
});

test('comma and whitespace session formats identify the same UTC boundary', () => {
  for (const value of ['Mon, 23:00:30', 'Mon 23:00:30', ' Mon,23:00:30 ', 'Mon , 23:00:30', 'Mon\t23:00:30']) {
    assert.equal(sessionSecond(value), 169230);
  }
});

test('weekly boundaries retain the first and final seconds of the week', () => {
  assert.equal(sessionSecond('Sun 00:00:00'), 0);
  assert.equal(sessionSecond('Sat 23:59:59'), 604799);
  assert.deepEqual(enabledSessions({trade_sessions:[session('Sat 23:59:59','Sat 23:59:59')]}), [[604799,604800]]);
});

test('malformed times and non-string boundaries are rejected', () => {
  for (const value of [undefined, null, 123, '', 'Monday 23:00:30', 'Mon 24:00:00',
                       'Mon 23:60:00', 'Mon 23:00:60', 'Mon 3:00:30', 'Mon 23:00:30 extra']) {
    assert.throws(() => sessionSecond(value), /INVALID_SCHEDULE/);
  }
});

test('disabled sessions do not open the market and the final enabled second is included', () => {
  const intervals = enabledSessions({trade_sessions:[
    session('Mon, 10:00:00','Mon, 10:00:59'), session('Mon 11:00:00','Mon 12:00:00','disabled'),
  ]});
  const open = second => intervals.some(([start,end]) => start<=second && second<end);
  assert.equal(open(122399), false);
  assert.equal(open(122400), true);
  assert.equal(open(122459), true);
  assert.equal(open(122460), false);
  assert.equal(open(126000), false);
});

test('missing, empty, reversed and week-wrapping calendars block collection', () => {
  for (const conditions of [{}, {trade_sessions:{}}, {trade_sessions:[]},
    {trade_sessions:[session('Mon 10:00:00','Mon 11:00:00','disabled')]},
    {trade_sessions:[session('Mon 11:00:00','Mon 10:00:00')]},
    {trade_sessions:[session('Sat 23:00:00','Sun 01:00:00')]},
    {trade_sessions:[{trade_mode:'enabled',week_end_time:'Mon 11:00:00'}]}]) {
    assert.throws(() => enabledSessions(conditions), /INVALID_SCHEDULE/);
  }
});
