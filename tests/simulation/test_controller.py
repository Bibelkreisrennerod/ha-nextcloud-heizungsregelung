import json
"""Steuerung v0.3 im Zusammenspiel: Physik aus tests2/harness, HA-Stubs."""
import sys, asyncio, importlib, random, math
from datetime import datetime, timedelta, timezone
import os
HERE=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,HERE)
import ha_stubs as stubs
from ha_stubs import *
import types
pk=types.ModuleType("nextcloud_raumbelegung"); pk.__path__=[os.path.join(HERE,"..","..","custom_components","nextcloud_raumbelegung")]; sys.modules["nextcloud_raumbelegung"]=pk
ctl=importlib.import_module("nextcloud_raumbelegung.controller")
cal=importlib.import_module("nextcloud_raumbelegung.caldav")
P=importlib.import_module("nextcloud_raumbelegung.physics")

def _curve(top,zero=-0.4,at0=0.55,full=3.0):  # wie harness.true_curve
    return tuple(top*(0.0 if x<=zero else (at0*(x-zero)/(-zero) if x<0 else min(1.0,at0+(1-at0)*x/full))) for x in P.CURVE_DEV)
ROOMS={"Saal":("saal",5,P.RoomParams(a=0.035,b=3.2/0.48,w=1.3/0.48,curve=_curve(0.48))),
       "Nebenraum (groß)":("ng",1,P.RoomParams(a=0.07,b=4.0/0.83,w=0.35/0.83,curve=_curve(0.83))),
       "Küche":("kuche",1,P.RoomParams(a=0.09,b=4.0/0.75,w=0.2/0.75,curve=_curve(0.75)))}
TPLANT=P.PlantParams(e_buf=0.7,t_start=0.3)
ents=[Ent("sensor.hmip_duty_cycle",None)]
for r,(aid,n,_) in ROOMS.items():
    ents.append(Ent(f"climate.{aid}",aid))
    for i in range(n): ents.append(Ent(f"sensor.{aid}_heizkorper_{i}_heizen",aid))
REG["ar"]=AR([Area(aid,r) for r,(aid,_,_) in ROOMS.items()])
REG["er"]=ER(ents)
hass=Hass()
temps={r:14.0 for r in ROOMS}; state=P.PlantState(TPLANT.e_buf); SHARE=[1.0]
for r,(aid,_,_) in ROOMS.items(): hass.setpoints[f"climate.{aid}"]=14.0
def outside_at(t): return hass.base+4*math.sin((t.hour+t.minute/60-9)/24*2*math.pi)
UNAVAILABLE=set()  # Klima-Entitaeten, die gerade "unavailable" melden (z. B. nach Neustart)
def publish():
    for r,(aid,n,_) in ROOMS.items():
        e=f"climate.{aid}"
        if e in UNAVAILABLE:
            hass.states[e]=State("unavailable",{}); continue
        hass.states[e]=State(hass.modes.get(e,"auto"),{"current_temperature":round(temps[r],1),"temperature":hass.setpoints[e]})
        v=VALVES.get(r,0.0)
        for i in range(n): hass.states[f"sensor.{aid}_heizkorper_{i}_heizen"]=State(str(round(v*100)),{"unit_of_measurement":"%"})
    hass.states["weather.home"]=State("rainy",{"temperature":round(outside_at(stubs.NOW[0]),1)})
    hass.states["sensor.flow"]=State(str(round(20+50*SHARE[0],1)),{})
    hass.states["sensor.hmip_duty_cycle"]=State("10",{})
    if RECORD[0]:
        for e,st in hass.states.items():
            hist=stubs.RECORDED.setdefault(e,[])
            if not hist or hist[-1].state!=st.state or hist[-1].attributes!=st.attributes:
                hist.append(State(st.state,dict(st.attributes),stubs.NOW[0]))
RECORD=[False]
VALVES={}; INTEGRAL={r:0.0 for r in ROOMS}; KI=0.15
def physics():
    dt=1/60
    for r,(aid,_,prm) in ROOMS.items():
        # Thermostat mit P-Anteil (Kennlinie) und I-Anteil, begrenzt auf die Hoechstoeffnung
        sp=hass.setpoints[f"climate.{aid}"]; top=max(prm.curve); dev=sp-temps[r]; p=P.valve(sp,temps[r],prm.curve)
        if not (dev>0 and p+INTEGRAL[r]>=top): INTEGRAL[r]=min(max(INTEGRAL[r]+KI*dev*dt,0.0),top)
        VALVES[r]=min(max(p+INTEGRAL[r],0.0),top)
    vs=dict(VALVES)
    D=sum(ROOMS[r][2].w*vs[r] for r in ROOMS)
    sup,share=P.plant_step(D,TPLANT,state,dt); SHARE[0]=share
    for r in ROOMS: temps[r]=P.room_step(temps[r],ROOMS[r][2],vs[r]*share,outside_at(stubs.NOW[0]),dt)
