import os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
exec(open("harness.py").read())
random.seed(1)
import sys
USE_FLOW='flow' in sys.argv
start=datetime(2026,11,18,0,0,tzinfo=timezone.utc)
w=World(start); lr=L.Learner(); lr.room_stats("Saal").valves=5
for r in ROOMS: lr.room_stats(r)
g=Gauge(lr,P.PlantParams())
def run(hours, setfn, cal=False):
    for m in range(int(hours*60)):
        for r in ROOMS: w.set[r]=setfn(r,w.t)
        w.step(1)
        if w.t.minute%5==0:
            frac=g.update(w.valves,PRIOR)
            samples={r:L.RoomSample(w.t,w.reading(r),round(w.valves[r]*100)/100,w.set[r]) for r in ROOMS}
            lr.add_sample(w.t,samples,round(w.outside(w.t),1),frac,PRIOR,lr.plant_params(P.PlantParams()),None,cal,(round(getattr(w,'flow',70.0)*2)/2 if USE_FLOW else None),g.share)
def report(title):
    print("==",title)
    pp=lr.plant_params(P.PlantParams())
    for r in ROOMS:
        p=lr.room_params(r,PRIOR[r]); t=TRUE[r]; st=lr.room_stats(r)
        print(f"  {r[:18]:18} a {p.a:.3f}/{t.a:.3f}  b {p.b:.2f}/{t.b:.2f}  w {p.w:.2f}/{t.w:.2f}  d {p.d*60:.0f}/{t.d*60:.0f} min  cool {st.cool_windows} heat {st.heat_windows} tot {st.dead_events}  fehler {st.err_idle or 0:.2f}/{st.err_heat or 0:.2f} K ({st.err_idle_n}/{st.err_heat_n}) {'kalibriert' if lr.calibrated(r) else ''}")
    for r in ROOMS:
        c=lr.curve(r); t=TRUE[r].curve
        print(f"  Kennlinie {r[:18]:18} " + " ".join(f"{x:+.1f}:{round(v*100)}/{round(u*100)}" for x,v,u in zip(P.CURVE_DEV,c,t)) + f"  (gemessen {lr.curve_points(r)})")
    print(f"  e_buf {pp.e_buf:.2f}/{TPLANT.e_buf}  t_start {pp.t_start:.2f}/{TPLANT.t_start}  episodes {lr.plant.episodes} fit {lr.plant.last_fit}")
import time as _time
def do_fit(title):
    t0=_time.time()
    data=lr.prepare_fit(w.t,P.PlantParams(),PRIOR)
    state=lr.run_fit(data,w.t,P.PlantParams(),round(w.outside(w.t),1))
    dt=_time.time()-t0
    if state is None: print("== Anpassung",title,"nicht moeglich"); return
    lr.apply_fit(state)
    sg=state["sigma"]; v=state["values"]
    print(f"== Anpassung {title}: {state['rows']} Messpunkte, {state['residuals']} Vorhersagen, {state['iterations']} Schritte, Fehler {state['rms']:.2f} K, {dt:.1f} s")
    for r in ROOMS:
        t=TRUE[r]
        def f(k,scale=1.0,unit=""):
            val=v[f"{r}|{k}"]*scale; tru=getattr(t,k)*scale; sd=sg[f"{r}|{k}"]
            if k=="d": return f"d {val*60:.0f}/{tru*60:.0f}min ±{sd*60:.0f}"
            if k=="tau": return f"tau {val*60:.0f}/{tru*60:.0f}min ±{sd*100:.0f}%"
            return f"{k} {val:.3g}/{tru:.3g} ±{sd*100:.0f}%"
        print(f"  {r[:18]:18} "+"  ".join(f(k) for k in ("a","b","d","tau","w"))+f"  Dauer ±{state['duration_sigma'][r]*100:.0f}%  Sicherheit {lr.safety(r):.2f}")
    print(f"  Puffer {v['|e_buf']:.2f}/{TPLANT.e_buf} ±{sg['|e_buf']*100:.0f}%")
    for e in state["experiments"][:4]:
        print(f"  Testlauf {e['label']:28} Gewinn {e['gain']:5.2f}  Dauer {e['hours']} h  verbessert {[(x['raum'][:8],x['groesse'],x['unsicherheit_minus_prozent']) for x in e['improves']]}")
# Nacht: alle frei auskuehlen (Sollwert 5) 
run(8, lambda r,t: 5.0); report("nach 8 h Auskuehlen")
run(12, lambda r,t: 14.0)
# E1: Saal allein auf 21
run(6, lambda r,t: 21.0 if r=="Saal" else 10.0, True); report("nach Kalibrierlauf 1 (Saal allein)")
run(14, lambda r,t: 14.0)
do_fit("nach Auskuehlen und Lauf 1 (Saal allein)")
# E2: alle gleichzeitig
run(6, lambda r,t: 10.0 if r=="Saal" else 21.0, True); report("nach Kalibrierlauf 2 (kleine Raeume zusammen)")
run(10, lambda r,t: 14.0); report("Ende")
do_fit("nach Lauf 2 (kleine Raeume zusammen)")
# Lauf 3: Saal und Nebenraum (gross) zusammen
run(6, lambda r,t: 21.0 if r in ("Saal","Nebenraum (groß)") else 10.0, True)
run(10, lambda r,t: 14.0)
do_fit("nach Lauf 3 (Saal und Nebenraum gross)")
import json; open("learned_flow.json" if USE_FLOW else "learned.json","w").write(json.dumps(lr.to_dict()))

