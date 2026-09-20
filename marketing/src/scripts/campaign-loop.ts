import { campaignSteps } from '../data/campaign-story';

for (const root of document.querySelectorAll<HTMLElement>('[data-campaign-loop]')) {
  const frames = [...root.querySelectorAll<HTMLElement>('[data-campaign-frame]')];
  const buttons = [...root.querySelectorAll<HTMLButtonElement>('[data-campaign-step]')];
  const next = root.querySelector<HTMLButtonElement>('[data-campaign-next]')!;
  const announcement = root.querySelector<HTMLElement>('[data-campaign-announcement]')!;
  let current = 0;
  function show(index: number, announce = true) {
    current = index;
    frames.forEach((frame, i) => { frame.hidden = i !== index; frame.inert = i !== index; });
    buttons.forEach((button, i) => button.setAttribute('aria-pressed', String(i === index)));
    next.textContent = index === frames.length - 1 ? 'Replay the loop ↻' : `Next: ${campaignSteps[index + 1].label} →`;
    if (announce) announcement.textContent = `Step ${index + 1} of ${frames.length}. ${campaignSteps[index].title}`;
  }
  root.dataset.enhanced = '';
  root.querySelector<HTMLElement>('.campaign-controls')!.hidden = false;
  next.hidden = false;
  buttons.forEach((button, i) => button.addEventListener('click', () => show(i)));
  next.addEventListener('click', () => show((current + 1) % frames.length));
  show(0, false);
}
