/* Pitch substitutions keep a legal XI; the server validates every saved lineup. */
(() => {
  function formationError(players) {
    if (players.length !== 11) {
      const count = Math.abs(players.length - 11);
      return `You selected ${players.length} starters. Select exactly 11: ${players.length > 11 ? 'remove' : 'add'} ${count} player${count === 1 ? '' : 's'}.`;
    }
    const limits = [[1, 'goalkeeper', 1, 1], [2, 'defender', 3, 5], [3, 'midfielder', 2, 5], [4, 'forward', 1, 3]];
    for (const [position, label, min, max] of limits) {
      const count = players.filter(p => p.position === position).length;
      if (count < min || count > max) {
        const limit = min === max ? 'exactly 1' : count < min ? `at least ${min}` : `no more than ${max}`;
        return `This lineup has ${count} ${label}${count === 1 ? '' : 's'}. You need ${limit} ${label}${min === max ? '' : 's'}.`;
      }
    }
    return '';
  }

  function swapError(players, first, second) {
    if (first.starting === second.starting) return 'Choose one starter and one substitute.';
    if ((first.position === 1) !== (second.position === 1)) return 'Goalkeepers can only swap with goalkeepers.';
    const proposed = players.filter(p => p === first || p === second ? !p.starting : p.starting);
    return formationError(proposed);
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { formationError, swapError };
  if (typeof document === 'undefined') return;
  const editors = new WeakMap();

  function initialise(section) {
    if (editors.has(section)) return;
    const form = section.querySelector('#planned-lineup-form');
    const pitch = section.querySelector('[data-lineup-pitch-editor]');
    if (!form || !pitch) return;
    const players = [...pitch.querySelectorAll('[data-lineup-player]')].map(node => ({
      id: Number(node.dataset.lineupPlayer), name: node.dataset.name,
      position: Number(node.dataset.position), points: Number(node.dataset.points.replace(',', '.')),
      starting: node.dataset.starting === 'true', node,
    }));
    if (players.length !== 15) return;
    let captain = players.find(p => p.node.dataset.captain === 'true')?.id;
    let vice = players.find(p => p.node.dataset.vice === 'true')?.id;
    const original = { starters: players.filter(p => p.starting).map(p => p.id), captain, vice, mode: form.elements.mode.value };
    const points = pitch.querySelector('[data-lineup-points]');
    const pointsNote = pitch.querySelector('[data-lineup-points-note]');
    const originalPoints = points.textContent;
    const originalNote = pointsNote.textContent;
    const comparisons = [...pitch.querySelectorAll('[data-lineup-comparison]')];
    const title = pitch.querySelector('[data-lineup-title]');
    const originalTitle = title.textContent;
    const saveState = section.querySelector('[data-lineup-save-state]');
    const originalSaveState = saveState.textContent;
    const feedback = section.querySelector('[data-lineup-feedback]');
    const actions = section.querySelector('[data-lineup-actions]');
    const error = section.querySelector('[data-lineup-error]');
    let selected = null;
    let dirty = false;
    let pending = false;
    let lockedControls = [];
    const starters = () => players.filter(p => p.starting);

    function orderPlayers(destination, ordered) {
      ordered.forEach((p, index) => {
        if (destination.children[index] !== p.node) destination.insertBefore(p.node, destination.children[index] || null);
      });
    }

    function setPending(value) {
      pending = value;
      form.setAttribute('aria-busy', String(value));
      if (value) {
        lockedControls = [...section.querySelectorAll('[data-lineup-select], [data-lineup-actions] button, [data-lineup-undo], select, [name="planner-gameweek"]')].map(control => [control, control.disabled]);
        for (const [control] of lockedControls) control.disabled = true;
        feedback.textContent = 'Saving your XI. Editing is paused until the request finishes.';
      } else {
        for (const [control, disabled] of lockedControls) control.disabled = disabled;
        lockedControls = [];
      }
    }

    function syncInputs() {
      const inputs = form.querySelector('[data-lineup-inputs]');
      inputs.replaceChildren();
      for (const [name, value] of [...starters().map(p => ['starters', p.id]), ['captain', captain ?? ''], ['vice', vice ?? '']]) {
        const input = document.createElement('input');
        input.type = 'hidden'; input.name = name; input.value = value;
        inputs.append(input);
      }
    }

    function showError(message, heading = 'Substitution not allowed') {
      error.querySelector('strong').textContent = heading;
      error.querySelector('[data-lineup-error-detail]').textContent = message;
      error.querySelector('p').hidden = true;
      error.hidden = false;
      error.focus();
    }

    function render() {
      const counts = [2, 3, 4].map(pos => starters().filter(p => p.position === pos).length);
      pitch.querySelector('[data-lineup-formation]').textContent = counts.join('–');
      pitch.querySelector('.lineup-pitch').setAttribute('aria-label', `Starting XI, formation ${counts.join('–')}`);
      section.querySelector('[data-lineup-count]').textContent = `${starters().length} / 11 starters · ${counts.join('–')}`;
      for (const p of players) {
        p.node.dataset.starting = String(p.starting);
        const badge = p.node.querySelector('.lineup-bench-position');
        badge.hidden = p.starting;
        badge.textContent = ['','GKP','DEF','MID','FWD'][p.position];
        p.node.querySelector('.lineup-armband')?.remove();
        if (p.starting && (p.id === captain || p.id === vice)) {
          const armband = document.createElement('span');
          armband.className = `lineup-armband${p.id === vice ? ' lineup-armband--vice' : ''}`;
          armband.textContent = p.id === captain ? 'C' : 'V';
          armband.setAttribute('aria-label', p.id === captain ? 'Captain' : 'Vice captain');
          p.node.querySelector('.lineup-kit').append(armband);
        }
        const opposite = selected && selected.starting !== p.starting;
        const reason = opposite ? swapError(players, selected, p) : '';
        p.node.classList.toggle('is-selected', selected === p);
        p.node.classList.toggle('is-swap-target', !!opposite && !reason);
        p.node.classList.toggle('is-swap-blocked', !!opposite && !!reason);
        const button = p.node.querySelector('[data-lineup-select]');
        button.setAttribute('aria-pressed', String(selected === p));
        button.setAttribute('aria-label', opposite ? (reason ? `${p.name}: ${reason}` : `Swap ${selected.name} with ${p.name}`) : `Select ${p.name} to substitute or assign captaincy`);
      }
      for (const position of [1, 2, 3, 4]) {
        orderPlayers(pitch.querySelector(`[data-lineup-position="${position}"]`), starters().filter(p => p.position === position));
      }
      // Keep the goalkeeper first and the explanatory projected-points bench order.
      const benchRow = pitch.querySelector('[data-lineup-bench]');
      orderPlayers(benchRow, players.filter(p => !p.starting).sort((a, b) => (a.position !== 1) - (b.position !== 1) || b.points - a.points || a.id - b.id));
      for (const row of pitch.querySelectorAll('[data-lineup-position]')) row.hidden = !row.children.length;
      actions.hidden = !selected;
      section.querySelector('[data-lineup-captain]').hidden = !selected?.starting;
      section.querySelector('[data-lineup-vice]').hidden = !selected?.starting;
      section.querySelector('[data-lineup-undo]').hidden = !dirty;
      syncInputs();
    }

    function invalidateForecast() {
      points.textContent = '—';
      pointsNote.textContent = 'Save to recalculate your draft XI';
      comparisons.forEach(node => { node.hidden = true; });
      title.textContent = 'Your draft XI';
    }

    function markChanged(message) {
      dirty = true;
      form.elements.mode.value = 'planned';
      invalidateForecast();
      saveState.textContent = 'Unsaved changes';
      saveState.classList.add('is-unsaved');
      error.hidden = true;
      feedback.textContent = message;
      render();
    }

    function choose(player) {
      error.hidden = true;
      if (!selected || selected.starting === player.starting) {
        selected = selected === player ? null : player;
        feedback.textContent = selected ? `${selected.name} selected. Choose a highlighted ${selected.starting ? 'bench player' : 'starter'} to swap, or cancel.` : 'Select a player on the pitch, then a highlighted substitute to swap them.';
        render();
        return;
      }
      const reason = swapError(players, selected, player);
      if (reason) return showError(`Cannot swap ${selected.name} with ${player.name}. ${reason} No players were moved.`);
      const outgoing = selected.starting ? selected : player;
      const incoming = selected.starting ? player : selected;
      outgoing.starting = false; incoming.starting = true;
      if (captain === outgoing.id) captain = incoming.id;
      if (vice === outgoing.id) vice = incoming.id;
      selected = null;
      markChanged(`${incoming.name} on for ${outgoing.name}. Save your lineup to update the transfer advice.`);
      incoming.node.querySelector('button').focus({ preventScroll: true });
    }

    section.addEventListener('click', event => {
      if (pending) return;
      const playerButton = event.target.closest('[data-lineup-select]');
      if (playerButton) return choose(players.find(p => p.node === playerButton.closest('[data-lineup-player]')));
      if (event.target.closest('[data-lineup-cancel]')) {
        selected?.node.querySelector('button').focus({ preventScroll: true });
        selected = null; feedback.textContent = 'Selection cancelled. Choose a player to make a substitution.'; render();
      }
      if (selected?.starting && event.target.closest('[data-lineup-captain], [data-lineup-vice]')) {
        const name = selected.name;
        const makingCaptain = !!event.target.closest('[data-lineup-captain]');
        if (makingCaptain) { if (vice === selected.id) vice = captain; captain = selected.id; }
        else { if (captain === selected.id) captain = vice; vice = selected.id; }
        selected.node.querySelector('button').focus({ preventScroll: true });
        selected = null;
        markChanged(`${name} is your ${makingCaptain ? 'captain' : 'vice-captain'}. Changes not saved yet.`);
      }
      if (event.target.closest('[data-lineup-undo]')) {
        section.querySelector('[data-lineup-save]').focus({ preventScroll: true });
        for (const p of players) p.starting = original.starters.includes(p.id);
        captain = original.captain; vice = original.vice; selected = null; dirty = false;
        form.elements.mode.value = original.mode;
        points.textContent = originalPoints; pointsNote.textContent = originalNote; title.textContent = originalTitle;
        comparisons.forEach(node => { node.hidden = false; });
        saveState.textContent = originalSaveState; saveState.classList.remove('is-unsaved');
        error.hidden = true; feedback.textContent = 'Changes undone. Your original lineup is restored.'; render();
      }
    });
    section.addEventListener('keydown', event => {
      if (!pending && event.key === 'Escape') { selected?.node.querySelector('button').focus({ preventScroll: true }); selected = null; render(); feedback.textContent = 'Selection cancelled.'; }
    });
    section.addEventListener('change', event => {
      if (!event.target.matches('[name="planner-gameweek"]')) return;
      const editingNextWeek = event.target.nextElementSibling?.nextElementSibling === pitch;
      section.querySelector('.planner-lineup-editor').hidden = !editingNextWeek;
      selected = null;
      feedback.textContent = 'Select a player on the pitch, then a highlighted substitute to swap them.';
      render();
    });
    form.addEventListener('change', () => {
      if (pending) return;
      dirty = true; saveState.textContent = 'Unsaved changes'; saveState.classList.add('is-unsaved');
      invalidateForecast();
      feedback.textContent = form.elements.mode.value === 'optimised' ? 'Save to restore the best projected XI and captain.' : 'Save to compare transfers against the XI shown on the pitch.';
      render();
    });
    // Capture validation before HTMX's submit handler can start a request.
    form.addEventListener('submit', event => {
      if (pending) { event.preventDefault(); event.stopImmediatePropagation(); return; }
      const problem = formationError(starters()) || (!starters().some(p => p.id === captain) || !starters().some(p => p.id === vice) || captain === vice ? 'Choose a different starting captain and vice-captain.' : '');
      if (problem && form.elements.mode.value === 'planned') {
        event.preventDefault(); event.stopImmediatePropagation(); showError(problem, 'Lineup not saved');
      } else syncInputs();
    }, true);
    form.addEventListener('htmx:beforeRequest', () => setPending(true));
    form.addEventListener('htmx:afterRequest', event => {
      setPending(false);
      if (event.detail.successful === false) showError("We couldn't save your lineup. Your draft is still on the pitch. Please try saving again.", 'Lineup not saved');
    });
    editors.set(section, { players });
    render();
    if (!error.hidden) error.focus();
  }

  function scan(root) {
    if (root.matches?.('.planner-panel')) initialise(root);
    root.querySelectorAll?.('.planner-panel').forEach(initialise);
  }
  document.addEventListener('DOMContentLoaded', () => scan(document));
  document.addEventListener('htmx:load', event => scan(event.detail?.elt || event.target));
  scan(document);
})();
