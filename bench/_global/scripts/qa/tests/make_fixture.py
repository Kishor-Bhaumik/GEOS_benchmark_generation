"""Synthetic infer_then_predict fixture: hidden mu, structural axis bc; A = 10mu+20mu^2, B = exp(6mu) (x1.3 for bc=q)."""
import csv, json, math, os, sys
out = sys.argv[1]
A = lambda mu, bc: 10*mu + 20*mu*mu + (0.4 if bc == 'q' else 0)
B = lambda mu, bc: math.exp(10*mu) * (1.3 if bc == 'q' else 1.0)
sw = os.path.join(out, 'sweep'); os.makedirs(sw, exist_ok=True)
grid = [0.10, 0.14, 0.18, 0.22, 0.26, 0.30]
json.dump({'axes': {'mu': grid, 'bc': ['p', 'q']}}, open(os.path.join(sw, 'sweep_config.json'), 'w'))
rows, i = [], 0
for bc in ('p', 'q'):
    for mu in grid:
        i += 1
        rows.append({'run_id': 'run_%04d' % i, 'mu': mu, 'bc': bc, 'ovalization_mm': A(mu, bc), 'slip_um': B(mu, bc)})
with open(os.path.join(sw, 'sweep_table.csv'), 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
open(os.path.join(sw, 'failed_runs.csv'), 'w').write('run_id,mu,bc,error\n')
mu_true = 0.2513
obs = float('%.4g' % A(mu_true, 'p'))
lo, hi = 0.22, 0.26
lo_run, hi_run = 'run_0004', 'run_0005'
hist, n = [{'bracket': [lo, hi], 'runs': [lo_run, hi_run]}], 0
def ref(mu):
    global n
    n += 1
    rid = 'refine/ref_%02d' % n
    d = os.path.join(sw, 'refine', 'ref_%02d' % n); os.makedirs(d, exist_ok=True)
    json.dump({'run_id': rid, 'params': {'mu': mu, 'bc': 'p'},
               'observables': {'ovalization_mm': A(mu, 'p'), 'slip_um': B(mu, 'p')}}, open(os.path.join(d, 'observables.json'), 'w'))
    return rid
tolB = None
while True:
    m = (lo + hi) / 2
    r = ref(m)
    if A(m, 'p') < obs: lo, lo_run = m, r
    else: hi, hi_run = m, r
    hist.append({'bracket': [lo, hi], 'runs': [lo_run, hi_run]})
    mid = (lo + hi) / 2
    if hi - lo <= 0.5 * 0.01 * mid and abs(B(hi, 'p') - B(lo, 'p')) <= 0.5 * 0.01 * B(mid, 'p'):
        break
mid = (lo + hi) / 2
final = ref(mid)
tv = B(mid, 'p')
qdir = os.path.join(out, 'question'); os.makedirs(qdir, exist_ok=True)
q = ('A steel casing is cemented in a circular borehole ... (synthetic test question). The friction coefficient, the same on '
     'both interfaces, is not known; it lies between 0.11 and 0.29. The ovalization of the casing bore in the final state '
     'is measured as %s mm. What is the cement-rock slip at an angle of 45 degrees in the final state, in micrometres?' % ('%.4g' % obs))
open(os.path.join(qdir, 'question.txt'), 'w').write(q)
key = {'question_type': 'infer_then_predict', 'answer_type': 'numeric', 'observable': 'slip', 'units': 'um',
       'answer_column': 'slip_um', 'true_value_grid': B(0.26, 'p'), 'true_value': tv,
       'tolerance_type': 'relative', 'tolerance_value': 1.0, 'tolerance_abs': 0.01 * abs(tv), 'needs_refinement': True,
       'bracket_from_table': [0.22, 0.26],
       'provenance': {'table': 'sweep_table.csv', 'runs': [final], 'columns': ['ovalization_mm', 'slip_um'],
                      'selection_rule': 'B at the refined inferred mu'},
       'case': {'mu': 'hidden', 'bc': 'p'},
       'refinement': {'final_bracket': [lo, hi], 'bracket_width': hi - lo, 'bracket_runs': [lo_run, hi_run],
                      'final_run': final, 'history': hist, 'runs': [h['runs'] for h in hist]},
       'type_fields': {'hidden_input': {'name': 'mu', 'interval': [0.11, 0.29]}, 'observed_output': 'ovalization, final state',
                       'observed_column': 'ovalization_mm', 'observed_value': obs, 'inferred_input_value': mid,
                       'baseline_value': 0.2,
                       'infer_predict_checks': {'uniqueness': True, 'nonlinearity': True, 'rounding_robustness': True}},
       'list_b_items': [{'item': '1 effective stress', 'handling': 'not relevant', 'sentence': ''}],
       'hardness_rationale': 'test', 'naive_prediction': None}
json.dump(key, open(os.path.join(qdir, 'answer_key.json'), 'w'), indent=1)
print('fixture: obs A %s, inferred mu %.5f, true B %.5f, %d refine runs' % (obs, mid, tv, n))
