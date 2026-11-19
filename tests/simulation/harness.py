"""Gemeinsame Testumgebung: wahre Physik eines Gebaeudes (fuer Simulationen)."""
import sys, types, importlib, math, random
from datetime import datetime, timedelta, timezone
pk=types.ModuleType("nr"); import os
HERE=os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
pk.__path__=[os.path.join(HERE,"..","..","custom_components","nextcloud_raumbelegung")]; sys.modules["nr"]=pk
P=importlib.import_module("nr.physics"); L=importlib.import_module("nr.learning"); PL=importlib.import_module("nr.planning")
ROOMS=["Saal","Nebenraum (groß)","Nebenraum (klein)","Ehemaliger Sonntagsschulraum","Küche","Vorraum Küche 2"]
# Wahrheit: Saal gross, zieht 1.3 Kesselleistungen, allein ca. 4 h
# Totzeit d: Saal 25 min (Wandthermostat weit weg von den Konvektoren), sonst 5-10 min
TRUE={"Saal":P.RoomParams(a=0.035,b=3.2,w=1.3,d=0.42),"Nebenraum (groß)":P.RoomParams(a=0.07,b=4.0,w=0.35,d=0.15),
 "Nebenraum (klein)":P.RoomParams(a=0.08,b=5.0,w=0.25,d=0.1),"Ehemaliger Sonntagsschulraum":P.RoomParams(a=0.07,b=4.5,w=0.25,d=0.1),
 "Küche":P.RoomParams(a=0.09,b=4.0,w=0.2,d=0.08),"Vorraum Küche 2":P.RoomParams(a=0.1,b=3.5,w=0.15,d=0.08)}
TPLANT=P.PlantParams(e_buf=0.7,t_start=0.3,p_max_kw=30)
class World:
    def __init__(s,start):
        s.t=start; s.temps={r:14.0 for r in ROOMS}; s.set={r:14.0 for r in ROOMS}; s.state=P.PlantState(TPLANT.e_buf); s.valves={r:0.0 for r in ROOMS}
        s.delays={r:P.Delay(TRUE[r].d,1/60) for r in ROOMS}
    def outside(s,t):  # Tagesgang
        h=(t.hour+t.minute/60); return 3+4*math.sin((h-9)/24*2*math.pi)
    def step(s,minutes=1):
        dt=minutes/60
        s.valves={r:P.valve(s.set[r],s.temps[r]) for r in ROOMS}
        D=sum(TRUE[r].w*s.valves[r] for r in ROOMS)
        sup,share=P.plant_step(D,TPLANT,s.state,dt)
        s.flow=20+50*share
        s.energy=getattr(s,'energy',0.0)+sup*dt
        out=s.outside(s.t)
        for r in ROOMS:
            s.temps[r]=P.room_step(s.temps[r],TRUE[r],s.delays[r].push(s.valves[r]*share),out,dt)
        s.t+=timedelta(minutes=minutes)
    def reading(s,r):  # 0,1 K Aufloesung wie Homematic
        return round(s.temps[r]*10)/10
class Gauge:
    def __init__(s,learner,plant0): s.l=learner; s.p0=plant0; s.state=None
    def update(s,valves,prior):
        plant=s.l.plant_params(s.p0)
        if s.state is None: s.state=P.PlantState(plant.e_buf)
        D=sum(s.l.room_params(r,prior[r]).w*valves[r] for r in ROOMS)
        _,s.share=P.plant_step(D,plant,s.state,5/60)
        s.state.e=min(s.state.e,plant.e_buf)
        return s.state.e/plant.e_buf if plant.e_buf>0 else 0
PRIOR={r:(240.0 if r=="Saal" else 120.0) for r in ROOMS}
