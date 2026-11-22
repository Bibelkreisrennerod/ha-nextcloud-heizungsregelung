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
# Traegheit der Heizkoerper (h): Saal wie am 20.11.2026 gemessen (Boost: 10 min Totzeit, 35 min Traegheit)
_TAU={"Saal":0.58,"Nebenraum (groß)":0.3,"Nebenraum (klein)":0.25,"Ehemaliger Sonntagsschulraum":0.25,"Küche":0.2,"Vorraum Küche 2":0.2}
_EFF["Saal"]=(0.035,3.2,1.3,0.17)
# Ventil -> Waerme (gamma) und Waermeaustausch mit Nachbarn bei offener Tuer (k, 1/h)
_GAMMA={"Saal":0.8,"Nebenraum (groß)":0.75,"Nebenraum (klein)":0.85,"Ehemaliger Sonntagsschulraum":0.8,"Küche":0.9,"Vorraum Küche 2":0.8}
_K={"Saal":0.08,"Küche":0.45,"Nebenraum (groß)":0.15,"Nebenraum (klein)":0.2,"Ehemaliger Sonntagsschulraum":0.15,"Vorraum Küche 2":0.25}
GROUPS={"Saal":"Gruppe 1","Küche":"Gruppe 1","Nebenraum (groß)":"Gruppe 2","Nebenraum (klein)":"Gruppe 2","Ehemaliger Sonntagsschulraum":"Gruppe 2","Vorraum Küche 2":"Gruppe 2"}
# b und w gelten bei der Hoechstoeffnung (wie bisher): je Einheit Waermeanteil umrechnen
TRUE={r:P.RoomParams(a=a,b=b/TOP[r]**_GAMMA[r],w=w/TOP[r]**_GAMMA[r],d=d,curve=true_curve(TOP[r]),tau=_TAU[r],gamma=_GAMMA[r],k=_K[r]) for r,(a,b,w,d) in _EFF.items()}
# Tuer Saal-Kueche: meist offen; zu in diesen Stunden ab Simulationsbeginn (wie bei Gottesdiensten)
DOOR_CLOSED=[(24,27),(56,60)]
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
        s.t=start; s.start=start; s.boiler=0.0; s.temps={r:14.0 for r in ROOMS}; s.set={r:14.0 for r in ROOMS}; s.state=P.PlantState(TPLANT.e_buf); s.valves={r:0.0 for r in ROOMS}
        s.integral={r:0.0 for r in ROOMS}
        s.offset={r:0.0 for r in ROOMS}  # Temperatur-Offset am Thermostat: verschiebt Anzeige und Regelung
        s.bias={r:0.0 for r in ROOMS}  # Fuehler misst so viel zu warm (z. B. nah am Heizkoerper); der Offset gleicht das aus
        s.delays={r:P.Delay(TRUE[r].d,1/60,lag=TRUE[r].tau) for r in ROOMS}
    def outside(s,t):  # Tagesgang
        h=(t.hour+t.minute/60); return 3+4*math.sin((h-9)/24*2*math.pi)
    def door(s,group):
        if group!="Gruppe 1": return 1
        hours=(s.t-s.start).total_seconds()/3600
        return 0 if any(a<=hours<b for a,b in DOOR_CLOSED) else 1
    def step(s,minutes=1):
        dt=minutes/60
        for r in ROOMS:
            target,s.integral[r]=pi_valve(r,s.set[r],s.temps[r]+s.bias[r]+s.offset[r],s.integral[r],dt)
            s.valves[r]+=(target-s.valves[r])*min(1.0,dt/VALVE_LAG)
        D=sum(TRUE[r].demand(s.valves[r]) for r in ROOMS)
        was_on,on_for=s.state.boiler_on,s.state.on_for
        sup,share=P.plant_step(D,TPLANT,s.state,dt)
        s.boiler=1.0 if was_on and on_for>=TPLANT.t_start else 0.0  # gemessene Kesselleistung (Anteil)
        s.flow=20+50*share
        s.energy=getattr(s,'energy',0.0)+sup*dt
        out=s.outside(s.t)
        before=dict(s.temps)
        for r in ROOMS:
            g=GROUPS[r]; others=[before[x] for x in ROOMS if x!=r and GROUPS[x]==g]
            ex=TRUE[r].k*s.door(g)*(sum(others)/len(others)-before[r]) if others else 0.0
            s.temps[r]=P.room_step(before[r],TRUE[r],s.delays[r].push(TRUE[r].heat(s.valves[r])*share),out,dt,ex)
        s.t+=timedelta(minutes=minutes)
    def reading(s,r):  # 0,1 K Aufloesung wie Homematic, mit Offset wie angezeigt
        return round((s.temps[r]+s.bias[r]+s.offset[r])*10)/10
class Gauge:
    def __init__(s,learner,plant0): s.l=learner; s.p0=plant0; s.state=None
    def update(s,valves,prior):
        plant=s.l.plant_params(s.p0)
        if s.state is None: s.state=P.PlantState(plant.e_buf)
        D=sum(s.l.room_params(r,prior[r]).demand(valves[r]) for r in ROOMS)
        _,s.share=P.plant_step(D,plant,s.state,5/60)
        s.state.e=min(s.state.e,plant.e_buf)
        return s.state.e/plant.e_buf if plant.e_buf>0 else 0
PRIOR={r:(240.0 if r=="Saal" else 120.0) for r in ROOMS}
