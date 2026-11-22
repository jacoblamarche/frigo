// Les formulaires data-async partent sans recharger la page, et la liste se met à jour
// toute seule quand l'autre personne la modifie.
const liste = document.getElementById('liste');

async function refresh() {
  if (!liste) return;
  try {
    const response = await fetch(liste.dataset.src, { headers: { 'If-None-Match': liste.dataset.etag } });
    if (response.status !== 200) return;
    liste.dataset.etag = response.headers.get('ETag');
    liste.innerHTML = await response.text();
    const count = liste.querySelector('[data-count]');
    const counter = document.getElementById('count');
    if (count && counter) counter.textContent = `${count.dataset.count} à acheter`;
  } catch {
    // Réseau coupé : on réessaiera au prochain tour.
  }
}

// En phase de capture, pour passer avant l'envoi asynchrone ci-dessous.
document.addEventListener('submit', (event) => {
  const message = event.target.dataset.confirm;
  if (message && !confirm(message)) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

document.addEventListener('submit', async (event) => {
  const form = event.target;
  if (!form.matches('form[data-async]')) return;
  event.preventDefault();
  const body = new FormData(form);
  if (form.hasAttribute('data-reset')) form.reset();
  await fetch(form.action, { method: 'POST', body, headers: { 'X-Frigo-Async': '1' } });
  if (form.id === 'paste') form.classList.remove('open');
  await refresh();
});

document.addEventListener('click', (event) => {
  const toggle = event.target.closest('[data-toggle]');
  if (!toggle) return;
  event.preventDefault();
  document.getElementById(toggle.dataset.toggle).classList.toggle('open');
});

setInterval(() => document.visibilityState === 'visible' && refresh(), 5000);
document.addEventListener('visibilitychange', () => document.visibilityState === 'visible' && refresh());

// Garde l'écran allumé pendant qu'on cuisine. Le navigateur relâche le verrou quand la page est
// cachée : on le redemande au retour.
const awake = document.querySelector('[data-wake]');
if (awake && 'wakeLock' in navigator) {
  const lock = () => navigator.wakeLock.request('screen').then(() => { awake.hidden = false; }, () => {});
  lock();
  document.addEventListener('visibilitychange', () => document.visibilityState === 'visible' && lock());
}
