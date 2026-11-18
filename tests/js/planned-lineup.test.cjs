const { test } = require('node:test');
const assert = require('node:assert/strict');
const { formationError, swapError } = require('../../assets/js/planned-lineup.js');

function squad() {
  const types = [1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4];
  const xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15];
  return types.map((position, i) => ({ id: i + 1, position, starting: xi.includes(i + 1) }));
}

test('a legal midfield substitution preserves the starting count', () => {
  const players = squad();
  assert.equal(swapError(players, players[7], players[11]), '');
  assert.equal(players.filter(p => p.starting).length, 11);
  assert.equal(players[7].starting, true);
  assert.equal(players[11].starting, false);
});
test('changing formation is allowed when the resulting XI is legal', () => {
  const players = squad();
  assert.equal(swapError(players, players[12], players[5]), '');
});
test('a third defender cannot be replaced by a midfielder', () => {
  const players = squad();
  assert.match(swapError(players, players[2], players[11]), /2 defenders.*at least 3 defenders/);
  assert.equal(players[2].starting, true);
  assert.equal(players[11].starting, false);
});
test('goalkeeper substitutions work in both directions but reject outfield players', () => {
  const players = squad();
  assert.equal(swapError(players, players[0], players[1]), '');
  assert.equal(swapError(players, players[1], players[0]), '');
  assert.match(swapError(players, players[0], players[11]), /Goalkeepers can only swap with goalkeepers/);
});
test('the 12-player error says how to fix the selection', () => {
  const players = squad().filter(p => p.starting);
  players.push({ id: 12, position: 3 });
  assert.equal(formationError(players), 'You selected 12 starters. Select exactly 11: remove 1 player.');
});
