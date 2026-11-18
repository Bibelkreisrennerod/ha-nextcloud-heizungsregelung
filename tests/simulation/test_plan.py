import os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
exec(open("harness.py").read())
import json, time
lr=L.Learner(json.loads(open("learned.json").read()))
def run_day(outside_shift=0.0, use_planner=True, label="", truth=False):
    day=datetime(2026,11,22,0,0,tzinfo=timezone.utc)  # So, 01:00 Ortszeit
    start=day-timedelta(hours=8)  # Planer laeuft schon seit Samstagabend
    w=World(start); w.state=P.PlantState(TPLANT.e_buf)
    base=w.outside; w.outside=lambda t: base(t)+outside_shift
    for r in ROOMS: w.temps[r]=14.0
    ev={"Saal":[(day+timedelta(hours=9),day+timedelta(hours=10,minutes=30))],
        "Nebenraum (groß)":[(day+timedelta(hours=9),day+timedelta(hours=10,minutes=30))],
        "Nebenraum (klein)":[(day+timedelta(hours=9),day+timedelta(hours=10,minutes=30))],
        "Küche":[(day+timedelta(hours=10,minutes=30),day+timedelta(hours=12))],
        "Vorraum Küche 2":[(day+timedelta(hours=10,minutes=30),day+timedelta(hours=12))],
        "Ehemaliger Sonntagsschulraum":[]}
    prio={"Saal":1,"Nebenraum (groß)":2}
    plans={}; reach={}; since={}; cmds=[]; tsum=0; nplan=0; res=None; last_sig=None; nreplan=0
    gauge=P.PlantState(lr.plant_params(P.PlantParams()).e_buf)
    for m in range(21*60):
        now=w.t
        if m%10==0:
            plant=TPLANT if truth else lr.plant_params(P.PlantParams())
            def committed(r):
                if any(s<=now<e for s,e in ev[r]): return None
                if w.set[r]>=21.0-0.01: return "main"
                if 14.0 < w.set[r] < 21.0: return "pre"
                return None
            inputs=[PL.RoomInput(name=r,params=(TRUE[r] if truth else lr.room_params(r,PRIOR[r])),temp=w.reading(r),comfort=21.0,setback=14.0,priority=prio.get(r,5),safety=(1.0 if truth else 1.1),events=[e for e in ev[r] if e[1]>now],committed=committed(r)) for r in ROOMS]
            t0=time.time()
            sig=PL.plan_signature(inputs,now)
            if res is None or sig!=last_sig or not PL.plan_still_valid(inputs,res,plant,gauge,lambda t: w.outside(t),now):
                res=PL.plan_heating(inputs,plant,gauge,lambda t: w.outside(t),now); last_sig=sig; nreplan+=1
            plans=res.plans
            tsum+=time.time()-t0; nplan+=1
            if m==8*60:
                f=lambda d: d.strftime('%H:%M') if d else '-'
                print("  Plan um 00:00, Absenkfenster",f(res.idle.start),f(res.idle.end))
                for r,p in plans.items():
                    if p.deadline: print(f"    {r[:22]:22} Vorstufe {f(p.pre)} {p.pre_level or ''} Komfort {f(p.main)} warm {f(p.reach)} Frist {f(p.deadline)}")
            for inp in inputs:
                sp=PL.setpoint_at(inp,plans.get(inp.name,PL.RoomPlan()),now,res.idle)
                if sp!=w.set[inp.name]: cmds.append((now.strftime("%H:%M"),inp.name[:12],sp))
                w.set[inp.name]=sp
        w.step(1)
        if w.t.minute%5==0:
            D=sum(lr.room_params(r,PRIOR[r]).w*w.valves[r] for r in ROOMS)
            P.plant_step(D,lr.plant_params(P.PlantParams()),gauge,5/60)
        for r in ROOMS:
            if not ev[r]: continue
            if w.temps[r]>=20.5:
                since.setdefault(r,w.t)
            else:
                since.pop(r,None)
            if w.t==ev[r][0][0]-timedelta(minutes=15):
                reach[r]=(since.get(r),w.temps[r])
    print("==",label,"aussen",round(w.outside(day+timedelta(hours=7)),1),"Planung avg ms",round(tsum/nplan*1000,1),"Neuplanungen",nreplan)
    for r in ROOMS:
        if not ev[r]: continue
        dl=ev[r][0][0]-timedelta(minutes=15)
        si,temp=reach[r]
        print(f"  {r[:22]:22} Frist {dl.strftime('%H:%M')} {temp:5.1f} °C -> {'OK      ' if si else 'ZU KALT '} warm seit {si.strftime('%H:%M') if si else '-    '} ({round((dl-si).total_seconds()/60) if si else 0} min Reserve)")
    print("  Energie (Kessel-h):",round(w.energy,1))
    print("  Befehle:",cmds)
import sys
truth="truth" in sys.argv
run_day(0,label="normal",truth=truth)
run_day(-8,label="kalt",truth=truth)
run_day(+6,label="mild",truth=truth)
