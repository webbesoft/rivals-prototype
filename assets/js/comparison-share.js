/* Render a self-contained image locally: no screenshots, uploads or external assets. */
(() => {
  const trigger = document.querySelector('[data-comparison-share]');
  const dialog = document.querySelector('.rv-share-dialog');
  if (!trigger || !dialog || !dialog.showModal) return;
  const preview = dialog.querySelector('[data-share-preview]');
  const save = dialog.querySelector('[data-share-save]');
  const share = dialog.querySelector('[data-share-native]');
  const status = dialog.querySelector('[data-share-status]');
  let imageURL;
  let file;
  let generation = 0;

  function render(data) {
    const canvas = document.createElement('canvas');
    canvas.width = 1080;
    canvas.height = 1350;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas unavailable');
    const ink = '#17372e', paper = '#f1f2e9', gold = '#f0d66a';
    function box(x, y, width, height, color) {
      ctx.fillStyle = color;
      ctx.fillRect(x, y, width, height);
    }
    function text(value, x, y, size, color = ink, align = 'left', width = 940) {
      ctx.fillStyle = color;
      ctx.textAlign = align;
      let fitted = size;
      do {
        ctx.font = `600 ${fitted}px Arial, sans-serif`;
        if (ctx.measureText(String(value)).width <= width || fitted <= 18) break;
        fitted -= 1;
      } while (true);
      // Names are plain text, bounded to their column even at the minimum size.
      let label = String(value);
      if (ctx.measureText(label).width > width) {
        while (label && ctx.measureText(`${label}…`).width > width) label = label.slice(0, -1);
        label += '…';
      }
      ctx.fillText(label, x, y);
    }
    box(0, 0, 1080, 1350, paper);
    box(0, 0, 1080, 660, '#142e29');
    box(64, 66, 48, 7, gold);
    text('RIVALSPY / HEAD TO HEAD', 132, 80, 24, paper);
    text(data.gameweek ? `PUBLISHED SQUADS / GW${data.gameweek}` : 'PUBLISHED SQUADS', 64, 144, 20, '#b4c3af');
    text('Read the rivalry.', 64, 235, 76, paper);
    text('YOUR SIDE', 64, 310, 19, gold);
    text('THE OPPOSITION', 1016, 310, 19, '#b4c3af', 'right');
    text(data.mine, 64, 365, 38, paper, 'left', 420);
    text(data.rival, 1016, 365, 38, paper, 'right', 420);
    text(data.myManager, 64, 407, 22, '#b4c3af', 'left', 420);
    text(data.rivalManager, 1016, 407, 22, '#b4c3af', 'right', 420);
    const points = value => value === '' || !Number.isFinite(Number(value)) ? null : Number(value);
    const mine = points(data.myPoints), rival = points(data.rivalPoints);
    text(mine === null ? '—' : mine.toLocaleString('en-GB'), 64, 515, 82, gold, 'left', 410);
    text(rival === null ? '—' : rival.toLocaleString('en-GB'), 1016, 515, 82, paper, 'right', 410);
    text('SEASON POINTS', 64, 554, 18, '#b4c3af');
    text('SEASON POINTS', 1016, 554, 18, '#b4c3af', 'right');
    const gap = mine === null || rival === null ? 'Totals not available'
      : mine === rival ? 'Level on points'
        : `${Math.abs(mine - rival)} pts ${mine > rival ? 'ahead' : 'behind'}`;
    text(gap, 540, 620, 30, gold, 'center');
    text('THE DETAIL', 64, 722, 20);
    text('YOU', 665, 722, 20, ink, 'center');
    text('RIVAL', 920, 722, 20, ink, 'center');
    function row(y, title, subtitle, mineValue, rivalValue) {
      box(64, y - 43, 952, 1, '#ccd2c3');
      text(title, 64, y, 27, ink, 'left', 430);
      text(subtitle, 64, y + 31, 18, '#58685c', 'left', 430);
      text(mineValue, 665, y + 5, 31, ink, 'center', 225);
      text(rivalValue, 920, y + 5, 31, ink, 'center', 225);
    }
    row(804, 'Captain choice', 'From the published squad', data.myCaptain, data.rivalCaptain);
    row(918, 'Recent form', 'Points / GW · Last 5 gameweeks', data.myForm, data.rivalForm);
    const next = Number(data.gameweek);
    row(1032, 'Expected squad points', next > 0 ? `GW${next + 1} forecast` : 'Next gameweek forecast', data.myForecast, data.rivalForecast);
    box(64, 1100, 952, 1, '#ccd2c3');
    text('Forecasts are estimates, not recorded scores.', 64, 1150, 22, '#58685c');
    text(`Season scores: ${data.scoreSource}`, 64, 1190, 18, '#58685c');
    text(`Analysis: ${data.timestamp}`, 64, 1224, 18, '#58685c');
    text('RIVALSPY', 64, 1300, 25);
    text('Every point has a rival.', 1016, 1300, 22, ink, 'right');
    return new Promise((resolve, reject) => canvas.toBlob(blob => {
      if (blob) resolve(blob);
      else reject(new Error('Image export failed'));
    }, 'image/png'));
  }

  trigger.hidden = false;
  trigger.addEventListener('click', async () => {
    const current = ++generation;
    dialog.showModal();
    status.textContent = 'Creating your infographic…';
    preview.hidden = save.hidden = share.hidden = true;
    if (imageURL) URL.revokeObjectURL(imageURL);
    imageURL = undefined;
    file = undefined;
    try {
      const blob = await render(trigger.dataset);
      if (current !== generation || !dialog.open) return;
      imageURL = URL.createObjectURL(blob);
      preview.src = imageURL;
      const slug = value => value.normalize('NFKD').replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '').slice(0, 60) || 'team';
      const filename = `rivalspy-${slug(trigger.dataset.mine)}-vs-${slug(trigger.dataset.rival)}.png`;
      save.href = imageURL;
      save.download = filename;
      preview.hidden = save.hidden = false;
      if (typeof File !== 'undefined' && navigator.canShare && navigator.share) {
        file = new File([blob], filename, { type: 'image/png' });
        share.hidden = !navigator.canShare({ files: [file] });
      }
      status.textContent = 'Your infographic is ready.';
    } catch (error) {
      status.textContent = 'We couldn’t create the infographic. Close this preview and try again.';
    }
  });
  share.addEventListener('click', async () => {
    if (!file) return;
    share.disabled = true;
    try {
      await navigator.share({ files: [file], title: `${trigger.dataset.mine} vs ${trigger.dataset.rival}` });
      status.textContent = 'Image shared.';
    } catch (error) {
      if (error.name !== 'AbortError') status.textContent = 'Sharing is unavailable. Use Save PNG to keep the image.';
    } finally {
      share.disabled = false;
    }
  });
  dialog.addEventListener('close', () => {
    generation += 1;
    if (imageURL) URL.revokeObjectURL(imageURL);
    imageURL = undefined;
    file = undefined;
    preview.removeAttribute('src');
    save.removeAttribute('href');
  });
})();
