#!/usr/bin/env python3
"""Vista da Terra: coletor experimental NOAA Washington VAAC (IWXXM 3.0).

Totalmente isolado do coletor HANS/GeoNet/Smithsonian.
Não lê previsões +6/+12/+18 h nem altera os contadores da live.
"""
import argparse
import datetime as dt
from html.parser import HTMLParser
import json
import math
import os
import re
import sys
import tempfile
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

UTC = dt.timezone.utc
PAGE = "https://prod-east-goes.woc.noaa.gov/products/atmosphere/vaac/messages.html"
HOST = "prod-east-goes.woc.noaa.gov"
UA = "VistaDaTerra-VAAC-Experimental/1.0 (github.com/paulofloripa2006-cloud/vista-da-terra-vulcoes)"
IWXXM = "http://icao.int/iwxxm/3.0"
GML = "http://www.opengis.net/gml/3.2"
AIXM = "http://www.aixm.aero/schema/5.1.1"
METCE = "http://def.wmo.int/metce/2013"
LIMIT = 40
BUDGET = 240
RESERVE = 60
GRACE_H = 3
FALLBACK_H = 9

def utcnow():
    return dt.datetime.now(UTC).replace(microsecond=0)

def stamp(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

def date(s):
    try:
        d = dt.datetime.fromisoformat(s.replace("Z","+00:00"))
        return d.astimezone(UTC) if d.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None

def txt(root, path):
    el = root.find(path)
    return el.text.strip() if el is not None and el.text else None

def fetch(url, timeout=15, maxbytes=700000):
    u=urlsplit(url)
    if u.scheme!="https" or u.hostname!=HOST:
        raise ValueError("URL de origem nao permitida")
    headers={"User-Agent":UA,"Accept":"text/xml,application/xml,text/html"}
    with urlopen(Request(url,headers=headers), timeout=timeout) as r:
        b=r.read(maxbytes+1)
        if len(b)>maxbytes:
            raise ValueError("Resposta excedeu limite de bytes")
        return b

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section=0
        self.urls=[]
    def handle_data(self,data):
        if "Advisories from the past 24 hours" in data:
            self.section=1
        elif "Advisories from the past 15 days" in data:
            self.section=2
    def handle_starttag(self,tag,attrs):
        if tag!="a" or self.section!=1: return
        href=dict(attrs).get("href","")
        if not re.search(r"\.xml(?:\?.*)?$",href,re.I): return
        url=urljoin(PAGE,href)
        p=urlsplit(url)
        if (p.scheme=="https" and p.hostname==HOST and
            re.fullmatch(r"/products/atmosphere/vaac/volcanoes/xml_files/FVXX[0-9A-Z]+_\d{8}_\d{4}\.xml",p.path,re.I)):
            self.urls.append(url)

def page_links(html):
    parser=Links()
    parser.feed(html)
    if parser.section<2:
        raise ValueError("Secoes NOAA nao localizadas; layout pode ter mudado")
    links=sorted(set(parser.urls))
    if not links:
        raise ValueError("Secao de 24 horas sem links XML")
    return links

def polygon(poslist):
    if poslist is None or not poslist.text:
        raise ValueError("GML posList ausente")
    if poslist.get("srsDimension","2")!="2":
        raise ValueError("Coordenadas GML nao sao bidimensionais")
    vals=[float(v) for v in poslist.text.split()]
    if len(vals)<8 or len(vals)%2:
        raise ValueError("Anel com numero de coordenadas invalido")
    pts=[vals[i:i+2] for i in range(0,len(vals),2)]
    if any(not(math.isfinite(a) and math.isfinite(b) and abs(a)<=90 and abs(b)<=180) for a,b in pts):
        raise ValueError("Latitude/longitude fora dos limites")
    if pts[0]!=pts[-1] or len({tuple(x) for x in pts[:-1]})<3:
        raise ValueError("Anel aberto ou menos de 3 vertices distintos")
    center=sum(a for a,b in pts[:-1])/len(pts[:-1])
    xyz=[]
    for lat,lon in pts:
        if xyz:
            while lon-xyz[-1][2]>180: lon-=360
            while lon-xyz[-1][2]<-180: lon+=360
        xyz.append((math.radians(lon)*6371*math.cos(math.radians(center)),math.radians(lat)*6371,lon))
    area=abs(sum(xyz[i][0]*xyz[i+1][1]-xyz[i+1][0]*xyz[i][1] for i in range(len(xyz)-1)))/2
    if area<0.01: raise ValueError("Poligono com area degenerada")
    return [[round(a,5),round(b,5)] for a,b in pts],round(area,3)

def layer(cloud):
    vol=cloud.find(".//{%s}AirspaceVolume"%AIXM)
    if vol is None: raise ValueError("AshCloud sem volume")
    up=vol.find("{%s}upperLimit"%AIXM)
    low=vol.find("{%s}lowerLimit"%AIXM)
    if up is None or up.get("uom")!="FL" or not up.text:
        raise ValueError("FL superior ausente")
    high=float(up.text)
    if low is not None and low.text and low.text.strip().upper() in ("GND","SFC"):
        bottom=0
    elif low is not None and low.get("uom")=="FL" and low.text:
        bottom=float(low.text)
    else:
        raise ValueError("FL inferior ausente")
    if not 0<=bottom<=high<=1000:
        raise ValueError("Limites FL invalidos")
    rings=vol.findall(".//{%s}LinearRing/{%s}posList"%(GML,GML))
    if len(rings)!=1: raise ValueError("Quantidade de aneis GML inesperada")
    pts,area=polygon(rings[0])
    dr=txt(cloud,"{%s}directionOfMotion"%IWXXM)
    sp=txt(cloud,"{%s}speedOfMotion"%IWXXM)
    dre=cloud.find("{%s}directionOfMotion"%IWXXM)
    spe=cloud.find("{%s}speedOfMotion"%IWXXM)
    direction=float(dr) if dr and dre is not None and dre.get("uom")=="deg" else None
    speed=float(sp) if sp and spe is not None and spe.get("uom") in ("[kn_i]","KT") else None
    if direction is not None and not 0<=direction<=360: direction=None
    if speed is not None and not 0<=speed<=300: speed=None
    return {"lowerFL":bottom,"upperFL":high,"polygon":pts,
            "vertices":len(pts)-1,"areaKm2":area,"motionDeg":direction,"speedKt":speed}

def parse_xml(body,url,now,catalog=None):
    if len(body)>1000000 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("XML excessivo ou entidades nao aceitas")
    root=ET.fromstring(body)
    if root.tag!="{%s}VolcanicAshAdvisory"%IWXXM:
        raise ValueError("Versao XML nao aceita (somente IWXXM 3.0)")
    w="{%s}"%IWXXM
    issue=date(txt(root,w+"issueTime/.//{%s}timePosition"%GML))
    if issue is None or issue>now+dt.timedelta(minutes=15):
        raise ValueError("Horário de emissao ausente ou futuro")
    name=txt(root,w+"volcano/.//{%s}name"%METCE)
    if not name: raise ValueError("Nome do vulcao ausente")
    match=re.search(r"(?<!\d)(\d{6})(?!\d)",name)
    gvp=match.group(1) if match else None
    valid=bool(gvp and (catalog is None or gvp in catalog))
    # UNKNOWN/UNNAMED ficam isolados por arquivo, nunca fundidos.
    vk=gvp if valid else "unverified:"+re.sub(r"[^a-z0-9]+","-",name.lower()).strip("-")+":"+url.split("/")[-1]
    point=txt(root,w+"volcano/.//{%s}Point/{%s}pos"%(GML,GML))
    coords=None
    if point:
        try:
            a,b=[float(x) for x in point.split()]
            if math.isfinite(a) and math.isfinite(b) and abs(a)<=90 and abs(b)<=180:
                coords=[round(a,5),round(b,5)]
        except (ValueError,TypeError): pass
    num=txt(root,w+"advisoryNumber")
    if not num: raise ValueError("Numero do aviso ausente")
    conditions=root.find(w+"observation/"+w+"VolcanicAshObservedOrEstimatedConditions")
    status=conditions.get("status","UNKNOWN") if conditions is not None else "ABSENT"
    obstime=None
    estimated=None
    layers=[]
    layer_errors=[]
    if conditions is not None:
        obstime=date(txt(conditions,w+"phenomenonTime/.//{%s}timePosition"%GML))
        for c in conditions.findall(w+"ashCloud/"+w+"VolcanicAshCloudObservedOrEstimated"):
            if c.get("isEstimated") is not None:
                v=c.get("isEstimated").lower() in ("true","1")
                estimated=v if estimated is None else (estimated or v)
            try:
                layers.append(layer(c))
            except (ValueError,OverflowError) as e:
                layer_errors.append(str(e))
    if layer_errors and not layers and status.upper() in ("IDENTIFIABLE","PROVIDED"):
        raise ValueError("Todas as camadas invalidas: "+"; ".join(layer_errors)[:160])
    nextel=root.find(w+"nextAdvisoryTime")
    nexttime=date(txt(root,w+"nextAdvisoryTime/.//{%s}timePosition"%GML))
    nil=bool(nextel is not None and any(k.endswith("nilReason") and v for k,v in nextel.attrib.items()))
    remarks=txt(root,w+"remarks")
    final=nil or bool(re.search(r"NO FURTHER ADVISOR(?:Y|IES)|FINAL ADVISORY",remarks or "",re.I))
    expiry=(nexttime+dt.timedelta(hours=GRACE_H)) if nexttime and nexttime>issue else issue+dt.timedelta(hours=FALLBACK_H)
    key="WASHINGTON|%s|%s|%s"%(vk,num,stamp(issue))
    return {
        "key":key,"volcanoKey":vk,
        "volcano":{"name":name,"gvp":gvp,"gvpValid":valid,"lat":coords[0] if coords else None,
                   "lon":coords[1] if coords else None,"area":txt(root,w+"stateOrRegion"),"summitFt":None},
        "advisory":{"number":num,"reportStatus":root.get("reportStatus","UNKNOWN"),
                    "issueUtc":stamp(issue),"observationUtc":stamp(obstime) if obstime else None,
                    "nextAdvisoryUtc":stamp(nexttime) if nexttime else None,
                    "expiresAtUtc":stamp(expiry),"expiryFallback":not bool(nexttime and nexttime>issue),
                    "fetchedUtc":stamp(now),"final":final,"replaces":None,"xmlUrl":url,
                    "infoSource":txt(root,w+"informationSource"),
                    "eruptionDetailsRaw":txt(root,w+"eruptionDetails"),"remarksRaw":remarks},
        "cloud":{"status":status,"isEstimated":estimated,"layers":layers,
                 "rejectedLayerReasons":layer_errors}
    }

def new_doc():
    return {"schema":"vaac-exp-1","experimental":True,
            "source":{"name":"NOAA Washington VAAC","url":PAGE},
            "collector":{},"rules":{"toleranceHours":GRACE_H,"acceptedIWXXM":["3.0"],"seenRetentionDays":15},
            "active":{},"closed":[],"rejected":[],"seen":{},"lastState":{}}

def load(path):
    if not os.path.exists(path): return new_doc()
    with open(path,encoding="utf8") as f: data=json.load(f)
    if data.get("schema")!="vaac-exp-1" or not isinstance(data.get("active"),dict) or not isinstance(data.get("seen"),dict):
        raise ValueError("JSON existente invalido: nao sobrescrever")
    return data

def close(doc,key,row,why,now):
    doc["active"].pop(key,None)
    doc["closed"].append({"volcanoKey":key,"key":row["key"],"reason":why,
                          "closedAtUtc":stamp(now),"issueUtc":row["advisory"]["issueUtc"]})

def apply(doc,row,now):
    key=row["volcanoKey"]
    issue=date(row["advisory"]["issueUtc"])
    latest=doc.setdefault("lastState",{}).get(key)
    if latest and issue <= date(latest["issueUtc"]): return "anterior"
    prior=doc["active"].get(key)
    if prior:
        close(doc,key,prior,"substituido",now)
        row["advisory"]["replaces"]=prior["key"]
    elif latest:
        row["advisory"]["replaces"]=latest["key"]
    isfinal=row["advisory"]["final"]
    expired=date(row["advisory"]["expiresAtUtc"])<=now
    doc["lastState"][key]={"key":row["key"],"issueUtc":row["advisory"]["issueUtc"],"final":isfinal}
    if isfinal or expired:
        doc["closed"].append({"volcanoKey":key,"key":row["key"],
                              "reason":"encerramento" if isfinal else "expirado",
                              "closedAtUtc":stamp(now),"issueUtc":row["advisory"]["issueUtc"]})
        doc["active"].pop(key,None)
        return "fechado"
    doc["active"][key]=row
    return "ativo"

def prune(doc,now):
    for key,row in list(doc["active"].items()):
        if (date(row["advisory"]["expiresAtUtc"]) or dt.datetime.min.replace(tzinfo=UTC))<=now:
            close(doc,key,row,"expirado",now)
    cutoff=now-dt.timedelta(hours=48)
    doc["closed"]=[r for r in doc.get("closed",[]) if (date(r.get("closedAtUtc")) or cutoff)>=cutoff]
    doc["rejected"]=[r for r in doc.get("rejected",[]) if (date(r.get("atUtc")) or cutoff)>=cutoff]
    cutoff=now-dt.timedelta(days=15)
    doc["seen"]={k:v for k,v in doc["seen"].items() if (date(v.get("atUtc")) or cutoff)>=cutoff}
    doc["lastState"]={k:v for k,v in doc.get("lastState",{}).items()
                      if (date(v.get("issueUtc")) or cutoff)>=cutoff}

def write_atomic(path,doc):
    folder=os.path.dirname(os.path.abspath(path))
    os.makedirs(folder,exist_ok=True)
    content=json.dumps(doc,ensure_ascii=False,separators=(",",":"),sort_keys=True)+"\n"
    if json.loads(content)["schema"]!="vaac-exp-1":
        raise ValueError("Falha validacao JSON")
    fd,tmp=tempfile.mkstemp(dir=folder,prefix=".vaac-",suffix=".tmp")
    try:
        with os.fdopen(fd,"w",encoding="utf8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp,encoding="utf8") as f: json.load(f)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def run(out,ids=None):
    started=time.monotonic()
    now=utcnow()
    doc=load(out)
    prune(doc,now)
    last_ok=doc.get("collector",{}).get("lastSuccessUtc")
    counts={"found":0,"processed":0,"pending":0,"accepted":0,"rejected":0,"expired":0,"closed":0}
    result="OK"
    err=None
    try:
        links=page_links(fetch(PAGE).decode("utf8","replace"))
        counts["found"]=len(links)
        pending=[u for u in links if u not in doc["seen"]]
        for i,url in enumerate(pending):
            if counts["processed"]>=LIMIT or time.monotonic()-started>BUDGET-RESERVE:
                counts["pending"]=len(pending)-i
                result="PARCIAL"
                break
            counts["processed"]+=1
            try:
                row=parse_xml(fetch(url,15,1000000),url,now,ids)
                event=apply(doc,row,now)
                counts["accepted"]+=1
                if event=="fechado": counts["closed"]+=1
                doc["seen"][url]={"key":row["key"],"atUtc":stamp(now),"result":event}
            except Exception as e:
                doc["rejected"].append({"xmlUrl":url,"reason":(type(e).__name__+": "+str(e))[:240],
                                        "atUtc":stamp(now)})
                counts["rejected"]+=1
                result="PARCIAL"
        last_ok=stamp(now)
    except Exception as e:
        result="FALHA"
        err=(type(e).__name__+": "+str(e))[:240]
    prune(doc,now)
    counts["expired"]=sum(r.get("reason")=="expirado" and r.get("closedAtUtc")==stamp(now) for r in doc["closed"])
    newest=max((r["advisory"]["issueUtc"] for r in doc["active"].values()),default=None)
    doc["collector"]={"lastAttemptUtc":stamp(now),"lastSuccessUtc":last_ok,"result":result,
                      "error":err,"counts":counts,"newestIssueUtc":newest,
                      "elapsedSeconds":round(time.monotonic()-started,2)}
    write_atomic(out,doc)
    print(json.dumps(doc["collector"],ensure_ascii=False),flush=True)
    return result!="FALHA"

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out",required=True)
    ap.add_argument("--catalog",default="data/gvp_catalogo.json")
    args=ap.parse_args()
    ids=None
    try:
        with open(args.catalog,encoding="utf8") as f:
            catalog=json.load(f)
        ids={str(x["vnum"]) for x in catalog.get("items",[]) if re.fullmatch(r"\d{6}",str(x.get("vnum","")))}
    except (OSError,ValueError,KeyError) as e:
        print("Catalogo GVP nao disponivel:",e,flush=True)
    if not run(args.out,ids): sys.exit(1)

if __name__=="__main__":
    main()