class Entry:
    entry_id="e1"; options={"flow_sensor":"sensor.flow"}
class Coord:
    last_update_success=True
    def __init__(s): s.data=None
class Data:
    def __init__(s,events,rooms): s.events=events; s.rooms=rooms
class RT:
    def __init__(s): s.coordinator=Coord(); s.settings={}; s.controller=None
rt=RT(); rt.settings[("Saal","preheat")]=240.0; rt.settings[("Saal","priority")]=1.0; rt.settings[("Nebenraum (groß)","priority")]=2.0
c=ctl.HeatingController(hass,Entry(),rt)
hass.base=2.0
async def minutes(n, quiet=False):
    for _ in range(n):
        stubs.NOW[0]+=timedelta(minutes=1); publish()
        await c._async_tick(stubs.NOW[0]); physics(); await asyncio.sleep(0)
def cmds(since):
    return [(t.strftime("%a %H:%M"),d["entity_id"][0].split(".")[1],d.get("temperature",d.get("hvac_mode"))) for t,dom,s,d in hass.services.calls if dom=="climate" and t>=since]
async def main(mode):
    global c
    stubs.NOW[0]=datetime(2026,11,18,12,0,tzinfo=timezone.utc)
    rt.coordinator.data=Data([],list(ROOMS))
    if mode=="history":
        # 3 Tage Homematic-Profil ohne Integration aufzeichnen (wie der Recorder)
        RECORD[0]=True
        for _ in range(3*24*60):
            t=stubs.NOW[0]+timedelta(minutes=1); stubs.NOW[0]=t
            hass.setpoints["climate.saal"]=21.0 if 16<=t.hour<20 else 14.0
            hass.setpoints["climate.kuche"]=21.0 if 9<=t.hour<12 else 14.0
            hass.setpoints["climate.ng"]=21.0 if 17<=t.hour<19 else 14.0
            publish(); physics()
        RECORD[0]=False
    await c.async_start()
    for cb in list(stubs.CALL_LATER): await cb(stubs.NOW[0])
    if mode=="history":
        for r in ROOMS:
            p=c.room_params(r); t=ROOMS[r][2]; st=c.learner.room_stats(r)
            print(f"{r[:18]:18} a {p.a:.3f}/{t.a:.3f} b {p.b:.2f}/{t.b:.2f} w {p.w:.2f}/{t.w:.2f} d {p.d*60:.0f}/{t.d*60:.0f} min  Auskuehlfenster {st.cool_windows} Heizfenster {st.heat_windows} kalibriert {c.learner.calibrated(r)}")
        print("Heizphasen",c.learner.plant.episodes,"Puffer",round(c.plant_params().e_buf,2),"Notiz:",stubs.NOTES.get("nextcloud_raumbelegung_history"))
        print("gespeichert bootstrapped:",stubs.Store.DATA["nextcloud_raumbelegung.e1"]["bootstrapped"])
        return
    if mode=="observe":
        rt.settings[(None,"automatic")]=0.0
        # Homematic-Profil: Saal Mi 18-20 Uhr auf 21
        for _ in range(12*60):
            t=stubs.NOW[0]; hass.setpoints["climate.saal"]=21.0 if 17<=t.hour<20 else 14.0
            await minutes(1)
        print("Befehle bei Automatik aus:",cmds(datetime(2026,11,1,tzinfo=timezone.utc)))
        print("Protokoll Saal:",c.history.get("Saal",[])[:1])
        print("Gelernt Saal:",c.room_params("Saal"),"Abkuehlfenster",c.learner.room_stats("Saal").cool_windows)
        return
    if mode=="calib":
        rt.settings[(None,"automatic")]=0.0
        await minutes(30)
        await c.async_start_calibration(["Saal"],None,None,None)
        print("Status:",c.status.get("kalibrierung_status"))
        for _ in range(10*60):
            await minutes(1)
            if c.calibration is None: break
        await asyncio.sleep(0)
        print("Ende",stubs.NOW[0].strftime("%H:%M"),"Notiz:",stubs.NOTES.get("nextcloud_raumbelegung_calibration"))
        await minutes(120)
        print("Auswertung:",c.learner.plant.last_fit)
        print("Befehle:",cmds(datetime(2026,11,1,tzinfo=timezone.utc))[:12],"...",cmds(datetime(2026,11,1,tzinfo=timezone.utc))[-4:])
        print("Modi:",{r:s.mode for r,s in c.rooms.items()})
        return
    rt.settings[(None,"automatic")]=1.0
    if mode=="restart":
        # 1) Kalibrierlauf ueberlebt einen Neustart (Speicher wie in HA als JSON)
        await minutes(100)
        await c.async_start_calibration(["Saal"],None,None,None)
        await minutes(120)
        running=c.calibration is not None and c.calibration.running
        await c.async_stop()
        key="nextcloud_raumbelegung.e1"
        stubs.Store.DATA[key]=json.loads(json.dumps(stubs.Store.DATA[key]))
        c=ctl.HeatingController(hass,Entry(),rt); await c.async_start()
        print("Kalibrierung vor/nach Neustart laeuft:",running,c.calibration is not None and c.calibration.running)
        await c.async_stop_calibration("abgebrochen")
        # 2) Neustart mitten im Aufheizen: der Plan haelt am laufenden Aufheizen fest
        day=datetime(2026,11,22,0,0,tzinfo=timezone.utc)
        ev=[cal.RoomEvent("Gottesdienst",day+timedelta(hours=9),day+timedelta(hours=10,minutes=30),False,["Saal"],"","Gemeinde")]
        rt.coordinator.data=Data(ev,list(ROOMS))
        while stubs.NOW[0]<day+timedelta(hours=9) and c.rooms.get("Saal") is not None and c.rooms["Saal"].mode!="aufheizen" or stubs.NOW[0]<day:
            await minutes(1)
        await minutes(10)
        before=c.rooms["Saal"].mode
        await c.async_stop()
        c=ctl.HeatingController(hass,Entry(),rt); await c.async_start()
        since=stubs.NOW[0]
        # Homematic IP meldet die ersten 3 Minuten nach dem Neustart "nicht verfuegbar"
        UNAVAILABLE.update(f"climate.{aid}" for aid,_,_ in ROOMS.values())
        await minutes(3)
        UNAVAILABLE.clear()
        await minutes(1)
        print("Saal vor/nach Neustart:",before,c.rooms["Saal"].mode,"zugesagt:",c._inputs["Saal"].committed if "Saal" in c._inputs else None,"Handverstellung:",c.rooms["Saal"].manual_until)
        await minutes(30)
        print("Befehle nach Neustart:",[x for x in cmds(since) if x[1]=="saal"])
        # 3) Welche Zusage folgt aus welchem Sollwert (Komfort 21, Absenkung 14)?
        def guess(target,temp,mode="heat"):
            hass.states["climate.saal"]=State(mode,{"current_temperature":temp,"temperature":target})
            return c._committed_after_restart({"climates":["climate.saal"],"target_now":target,"temp":temp},21.0,14.0)
        print("Zusage aus Sollwert:",{"21/17":guess(21,17),"21/20.8":guess(21,20.8),"18 Vorstufe":guess(18,16),"14":guess(14,14),"12 wartend":guess(12,13),"21 Profil":guess(21,17,"auto")})
        return
    # Automatik an: Sonntag Gottesdienst
    day=datetime(2026,11,22,0,0,tzinfo=timezone.utc)
    ev=[cal.RoomEvent("Gottesdienst",day+timedelta(hours=9),day+timedelta(hours=10,minutes=30),False,["Saal","Nebenraum (groß)"],"","Gemeinde"),
        cal.RoomEvent("Mittagessen",day+timedelta(hours=10,minutes=30),day+timedelta(hours=12),False,["Küche"],"","Gemeinde")]
    rt.coordinator.data=Data(ev,list(ROOMS))
    since=stubs.NOW[0]
    while stubs.NOW[0]<day+timedelta(hours=13):
        await minutes(1)
        if stubs.NOW[0]==day+timedelta(hours=8,minutes=45):
            print("08:45 Temperaturen",{r:round(t,1) for r,t in temps.items()})
        if stubs.NOW[0]==day:
            for r,s in c.rooms.items(): print("  Plan",r,{k:(v.strftime('%H:%M') if hasattr(v,'strftime') else v) for k,v in s.status.items() if k in ("pre_start","pre_level","main_start","reach","deadline","lead")})
    print("Befehle:",[x for x in cmds(since) if x[2] not in ("heat",)])
    for r in ROOMS: print("Protokoll",r,{k:v for k,v in (c.history.get(r) or [{}])[0].items() if k in ("heizstart","erreicht_um","abweichung_frist","vorhergesagt","aufheizdauer","gewertet")})
    print("Notizen:",list(stubs.NOTES))
    fs=c.learner.fit_state or {}
    print("Anpassung:",fs.get("rows"),"Messpunkte, Fehler",fs.get("rms"),"K, Empfehlung",(fs.get("experiments") or [{}])[0].get("label"),"Sensor:",{k:c.status.get("fit",{}).get(k) for k in ("rms","zeitpunkt","e_buf_spread")})
asyncio.run(main(sys.argv[1] if len(sys.argv)>1 else "auto"))
