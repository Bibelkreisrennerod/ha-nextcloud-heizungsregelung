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
# Nacht: alle frei auskuehlen (Sollwert 5) 
run(8, lambda r,t: 5.0); report("nach 8 h Auskuehlen")
run(12, lambda r,t: 14.0)
# E1: Saal allein auf 21
run(6, lambda r,t: 21.0 if r=="Saal" else 10.0, True); report("nach Kalibrierlauf 1 (Saal allein)")
run(14, lambda r,t: 14.0)
# E2: alle gleichzeitig
run(6, lambda r,t: 10.0 if r=="Saal" else 21.0, True); report("nach Kalibrierlauf 2 (kleine Raeume zusammen)")
run(10, lambda r,t: 14.0); report("Ende")
import json; open("learned_flow.json" if USE_FLOW else "learned.json","w").write(json.dumps(lr.to_dict()))

