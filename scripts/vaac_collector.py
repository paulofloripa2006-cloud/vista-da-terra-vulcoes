#!/usr/bin/env python3
"""Vista da Terra: coletor experimental NOAA Washington VAAC (IWXXM 3.0).

Versao corrigida (vaac-exp-1, revisao 5) — 01/11/2026.

- Totalmente isolado do coletor HANS/GeoNet/Smithsonian (scripts/coletor.py).
- Grava somente o arquivo informado em --out (branch vaac-data).
- Nao le previsoes +6/+12/+18 h, nao altera contadores, nao toca vulcoes.json.
- Somente biblioteca padrao do Python.

Formato aceito: envelope WMO MeteorologicalBulletin (collect 2014) contendo
um ou mais meteorologicalInformation/VolcanicAshAdvisory IWXXM 3.0, ou o aviso
IWXXM 3.0 solto. Outro namespace de aviso = quarentena ("versao nao testada").

REGRA OBRIGATORIA PARA QUEM CONSUMIR O JSON: comparar advisory.expiresAtUtc
com o proprio relogio UTC. O arquivo nao muda sozinho se o GitHub parar.
"""
import argparse
import datetime as dt
from html.parser import HTMLParser
import hashlib
import http.client
import json
import math
import os
import re
import socket
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

UTC = dt.timezone.utc
PAGE = "https://prod-east-goes.woc.noaa.gov/products/atmosphere/vaac/messages.html"
HOST = "prod-east-goes.woc.noaa.gov"
UA = ("VistaDaTerra-VAAC-Experimental/2.0 "
      "(github.com/paulofloripa2006-cloud/vista-da-terra-vulcoes)")

COLLECT = "http://def.wmo.int/collect/2014"
IWXXM = "http://icao.int/iwxxm/3.0"
GML = "http://www.opengis.net/gml/3.2"
AIXM = "http://www.aixm.aero/schema/5.1.1"
METCE = "http://def.wmo.int/metce/2013"
W = "{%s}" % IWXXM

SCHEMA = "vaac-exp-1"
REVISION = 5
LIMIT = 40              # maximo de XML baixados por execucao
BUDGET = 240            # orcamento global (s)
RESERVE = 60            # reserva para gravar/publicar (s)
XML_TIMEOUT = 15        # tempo maximo por XML (s)
GRACE_H = 3             # prazo operacional apos o proximo aviso previsto
FALLBACK_H = 9          # sem proximo horario e sem encerramento: emissao + 9 h
MAX_NEXT_H = 12         # proximo aviso plausivel: ate 12 h depois da emissao
MAX_VALIDITY_H = 15     # teto absoluto: nenhum aviso vale mais que emissao + 15 h
DISPLAY_OBS_H = 6       # poligono so e exibivel ate 6 h apos o horario da observacao
IDENTITY_KM = 25        # distancia maxima VAAC x catalogo para identidade conferida
MAX_404 = 6             # tentativas antes de considerar 404 permanente
SEEN_DAYS = 15
SHOW_HOURS = 48         # closed/rejected visiveis por 48 h

STATUS_RANK = {"NORMAL": 0, "AMENDMENT": 1, "CORRECTION": 2}
ASH_ENDED_RE = re.compile(r"\bVA\s+E[MN]S\s+(?:HAVE\s+|HAS\s+)?(?:ENDED|CEASED)\b", re.I)
NO_FURTHER_RE = re.compile(r"NO\s+FURTHER\s+ADVISOR(?:Y|IES)", re.I)
BULLETIN_RE = re.compile(
    r"^A_([A-Z]{4}\d{2})([A-Z]{4})(\d{6})((?:CC|AA|RR)[A-Z])?_C_([A-Z]{4})_(\d{14})\.xml$")
FILE_RE = re.compile(r"FVXX[0-9A-Z]+_(\d{8})_(\d{4})\.xml$", re.I)


# ----------------------------------------------------------------- utilidades
def utcnow():
    return dt.datetime.now(UTC).replace(microsecond=0)


