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

ROOMS={"Saal":("saal",5,P.RoomParams(a=0.035,b=3.2,w=1.3)),
       "Nebenraum (groß)":("ng",1,P.RoomParams(a=0.07,b=4.0,w=0.35)),
       "Küche":("kuche",1,P.RoomParams(a=0.09,b=4.0,w=0.2))}
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
def publish():
    for r,(aid,n,_) in ROOMS.items():
        e=f"climate.{aid}"
        hass.states[e]=State(hass.modes.get(e,"auto"),{"current_temperature":round(temps[r],1),"temperature":hass.setpoints[e]})
        v=P.valve(hass.setpoints[e],temps[r])
        for i in range(n): hass.states[f"sensor.{aid}_heizkorper_{i}_heizen"]=State(str(round(v*100)),{"unit_of_measurement":"%"})
    hass.states["weather.home"]=State("rainy",{"temperature":round(outside_at(stubs.NOW[0]),1)})
    hass.states["sensor.flow"]=State(str(round(20+50*SHARE[0],1)),{})
    hass.states["sensor.hmip_duty_cycle"]=State("10",{})
def physics():
    dt=1/60
    vs={r:P.valve(hass.setpoints[f"climate.{aid}"],temps[r]) for r,(aid,_,_) in ROOMS.items()}
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
    stubs.NOW[0]=datetime(2026,11,18,12,0,tzinfo=timezone.utc)
    rt.coordinator.data=Data([],list(ROOMS))
    await c.async_start()
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
asyncio.run(main(sys.argv[1] if len(sys.argv)>1 else "auto"))
