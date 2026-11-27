(() => {
  const editors = new WeakSet();

  function initialise(route) {
    if (editors.has(route)) return;
    const players = JSON.parse(route.querySelector('script[type="application/json"]').textContent);
    const playersById = new Map(players.map(player => [String(player.id), player]));
    const form = route.closest('form');
    const horizon = form.elements.horizon;
    const windowLabel = route.querySelector('[data-transfer-window]');
    const moves = [1, 2].map(index => ({
      sell: route.querySelector(`[name="out_${index}"]`),
      buy: route.querySelector(`[name="in_${index}"]`),
    }));

    function update({ sell, buy }) {
      const selected = buy.value;
      const outgoing = playersById.get(sell.value);
      const weeks = Number(horizon.value);
      const total = player => player.points.slice(0, weeks).reduce((sum, points) => sum + points, 0);
      const choices = players.filter(player => player.position === outgoing?.position);
      buy.replaceChildren(new Option('No move', ''), ...choices.map(player => {
        const difference = Number((total(player) - total(outgoing)).toFixed(1));
        const label = `${player.name} · ${player.club} · ${difference >= 0 ? '+' : ''}${difference.toFixed(1)} xPts`;
        return new Option(label, String(player.id));
      }));
      buy.value = choices.some(player => String(player.id) === selected) ? selected : '';
      buy.disabled = !outgoing;
    }

    function updateWindow() {
      const start = Number(route.dataset.startGameweek);
      const end = Math.min(38, start + Number(horizon.value) - 1);
      windowLabel.textContent = start === end ? `GW${start}` : `GW${start}–${end}`;
    }

    form.addEventListener('change', event => {
      if (event.target === horizon) updateWindow();
      for (const move of moves) {
        if (event.target === move.sell || event.target === horizon || event.target.name?.startsWith('owned_')) update(move);
      }
    });
    updateWindow();
    moves.forEach(update);
    editors.add(route);
  }

  function scan(root) {
    if (root.matches?.('[data-transfer-route]')) initialise(root);
    root.querySelectorAll?.('[data-transfer-route]').forEach(initialise);
  }
  document.addEventListener('change', event => {
    const week = event.target;
    const outlook = week.closest('.outlook-switch');
    if (!outlook || !week.matches('.planner-week-toggle')) return;
    const otherId = week.id.startsWith('route-') ? week.id.slice(6) : 'route-' + week.id;
    const otherWeek = outlook.querySelector('#' + otherId);
    if (otherWeek) otherWeek.checked = true;
  });
  document.addEventListener('DOMContentLoaded', () => scan(document));
  document.addEventListener('htmx:load', event => scan(event.detail?.elt || event.target));
  scan(document);
})();