def stamp(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def date(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.astimezone(UTC) if d.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def txt(root, path):
    el = root.find(path)
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(min(1, math.sqrt(a)))


def norm_name(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\d{6}", " ", s)
    return " ".join(re.findall(r"[a-z]+", s))


def names_match(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a) or a.split()[0] == b.split()[0]


def file_time(url):
    """Horario contido no nome FVXX.._AAAAMMDD_HHMM.xml (so para ordenar downloads)."""
    m = FILE_RE.search(url)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError:
        return None


# --------------------------------------------------------- erros classificados
class TempError(Exception):
    """Erro temporario: nova tentativa no proximo ciclo."""
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


class PermError(Exception):
    """Erro permanente: o arquivo nao sera baixado de novo."""


class Malformed(Exception):
    """XML mal formado: so vira permanente na segunda leitura identica."""


# --------------------------------------------------------------------- rede
class Body(bytes):
    """Bytes recebidos + se o tamanho foi conferido com Content-Length."""
    length_verified = False


def body_of(raw, verified=False):
    b = Body(raw)
    b.length_verified = bool(getattr(raw, "length_verified", verified))
    return b


def fetch(url, timeout=XML_TIMEOUT, maxbytes=1_000_000, allowed=(("https", HOST),)):
    u = urlsplit(url)
    if (u.scheme, u.hostname) not in allowed:
        raise PermError("URL de origem nao permitida")
    req = Request(url, headers={"User-Agent": UA,
                                "Accept": "text/xml,application/xml,text/html"})
    try:
        with urlopen(req, timeout=timeout) as r:
            declared = r.headers.get("Content-Length")
            body = r.read(maxbytes + 1)
    except HTTPError as e:
        if e.code == 404:
            raise TempError("http404", "HTTP 404")
        if e.code >= 500 or e.code == 429:
            raise TempError("http5xx", "HTTP %d" % e.code)
        raise PermError("HTTP %d" % e.code)
    except http.client.IncompleteRead as e:
        raise TempError("truncado", "Resposta incompleta: %d bytes" % len(e.partial))
    except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            http.client.HTTPException) as e:
        raise TempError("rede", "%s: %s" % (type(e).__name__, e))
    if len(body) > maxbytes:
        raise PermError("Resposta excedeu limite de bytes")
    if declared and declared.isdigit() and int(declared) != len(body):
        raise TempError("truncado", "Recebidos %d de %s bytes" % (len(body), declared))
    # Sem Content-Length (ex.: chunked) a completude NAO e comprovada.
    return body_of(body, bool(declared and declared.isdigit()))


# ------------------------------------------------------------- pagina indice
class Links(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.host = urlsplit(page).hostname
        self.section = 0
        self.urls = []

    def handle_data(self, data):
        if "Advisories from the past 24 hours" in data:
            self.section = 1
        elif "Advisories from the past 15 days" in data:
            self.section = 2

    def handle_starttag(self, tag, attrs):
        if tag != "a" or self.section != 1:
            return
        href = dict(attrs).get("href", "") or ""
        if not re.search(r"\.xml(?:\?.*)?$", href, re.I):
            return
        url = urljoin(self.page, href)
        p = urlsplit(url)
        if (p.hostname == self.host and re.fullmatch(
                r"/products/atmosphere/vaac/volcanoes/xml_files/FVXX[0-9A-Z]+_\d{8}_\d{4}\.xml",
                p.path, re.I)):
            self.urls.append(url)


def page_links(html, page=PAGE):
    parser = Links(page)
    parser.feed(html)
    if parser.section < 2:
        raise ValueError("Secoes NOAA nao localizadas; layout pode ter mudado")
    links = sorted(set(parser.urls),
                   key=lambda u: (file_time(u) or dt.datetime.min.replace(tzinfo=UTC), u),
                   reverse=True)  # mais novos primeiro
    if not links:
        raise ValueError("Secao de 24 horas sem links XML")
    return links


# ---------------------------------------------------------------- geometria
def _orient(a, b, c):
    v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)


def _on_seg(a, b, c):
    return (min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9 and
            min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9)


def _cross(p1, p2, q1, q2):
    o1, o2, o3, o4 = _orient(p1, p2, q1), _orient(p1, p2, q2), _orient(q1, q2, p1), _orient(q1, q2, p2)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and _on_seg(p1, p2, q1)) or (o2 == 0 and _on_seg(p1, p2, q2)) or
            (o3 == 0 and _on_seg(q1, q2, p1)) or (o4 == 0 and _on_seg(q1, q2, p2)))


def self_intersects(xy):
    """xy = anel fechado (primeiro == ultimo). Testa arestas nao adjacentes."""
    edges = [(xy[i], xy[i + 1]) for i in range(len(xy) - 1)]
    n = len(edges)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # arestas vizinhas compartilham vertice
            if _cross(edges[i][0], edges[i][1], edges[j][0], edges[j][1]):
                return True
    return False


def polygon(poslist):
    if poslist is None or not poslist.text:
        raise ValueError("GML posList ausente")
    if poslist.get("srsDimension", "2") != "2":
        raise ValueError("Coordenadas GML nao sao bidimensionais")
    try:
        vals = [float(v) for v in poslist.text.split()]
    except ValueError:
        raise ValueError("posList com valor nao numerico")
    if len(vals) % 2:
        raise ValueError("posList com numero impar de valores")
    pts = [vals[i:i + 2] for i in range(0, len(vals), 2)]
    declared = poslist.get("count")
    if declared is not None and (not declared.isdigit() or int(declared) != len(pts)):
        raise ValueError("count declarado (%s) difere das posicoes reais (%d)" % (declared, len(pts)))
    if len(pts) < 4:
        raise ValueError("Anel com menos de 4 posicoes")
    if any(not (math.isfinite(a) and math.isfinite(b) and abs(a) <= 90 and abs(b) <= 180)
           for a, b in pts):
        raise ValueError("Latitude/longitude fora dos limites")
    if pts[0] != pts[-1]:
        raise ValueError("Anel aberto")
    if len({tuple(x) for x in pts[:-1]}) < 3:
        raise ValueError("Menos de 3 vertices distintos")
    center = sum(a for a, _ in pts[:-1]) / len(pts[:-1])
    xyz, crosses, prev = [], False, None
    for lat, lon in pts:
        if prev is not None:
            while lon - prev > 180:
                lon -= 360; crosses = True
            while lon - prev < -180:
                lon += 360; crosses = True
        prev = lon
        xyz.append((math.radians(lon) * 6371 * math.cos(math.radians(center)),
                    math.radians(lat) * 6371))
    if self_intersects(xyz):
        raise ValueError("Anel autointersectante")
    area = abs(sum(xyz[i][0] * xyz[i + 1][1] - xyz[i + 1][0] * xyz[i][1]
                   for i in range(len(xyz) - 1))) / 2
    if area < 0.01:
        raise ValueError("Poligono com area degenerada")
    return [[round(a, 5), round(b, 5)] for a, b in pts], round(area, 3), crosses


