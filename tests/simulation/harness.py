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
# Ventile wie gemessen (19.11.2026): jeder Heizkoerper hat eine eigene Hoechstoeffnung
# (Saal 48 %, Nebenraum gross 83 %), am Sollwert noch gut halb offen, zu ab 0,4 K darueber,
# voll erst 3 K unter Soll. Bewusst anders als die Startform des Modells.
TOP={"Saal":0.48,"Nebenraum (groß)":0.83,"Nebenraum (klein)":0.7,"Ehemaliger Sonntagsschulraum":0.65,"Küche":0.75,"Vorraum Küche 2":0.6}
def true_curve(top,zero=-0.4,at0=0.55,full=3.0):
    pts=[]
    for x in P.CURVE_DEV:
        rel=0.0 if x<=zero else (at0*(x-zero)/(-zero) if x<0 else min(1.0,at0+(1-at0)*x/full))
        pts.append(round(top*rel,4))
    return tuple(pts)
# b und w je Einheit Ventilstellung: bei Hoechstoeffnung wie bisher (Saal 3,2 K/h und 1,3 Kesselleistungen)
_EFF={"Saal":(0.035,3.2,1.3,0.42),"Nebenraum (groß)":(0.07,4.0,0.35,0.15),"Nebenraum (klein)":(0.08,5.0,0.25,0.1),
 "Ehemaliger Sonntagsschulraum":(0.07,4.5,0.25,0.1),"Küche":(0.09,4.0,0.2,0.08),"Vorraum Küche 2":(0.1,3.5,0.15,0.08)}
TRUE={r:P.RoomParams(a=a,b=b/TOP[r],w=w/TOP[r],d=d,curve=true_curve(TOP[r])) for r,(a,b,w,d) in _EFF.items()}
VALVE_LAG=10/60  # h: Stellmotor folgt verzoegert
KI=0.15  # I-Anteil des Homematic-Reglers: Ventil oeffnet je Stunde und Kelvin Abweichung so viel weiter
def pi_valve(r,setpoint,temp,integral,dt):
    """Wahres Thermostat: Kennlinie (P-Anteil) plus I-Anteil, begrenzt auf die Hoechstoeffnung."""
    curve=TRUE[r].curve; top=max(curve); dev=setpoint-temp
    p=P.valve(setpoint,temp,curve)
    if not (dev>0 and p+integral>=top): integral=min(max(integral+KI*dev*dt,0.0),top)
    return min(max(p+integral,0.0),top), integral
TPLANT=P.PlantParams(e_buf=0.7,t_start=0.3,p_max_kw=30)
class World:
    def __init__(s,start):
        s.t=start; s.temps={r:14.0 for r in ROOMS}; s.set={r:14.0 for r in ROOMS}; s.state=P.PlantState(TPLANT.e_buf); s.valves={r:0.0 for r in ROOMS}
        s.integral={r:0.0 for r in ROOMS}
        s.delays={r:P.Delay(TRUE[r].d,1/60) for r in ROOMS}
    def outside(s,t):  # Tagesgang
        h=(t.hour+t.minute/60); return 3+4*math.sin((h-9)/24*2*math.pi)
    def step(s,minutes=1):
        dt=minutes/60
        for r in ROOMS:
            target,s.integral[r]=pi_valve(r,s.set[r],s.temps[r],s.integral[r],dt)
            s.valves[r]+=(target-s.valves[r])*min(1.0,dt/VALVE_LAG)
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
