// ── Semester Planner (shared across advisor + my_plan pages) ─────────────────
// Stored in localStorage so it persists across page reloads without a server round-trip.

const PLAN_KEY    = 'semester_plan';
const CREDIT_MAX  = 21;

function getPlan() {
  try { return JSON.parse(localStorage.getItem(PLAN_KEY) || '[]'); }
  catch { return []; }
}

function savePlan(plan) {
  localStorage.setItem(PLAN_KEY, JSON.stringify(plan));
}

function addToPlan(code, title, credits) {
  const plan = getPlan();
  if (!plan.find(c => c.code === code)) {
    plan.push({ code, title, credits: parseFloat(credits) || 3 });
    savePlan(plan);
  }
  renderPlanner();
  syncAddButtons();
}

function removeFromPlan(code) {
  savePlan(getPlan().filter(c => c.code !== code));
  renderPlanner();
  syncAddButtons();
}

function renderPlanner() {
  const planList    = document.getElementById('plan-list');
  const planEmpty   = document.getElementById('plan-empty');
  const planCredits = document.getElementById('plan-credits');
  const planWarning = document.getElementById('plan-warning');
  if (!planList) return;

  const plan  = getPlan();
  const total = plan.reduce((sum, c) => sum + c.credits, 0);

  planCredits.textContent = `${total} cr`;
  planCredits.className   = `badge ms-auto ${total > CREDIT_MAX ? 'bg-danger' : 'bg-secondary'}`;

  if (plan.length === 0) {
    planEmpty.classList.remove('d-none');
    planList.classList.add('d-none');
    planList.innerHTML = '';
  } else {
    planEmpty.classList.add('d-none');
    planList.classList.remove('d-none');
    planList.innerHTML = plan.map(c => `
      <div class="d-flex justify-content-between align-items-center small px-3 py-2 border-bottom">
        <div>
          <a href="/course/${encodeURIComponent(c.code)}" class="fw-semibold text-decoration-none">${c.code}</a>
          ${c.title ? `<span class="text-muted ms-1">${c.title.substring(0, 45)}${c.title.length > 45 ? '…' : ''}</span>` : ''}
        </div>
        <div class="d-flex align-items-center gap-2 flex-shrink-0 ms-2">
          <span class="text-muted">${c.credits} cr</span>
          <button type="button" class="btn btn-sm btn-outline-danger py-0 px-1 remove-from-plan-btn"
                  data-code="${c.code}"
                  style="font-size:.7rem">✕</button>
        </div>
      </div>
    `).join('');
  }

  if (planWarning) {
    planWarning.classList.toggle('d-none', total <= CREDIT_MAX);
  }
}

function syncAddButtons() {
  const codes = new Set(getPlan().map(c => c.code));
  document.querySelectorAll('.add-to-plan-btn').forEach(btn => {
    const inPlan = codes.has(btn.dataset.code);
    btn.disabled   = inPlan;
    btn.innerHTML  = inPlan ? '<i class="bi bi-check"></i>' : '<i class="bi bi-plus"></i>';
    btn.className  = `btn btn-sm py-0 add-to-plan-btn ${inPlan ? 'btn-success' : 'btn-outline-primary'}`;
  });
}

// Always wire up + and × buttons via event delegation regardless of page
document.addEventListener('click', e => {
  const add = e.target.closest('.add-to-plan-btn');
  if (add && !add.disabled) { addToPlan(add.dataset.code, add.dataset.title, add.dataset.credits); return; }

  const remove = e.target.closest('.remove-from-plan-btn');
  if (remove) { removeFromPlan(remove.dataset.code); }
});

// Initialise planner display on pages that have it
document.addEventListener('DOMContentLoaded', () => {
  if (!document.getElementById('plan-list')) return;
  renderPlanner();
  syncAddButtons();
});