def layer(cloud):
    vols = cloud.findall(".//{%s}AirspaceVolume" % AIXM)
    if len(vols) != 1:
        raise ValueError("Camada sem volume unico")
    vol = vols[0]
    up = vol.find("{%s}upperLimit" % AIXM)
    low = vol.find("{%s}lowerLimit" % AIXM)
    if up is None or up.get("uom") != "FL" or not (up.text or "").strip():
        raise ValueError("FL superior ausente")
    high = float(up.text)
    lowtxt = (low.text or "").strip().upper() if low is not None else ""
    if lowtxt in ("GND", "SFC"):
        bottom, lower_label = 0.0, lowtxt
    elif low is not None and low.get("uom") == "FL" and lowtxt:
        bottom, lower_label = float(lowtxt), "FL"
    else:
        raise ValueError("FL inferior ausente")
    if not 0 <= bottom <= high <= 1000:
        raise ValueError("Limites FL invalidos")
    rings = vol.findall(".//{%s}LinearRing/{%s}posList" % (GML, GML))
    if len(rings) != 1:
        raise ValueError("Quantidade de aneis GML inesperada (%d)" % len(rings))
    pts, area, crosses = polygon(rings[0])
    dre = cloud.find(W + "directionOfMotion")
    spe = cloud.find(W + "speedOfMotion")
    direction = speed = None
    try:
        if dre is not None and dre.get("uom") == "deg" and dre.text:
            direction = float(dre.text)
        if spe is not None and spe.get("uom") in ("[kn_i]", "KT") and spe.text:
            speed = float(spe.text)
    except ValueError:
        pass
    if direction is not None and not 0 <= direction <= 360:
        direction = None
    if speed is not None and not 0 <= speed <= 300:
        speed = None
    return {"lower": lower_label, "lowerFL": bottom, "upperFL": high,
            "polygon": pts, "vertices": len(pts) - 1, "areaKm2": area,
            "crossesDateline": crosses, "motionDeg": direction, "speedKt": speed}


# --------------------------------------------------------- leitura do aviso
def parse_bulletin_id(text):
    """Extrai indicador de correcao (BBB) e horario de geracao do bulletinIdentifier."""
    if not text:
        return {"raw": None, "bbb": None, "generatedUtc": None}
    m = BULLETIN_RE.match(text.strip())
    if not m:
        return {"raw": text.strip(), "bbb": None, "generatedUtc": None}
    try:
        gen = dt.datetime.strptime(m.group(6), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        gen = None
    return {"raw": text.strip(), "bbb": m.group(4), "generatedUtc": stamp(gen)}


def parse_advisory(adv, url, now, catalog, bulletin):
    if adv.tag != W + "VolcanicAshAdvisory":
        raise PermError("Versao de aviso nao testada (%s)" % adv.tag.split("}")[0].strip("{"))
    issue = date(txt(adv, W + "issueTime/.//{%s}timePosition" % GML))
    if issue is None:
        raise ValueError("Horario de emissao ausente")
    if issue > now + dt.timedelta(minutes=15):
        raise ValueError("Horario de emissao no futuro")
    centre = txt(adv, W + "issuingVolcanicAshAdvisoryCentre/.//{%s}name" % AIXM) or "WASHINGTON"
    name = txt(adv, W + "volcano/.//{%s}name" % METCE)
    if not name:
        raise ValueError("Nome do vulcao ausente")
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", name)
    gvp = m.group(1) if m else None
    coords = None
    point = txt(adv, W + "volcano/.//{%s}Point/{%s}pos" % (GML, GML))
    if point:
        try:
            a, b = [float(x) for x in point.split()]
            if math.isfinite(a) and math.isfinite(b) and abs(a) <= 90 and abs(b) <= 180:
                coords = [round(a, 5), round(b, 5)]
        except (ValueError, TypeError):
            pass
    identity = {"catalogName": None, "distanceKm": None, "nameMatch": None}
    if not gvp:
        gvp_status = "ausente"
    elif catalog is None:
        gvp_status = "declarado_nao_conferido"
    elif gvp not in catalog:
        gvp_status = "fora_do_catalogo"
    elif not isinstance(catalog, dict) or not isinstance(catalog.get(gvp), dict):
        gvp_status = "numero_no_catalogo_sem_conferencia"
    else:
        ref = catalog[gvp]
        identity["catalogName"] = ref.get("name")
        identity["nameMatch"] = names_match(name, ref.get("name"))
        dist_ok = True
        if coords and isinstance(ref.get("lat"), (int, float)) and isinstance(ref.get("lon"), (int, float)):
            identity["distanceKm"] = round(km(coords[0], coords[1], ref["lat"], ref["lon"]), 1)
            dist_ok = identity["distanceKm"] <= IDENTITY_KM
        gvp_status = "identidade_conferida" if identity["nameMatch"] and dist_ok else "identidade_divergente"
    valid = gvp_status == "identidade_conferida"
    # Chave do vulcao: numero com identidade conferida, ou declarado/no catalogo quando
    # nao ha como conferir. Ausente, fora do catalogo ou identidade divergente:
    # isolado por arquivo, nunca fundido com outros registros do mesmo numero.
    if gvp_status in ("identidade_conferida", "numero_no_catalogo_sem_conferencia", "declarado_nao_conferido"):
        vk = gvp
    else:
        prefix = "identity-conflict:" + gvp if gvp_status == "identidade_divergente" else "unverified"
        vk = (prefix + ":" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
              + ":" + url.rsplit("/", 1)[-1])
    summit = None
    se = adv.find(W + "summitElevation")
    if se is not None and se.text:
        try:
            v = float(se.text)
            uom = se.get("uom")
            meters = v * 0.3048 if uom in ("[ft_i]", "FT", "ft") else (v if uom == "m" else None)
            summit = {"value": v, "uom": uom, "meters": round(meters) if meters is not None else None}
        except ValueError:
            summit = None
    num = txt(adv, W + "advisoryNumber")
    if not num:
        raise ValueError("Numero do aviso ausente")

    cond = adv.find(W + "observation/" + W + "VolcanicAshObservedOrEstimatedConditions")
    status = cond.get("status", "DESCONHECIDO") if cond is not None else "AUSENTE"
    est_attr = cond.get("isEstimated") if cond is not None else None
    est_norm = est_attr.strip().lower() if est_attr is not None else None
    estimated = True if est_norm in ("true", "1") else (False if est_norm in ("false", "0") else None)
    obstime = date(txt(cond, W + "phenomenonTime/.//{%s}timePosition" % GML)) if cond is not None else None
    layers, layer_errors = [], []
    if cond is not None:
        for c in cond.findall(W + "ashCloud/" + W + "VolcanicAshCloudObservedOrEstimated"):
            try:
                layers.append(layer(c))
            except (ValueError, OverflowError) as e:
                layer_errors.append(str(e))
    forecasts_ignored = len(adv.findall(W + "forecast"))

    nextel = adv.find(W + "nextAdvisoryTime")
    nexttime = date(txt(adv, W + "nextAdvisoryTime/.//{%s}timePosition" % GML))
    next_pos = adv.find(W + "nextAdvisoryTime/.//{%s}timePosition" % GML)
    # "before" = a proxima emissao ocorrera ATE esse horario (prazo, nao instante exato).
    next_qualifier = next_pos.get("indeterminatePosition") if next_pos is not None else None
    nil_reason = None
    if nextel is not None:
        for k, val in nextel.attrib.items():
            if k.endswith("nilReason") and val:
                nil_reason = val.strip()
    remarks = txt(adv, W + "remarks")
    details = txt(adv, W + "eruptionDetails")
    text_no_further = bool(NO_FURTHER_RE.search(remarks or ""))
    ash_ended = bool(ASH_ENDED_RE.search(details or "") or ASH_ENDED_RE.search(remarks or ""))
    # Estado do proximo aviso. So "inapplicable" sem horario (ou texto explicito
    # "NO FURTHER ADVISORIES" sem horario) encerra a serie. Qualquer outra
    # combinacao contraditoria ou desconhecida fica "indeterminado" e NAO encerra.
    if nexttime and (nil_reason or text_no_further):
        next_state = "contraditorio"
    elif nexttime:
        next_state = ("programado" if issue < nexttime <= issue + dt.timedelta(hours=MAX_NEXT_H)
                      else "incoerente")
    elif nil_reason and nil_reason.rstrip("/").endswith("/inapplicable"):
        next_state = "sem_proximo_aviso"
    elif nil_reason:
        next_state = "indeterminado"
    elif text_no_further:
        next_state = "sem_proximo_aviso"
    else:
        next_state = "ausente"
    no_further = next_state == "sem_proximo_aviso"

    cap = issue + dt.timedelta(hours=MAX_VALIDITY_H)
    if next_state == "programado":
        expiry, fallback = nexttime + dt.timedelta(hours=GRACE_H), False
    else:
        expiry, fallback = issue + dt.timedelta(hours=FALLBACK_H), True
    capped = expiry > cap
    expiry = min(expiry, cap)

    if status == "IDENTIFIABLE":
        kind = "estimada" if estimated else ("observada" if estimated is False else "indefinida")
    elif status == "NOT_IDENTIFIABLE":
        kind = "nao_identificavel"
    else:
        kind = "outro:" + status
    warnings = []
    if status == "IDENTIFIABLE" and not layers:
        warnings.append("identificavel_sem_geometria_valida")
    if status == "IDENTIFIABLE" and estimated is None:
        warnings.append("isEstimated_ausente" if est_attr is None else "isEstimated_invalido:" + est_attr[:20])
    obs_ok = bool(obstime and issue - dt.timedelta(hours=24) <= obstime <= issue + dt.timedelta(minutes=15))
    obs_fresh = bool(obs_ok and issue - obstime <= dt.timedelta(hours=DISPLAY_OBS_H))
    if status == "IDENTIFIABLE" and not obstime:
        warnings.append("sem_horario_de_observacao")
    elif status == "IDENTIFIABLE" and not obs_ok:
        warnings.append("horario_de_observacao_incoerente")
    elif status == "IDENTIFIABLE" and not obs_fresh:
        warnings.append("observacao_antiga_para_exibicao")
    if next_state in ("contraditorio", "incoerente", "indeterminado"):
        warnings.append("proximo_aviso_" + next_state)
    if capped:
        warnings.append("validade_limitada_ao_teto")
    if gvp_status == "identidade_divergente":
        warnings.append("identidade_do_vulcao_divergente")
    displayable = bool(status == "IDENTIFIABLE" and layers and estimated is not None and obs_fresh)
    display_until = min(obstime + dt.timedelta(hours=DISPLAY_OBS_H), expiry) if displayable else None

    rstatus = (adv.get("reportStatus") or "DESCONHECIDO").upper()
    doc_id = bulletin["raw"] or url.rsplit("/", 1)[-1]
    key = "%s|%s|%s|%s|%s|%s|%s" % (centre, vk, num, stamp(issue), rstatus,
                                    bulletin["bbb"] or "-", doc_id)
    return {
        "key": key, "volcanoKey": vk,
        "volcano": {"name": name, "gvp": gvp, "gvpValid": valid, "gvpStatus": gvp_status,
                    "identity": identity,
                    "lat": coords[0] if coords else None, "lon": coords[1] if coords else None,
                    "area": txt(adv, W + "stateOrRegion"), "summit": summit},
        "advisory": {"centre": centre, "number": num, "reportStatus": rstatus,
                     "bulletinId": bulletin["raw"], "bulletinBBB": bulletin["bbb"],
                     "bulletinGeneratedUtc": bulletin["generatedUtc"],
                     "issueUtc": stamp(issue), "observationUtc": stamp(obstime),
                     "nextAdvisoryUtc": stamp(nexttime), "nextAdvisoryState": next_state,
                     "nextAdvisoryQualifier": next_qualifier,
                     "nextAdvisoryNilReason": nil_reason, "noFurtherAdvisoryScheduled": no_further,
                     "ashEmissionEndedReported": ash_ended,
                     "expiresAtUtc": stamp(expiry), "expiryFallback": fallback,
                     "fetchedUtc": stamp(now), "replaces": None, "xmlUrl": url,
                     "infoSource": txt(adv, W + "informationSource"),
                     "eruptionDetailsRaw": details, "remarksRaw": remarks,
                     "forecastsIgnored": forecasts_ignored},
        "cloud": {"status": status, "isEstimated": estimated, "kind": kind,
                  "positionMeaning": "posicao no horario da observacao, sem extrapolacao",
                  "layers": layers, "rejectedLayerReasons": layer_errors,
                  "warnings": warnings, "displayable": displayable,
                  "displayableUntilUtc": stamp(display_until),
                  "observationAgeAtIssueMin": round((issue - obstime).total_seconds() / 60) if obstime else None},
    }


def parse_document(body, url, now, catalog=None):
    """Devolve (avisos_validos, rejeicoes_individuais). Lanca Malformed/PermError."""
    if len(body) > 1_000_000:
        raise PermError("XML excessivo")
    up = body.upper()
    if b"<!DOCTYPE" in up or b"<!ENTITY" in up:
        raise PermError("DOCTYPE/entidades nao aceitos")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise Malformed("XML mal formado: %s" % e)
    if root.tag == "{%s}MeteorologicalBulletin" % COLLECT:
        bulletin = parse_bulletin_id(txt(root, "{%s}bulletinIdentifier" % COLLECT))
        infos = root.findall("{%s}meteorologicalInformation" % COLLECT)
        if not infos:
            raise PermError("Boletim sem meteorologicalInformation")
        advisories = []
        for i, info in enumerate(infos):
            kids = list(info)
            if not kids:
                advisories.append(("%d" % i, None))
            for j, kid in enumerate(kids):
                # Esquema OMM preve 1 aviso por meteorologicalInformation; se vierem
                # varios, cada um e tratado separadamente e o desvio fica registrado.
                advisories.append(("%d.%d" % (i, j) if len(kids) > 1 else "%d" % i, kid))
    elif root.tag == W + "VolcanicAshAdvisory":
        bulletin = parse_bulletin_id(None)
        advisories = [(0, root)]
    elif root.tag.endswith("}VolcanicAshAdvisory") or root.tag.endswith("}MeteorologicalBulletin"):
        raise PermError("Versao nao testada (quarentena): %s" % root.tag)
    else:
        raise PermError("Raiz inesperada: %s" % root.tag)

    rows, errors = [], []
    for i, adv in advisories:
        try:
            if adv is None:
                raise ValueError("meteorologicalInformation vazio")
            rows.append(parse_advisory(adv, url, now, catalog, bulletin))
        except (ValueError, PermError) as e:
            errors.append({"index": i, "reason": str(e)[:240]})
    multi = sum(1 for i, _ in advisories if "." in str(i))
    if multi:
        for r in rows:
            r["advisory"].setdefault("structureWarnings", []).append(
                "varios avisos no mesmo meteorologicalInformation (fora do padrao OMM)")
    if not rows and errors and all("nao testada" in e["reason"] for e in errors):
        raise PermError("Versao nao testada (quarentena): " + errors[0]["reason"])
    return rows, errors


# ------------------------------------------------------ comparacao de versoes
def compare(new, old):
    """1 = new substitui old; -1 = mantem old; 0 = duplicado; None = empate nao resolvido."""
    ni, oi = date(new["issueUtc"]), date(old["issueUtc"])
    if ni != oi:
        return 1 if ni > oi else -1
    if new["key"] == old["key"]:
        return 0
    # Indicador WMO de correcao no cabecalho (grupo BBB): sem BBB < CCA < CCB ...
    nb, ob = new.get("bulletinBBB") or "", old.get("bulletinBBB") or ""
    if nb != ob and nb[:2] in ("", "CC") and ob[:2] in ("", "CC"):
        return 1 if nb > ob else -1
    ns, os_ = STATUS_RANK.get(new["reportStatus"]), STATUS_RANK.get(old["reportStatus"])
    if ns is not None and os_ is not None and ns != os_:
        return 1 if ns > os_ else -1
    # Horario de geracao do bulletinIdentifier: NAO usado ate ser comprovado
    # em correcoes reais com o mesmo issueTime (regra acordada com o GPT).
    return None


def summary(row):
    a = row["advisory"]
    return {"key": row["key"], "issueUtc": a["issueUtc"], "reportStatus": a["reportStatus"],
            "bulletinBBB": a["bulletinBBB"], "bulletinGeneratedUtc": a["bulletinGeneratedUtc"],
            "closing": a["noFurtherAdvisoryScheduled"], "expiresAtUtc": a["expiresAtUtc"],
            "xmlUrl": a["xmlUrl"]}


# ------------------------------------------------------------------- estado
def new_doc():
    return {"schema": SCHEMA, "revision": REVISION, "experimental": True,
            "source": {"name": "NOAA Washington VAAC", "url": PAGE},
            "collector": {},
            "rules": {"toleranceHours": GRACE_H, "fallbackHours": FALLBACK_H,
                      "acceptedIWXXM": ["3.0"], "acceptedEnvelope": ["WMO collect 2014"],
                      "seenRetentionDays": SEEN_DAYS, "max404Attempts": MAX_404,
                      "consumerMustCheckExpiry": True,
                      "maxNextAdvisoryHours": MAX_NEXT_H, "maxValidityHours": MAX_VALIDITY_H,
                      "displayMaxObservationAgeHours": DISPLAY_OBS_H,
                      "consumerMustCheckDisplayableUntil": True,
                      "identityMaxDistanceKm": IDENTITY_KM,
                      "forecastsStored": False,
                      "notes": ["Aviso de cinzas nao comprova erupcao nova.",
                                "noFurtherAdvisoryScheduled encerra a serie de avisos, "
                                "nao a atividade vulcanica.",
                                "Poligono = posicao no horario da observacao.",
                                "Exibir poligono somente se displayable e relogio < displayableUntilUtc.",
                                "O consumidor deve conferir expiresAtUtc e displayableUntilUtc com o "
                                "proprio relogio UTC, nunca so o campo displayable gravado.",
                                "Poligono que deixa de ser exibivel por idade da observacao NAO "
                                "significa que as cinzas desapareceram.",
                                "nextAdvisoryQualifier 'before' = proxima emissao ate esse horario "
                                "(prazo), nao instante exato."]},
            "active": {}, "conflicts": {}, "closed": [], "rejected": [],
            "pending": {}, "seen": {}, "lastState": {}}


def load(path):
    if not os.path.exists(path):
        return new_doc()
    with open(path, encoding="utf8") as f:
        data = json.load(f)
    if data.get("schema") != SCHEMA or not isinstance(data.get("active"), dict) \
            or not isinstance(data.get("seen"), dict):
        raise ValueError("JSON existente invalido: nao sobrescrever")
    base = new_doc()
    for k in ("conflicts", "pending", "lastState"):
        data.setdefault(k, {})
    data["rules"] = base["rules"]
    data["revision"] = REVISION
    return data


def close_row(doc, vk, row, why, now):
    doc["active"].pop(vk, None)
    doc["closed"].append({"volcanoKey": vk, "key": row["key"], "reason": why,
                          "closedAtUtc": stamp(now), "issueUtc": row["advisory"]["issueUtc"],
                          "volcano": row["volcano"]["name"]})


def apply(doc, row, now):
    vk = row["volcanoKey"]
    a = row["advisory"]
    cand = summary(row)
    latest = doc["lastState"].get(vk)
    if latest:
        cmp = compare(cand, latest)
        if cmp == 0:
            return "duplicado"
        if cmp == -1:
            return "anterior"
        if cmp is None:
            conflict = {"atUtc": stamp(now), "candidates": [latest, cand],
                        "reason": "empate nao resolvido (mesma emissao)"}
            if latest.get("closing") or cand["closing"]:
                # Possivel encerramento envolvido: nada e apresentado como atual.
                doc["conflicts"][vk] = conflict
                prior = doc["active"].get(vk)
                if prior:
                    prior["cloud"]["displayable"] = False
                    prior["cloud"]["warnings"].append("conflito_de_versoes")
                return "conflito"
            doc["conflicts"][vk] = conflict
            return "conflito_mantido_anterior"
        a["replaces"] = latest["key"]
    doc["conflicts"].pop(vk, None)
    prior = doc["active"].get(vk)
    if prior:
        close_row(doc, vk, prior, "substituido", now)
    doc["lastState"][vk] = cand
    if a["noFurtherAdvisoryScheduled"]:
        why = "serie_encerrada" + ("_emissao_de_cinzas_encerrada_relatada"
                                   if a["ashEmissionEndedReported"] else "")
        close_row(doc, vk, row, why, now)
        return "encerrado"
    if date(a["expiresAtUtc"]) <= now:
        close_row(doc, vk, row, "expirado", now)
        return "expirado"
    doc["active"][vk] = row
    return "ativo"


def _cand_expiry(c):
    return date(c.get("expiresAtUtc")) or ((date(c.get("issueUtc")) or dt.datetime.min.replace(tzinfo=UTC))
                                           + dt.timedelta(hours=MAX_VALIDITY_H))


def prune(doc, now):
    for vk, row in list(doc["active"].items()):
        if (date(row["advisory"]["expiresAtUtc"]) or dt.datetime.min.replace(tzinfo=UTC)) <= now:
            close_row(doc, vk, row, "expirado", now)
        else:
            until = date(row["cloud"].get("displayableUntilUtc"))
            if row["cloud"].get("displayable") and until and until <= now:
                row["cloud"]["displayable"] = False
                row["cloud"]["warnings"].append("observacao_envelhecida")
    for vk, c in list(doc["conflicts"].items()):
        if vk not in doc["active"] and all(_cand_expiry(x) <= now for x in c.get("candidates", [])):
            doc["conflicts"].pop(vk)
            doc["closed"].append({"volcanoKey": vk, "key": c["candidates"][-1].get("key"),
                                  "reason": "conflito_resolvido_por_expiracao", "closedAtUtc": stamp(now),
                                  "issueUtc": c["candidates"][-1].get("issueUtc"), "volcano": None})
    cut = now - dt.timedelta(hours=SHOW_HOURS)
    doc["closed"] = [r for r in doc["closed"] if (date(r.get("closedAtUtc")) or cut) >= cut]
    doc["rejected"] = [r for r in doc["rejected"] if (date(r.get("atUtc")) or cut) >= cut]
    doc["pending"] = {u: p for u, p in doc["pending"].items()
                      if (date(p.get("firstUtc")) or cut) >= cut}
    cut = now - dt.timedelta(days=SEEN_DAYS)
    doc["seen"] = {k: v for k, v in doc["seen"].items() if (date(v.get("atUtc")) or cut) >= cut}
    doc["lastState"] = {k: v for k, v in doc["lastState"].items()
                        if (date(v.get("issueUtc")) or cut) >= cut}


def validate_doc(doc):
    if doc.get("schema") != SCHEMA:
        raise ValueError("schema")
    for k in ("active", "conflicts", "pending", "seen", "lastState"):
        if not isinstance(doc.get(k), dict):
            raise ValueError(k)
    for k in ("closed", "rejected"):
        if not isinstance(doc.get(k), list):
            raise ValueError(k)
    for vk, row in doc["active"].items():
        if row["volcanoKey"] != vk or not date(row["advisory"]["expiresAtUtc"]):
            raise ValueError("registro ativo invalido: " + vk)


def write_atomic(path, doc):
    validate_doc(doc)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    content = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    validate_doc(json.loads(content))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".vaac-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp, encoding="utf8") as f:
            validate_doc(json.load(f))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ----------------------------------------------------------------- execucao
def run(out, catalog=None, fetcher=fetch, page=PAGE, clock=time.monotonic, now=None):
    """Retorna 'OK', 'PARCIAL' ou 'FALHA'. Grava sempre que o JSON for valido."""
    started = clock()
    now = now or utcnow()
    doc = load(out)          # JSON existente invalido -> excecao, nada e sobrescrito
    before = {r["key"] for r in doc["closed"]}
    prune(doc, now)
    expired_start = sum(1 for r in doc["closed"] if r["key"] not in before and r["reason"] == "expirado")
    last_ok = doc.get("collector", {}).get("lastSuccessUtc")
    c = {"found": 0, "downloaded": 0, "accepted": 0, "pendingRetry": 0, "notStartedBudget": 0,
         "rejectedPermanent": 0, "rejectedAdvisories": 0, "duplicates": 0, "older": 0,
         "conflicts": 0, "closed": 0, "expired": expired_start, "active": 0}
    result, err = "OK", None
    try:
        links = page_links(fetcher(page, 20, 2_000_000).decode("utf8", "replace"), page)
        c["found"] = len(links)
        listed = set(links)
        # pendentes que sairam da lista de 24 h viram rejeicao definitiva
        for u, p in list(doc["pending"].items()):
            if u not in listed:
                doc["pending"].pop(u)
                doc["rejected"].append({"xmlUrl": u, "reason": "saiu do indice sem leitura valida: "
                                        + p.get("lastError", ""), "atUtc": stamp(now),
                                        "attempts": p.get("attempts")})
                doc["seen"][u] = {"atUtc": stamp(now), "result": "invalido"}
        todo = [u for u in links if u not in doc["seen"]]
        parsed = []
        for i, url in enumerate(todo):
            if c["downloaded"] >= LIMIT or clock() - started > BUDGET - RESERVE - XML_TIMEOUT:
                c["notStartedBudget"] = len(todo) - i
                result = "PARCIAL"
                break
            c["downloaded"] += 1
            p = doc["pending"].get(url, {"attempts": 0, "firstUtc": stamp(now)})
            p["attempts"] += 1
            p["lastUtc"] = stamp(now)
            try:
                body = body_of(fetcher(url, XML_TIMEOUT, 1_000_000))
                rows, errs = parse_document(body, url, now, catalog)
                for e in errs:
                    doc["rejected"].append({"xmlUrl": url, "reason": "aviso %s: %s" % (e["index"], e["reason"]),
                                            "atUtc": stamp(now)})
                    c["rejectedAdvisories"] += 1
                if not rows:
                    raise PermError("Nenhum aviso valido no boletim")
                parsed.extend(rows)
                doc["pending"].pop(url, None)
                doc["seen"][url] = {"atUtc": stamp(now), "result": "lido",
                                    "keys": [r["key"] for r in rows]}
            except Malformed as e:
                h = hashlib.sha256(body).hexdigest()
                if not body.length_verified:
                    # completude nao comprovada: segue pendente ate sair do indice
                    p.update(malformedHash=h, lastError=str(e)[:200] + " (tamanho nao comprovado)",
                             kind="malformado_sem_tamanho")
                    doc["pending"][url] = p
                    c["pendingRetry"] += 1
                elif p.get("malformedHash") == h and p.get("malformedVerified"):
                    doc["pending"].pop(url, None)
                    doc["seen"][url] = {"atUtc": stamp(now), "result": "invalido"}
                    doc["rejected"].append({"xmlUrl": url, "reason": "%s (2 leituras identicas)" % e,
                                            "atUtc": stamp(now)})
                    c["rejectedPermanent"] += 1
                else:
                    p.update(malformedHash=h, malformedVerified=True, lastError=str(e)[:200], kind="malformado")
                    doc["pending"][url] = p
                    c["pendingRetry"] += 1
                result = "PARCIAL"
            except TempError as e:
                if e.kind == "http404":
                    p["count404"] = p.get("count404", 0) + 1
                if e.kind == "http404" and p["count404"] >= MAX_404:
                    doc["pending"].pop(url, None)
                    doc["seen"][url] = {"atUtc": stamp(now), "result": "invalido"}
                    doc["rejected"].append({"xmlUrl": url, "reason": "HTTP 404 em %d tentativas (%d tentativas no total)" % (p["count404"], p["attempts"]),
                                            "atUtc": stamp(now)})
                    c["rejectedPermanent"] += 1
                else:
                    p.update(lastError=str(e)[:200], kind=e.kind)
                    doc["pending"][url] = p
                    c["pendingRetry"] += 1
                result = "PARCIAL"
            except Exception as e:  # PermError, ValueError e inesperados: so este arquivo
                doc["pending"].pop(url, None)
                doc["seen"][url] = {"atUtc": stamp(now), "result": "invalido"}
                doc["rejected"].append({"xmlUrl": url, "reason": (type(e).__name__ + ": " + str(e))[:240],
                                        "atUtc": stamp(now)})
                c["rejectedPermanent"] += 1
                result = "PARCIAL"
        # aplicar em ordem cronologica real (emissao, depois revisao)
        parsed.sort(key=lambda r: (r["advisory"]["issueUtc"],
                                   STATUS_RANK.get(r["advisory"]["reportStatus"], 0),
                                   r["advisory"]["bulletinBBB"] or ""))
        for row in parsed:
            ev = apply(doc, row, now)
            c["accepted"] += 1
            if ev == "duplicado": c["duplicates"] += 1
            elif ev == "anterior": c["older"] += 1
            elif ev.startswith("conflito"): c["conflicts"] += 1
            elif ev == "encerrado": c["closed"] += 1
            elif ev == "expirado": c["expired"] += 1
        last_ok = stamp(now)
    except Exception as e:
        result = "FALHA"
        err = (type(e).__name__ + ": " + str(e))[:240]
    before = {r["key"] for r in doc["closed"]}
    prune(doc, now)
    c["expired"] += sum(1 for r in doc["closed"] if r["key"] not in before and r["reason"] == "expirado")
    c["active"] = len(doc["active"])
    issues = [r["advisory"]["issueUtc"] for r in doc["active"].values()]
    doc["collector"] = {"lastAttemptUtc": stamp(now), "lastSuccessUtc": last_ok,
                        "result": result, "error": err, "counts": c,
                        "newestActiveIssueUtc": max(issues) if issues else None,
                        "oldestActiveIssueUtc": min(issues) if issues else None,
                        "elapsedSeconds": round(clock() - started, 2)}
    write_atomic(out, doc)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--catalog", default="data/gvp_catalogo.json")
    args = ap.parse_args()
    ids = None
    try:
        with open(args.catalog, encoding="utf8") as f:
            cat = json.load(f)
        ids = {str(x["vnum"]): {"name": x.get("name"), "lat": x.get("lat"), "lon": x.get("lon")}
               for x in cat.get("items", [])
               if re.fullmatch(r"\d{6}", str(x.get("vnum", "")))} or None
    except (OSError, ValueError, KeyError, AttributeError) as e:
        print("Catalogo GVP nao disponivel:", e, flush=True)
    try:
        result = run(args.out, ids)
    except Exception as e:  # JSON existente invalido ou falha critica de gravacao
        print("FALHA CRITICA (nada gravado):", type(e).__name__, e, flush=True)
        sys.exit(2)
    with open(args.out, encoding="utf8") as f:
        print(json.dumps(json.load(f)["collector"], ensure_ascii=False), flush=True)
    sys.exit(1 if result == "FALHA" else 0)


if __name__ == "__main__":
    main()
