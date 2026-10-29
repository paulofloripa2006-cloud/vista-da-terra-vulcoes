#!/usr/bin/env python3
"""Vista da Terra — coletor de dados vulcânicos (schema 1).

Roda no GitHub Actions. Usa somente a biblioteca padrão do Python.
Cada fonte é coletada e validada separadamente; a falha de uma não afeta as outras.
Quando uma fonte falha, os dados anteriores dela são preservados e só
lastFetchStatus, lastAttemptAt e lastError mudam.
"""
import json, math, os, re, sys, time, datetime as dt, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from html import unescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "vulcoes.json")
CATALOG = os.path.join(ROOT, "data", "gvp_catalogo.json")

GVP_RSS = "https://volcano.si.edu/news/WeeklyVolcanoRSS.xml"
GVP_WFS = ("https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows?service=WFS&version=2.0.0"
           "&request=GetFeature&typeName=GVP-VOTW%3ASmithsonian_VOTW_Holocene_Volcanoes"
           "&outputFormat=json&count=5000")
HANS = "https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes"
GEONET = "https://api.geonet.org.nz/volcano/val"

UA = "VistaDaTerra-coletor/1.0 (+https://github.com; monitoramento vulcanico, 1 consulta a cada 3 h)"
MIN_GVP_ITEMS = 5
MIN_GEONET_ITEMS = 10
HEARTBEAT_H = 6          # regravação mínima, para lastSuccessAt não envelhecer mais de ~6 h no arquivo
DOMINANT_MIN_SHARE = 0.8 # o período escolhido precisa reunir ≥80% dos itens válidos
CATALOG_MAX_AGE_D = 30

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
ISO = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


class FetchError(Exception):
    pass


def fetch(url, accept="*/*", timeout=30, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return r.status, r.headers.get("Content-Type", ""), body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:  # rede, DNS, tempo esgotado
            last = f"{type(e).__name__}: {e}"
        if i + 1 < tries:
            time.sleep(5)
    raise FetchError(last or "falha desconhecida")


def coord(lat, lon):
    """Devolve (lat, lon) válidos ou (None, None). Rejeita texto, NaN, infinito e fora dos limites."""
    try:
        a, b = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(a) and math.isfinite(b)) or abs(a) > 90 or abs(b) > 180:
        return None, None
    return round(a, 4), round(b, 4)


def norm(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------- catálogo GVP
def load_catalog():
    """Tabela vnum → nome, país, lat, lon. Atualizada no máximo a cada 30 dias."""
    cat = None
    try:
        with open(CATALOG, encoding="utf-8") as f:
            cat = json.load(f)
        age = (NOW - dt.datetime.fromisoformat(cat["savedAt"].replace("Z", "+00:00"))).days
        if age <= CATALOG_MAX_AGE_D and cat.get("items"):
            return cat
    except Exception:
        pass
    try:
        st, ct, body = fetch(GVP_WFS, accept="application/json", timeout=60)
        feats = json.loads(body).get("features", [])
        items = []
        for f in feats:
            p = f.get("properties") or {}
            vn = p.get("Volcano_Number") or p.get("VolcanoNumber") or p.get("volcano_nu")
            name = p.get("Volcano_Name") or p.get("VolcanoName") or p.get("volcano_na")
            country = p.get("Country") or p.get("country")
            lat = p.get("Latitude") if p.get("Latitude") is not None else p.get("latitude")
            lon = p.get("Longitude") if p.get("Longitude") is not None else p.get("longitude")
            if lat is None or lon is None:
                c = (f.get("geometry") or {}).get("coordinates") or []
                if len(c) >= 2:
                    lon, lat = c[0], c[1]
            lat, lon = coord(lat, lon)
            if vn and name and lat is not None:
                items.append({"vnum": str(vn), "name": name, "country": country or "",
                              "lat": lat, "lon": lon})
        if len(items) < 500:
            raise FetchError(f"catálogo com apenas {len(items)} vulcões")
        cat = {"savedAt": ISO(NOW), "items": items}
        with open(CATALOG, "w", encoding="utf-8") as f:
            json.dump(cat, f, ensure_ascii=False, separators=(",", ":"))
        keys = sorted((feats[0].get("properties") or {}).keys())[:12] if feats else []
        log(f"Catálogo GVP atualizado: {len(items)} vulcões de {len(feats)} registros; campos: {', '.join(keys)}")
        return cat
    except Exception as e:
        log(f"AVISO catálogo GVP indisponível ({e}); usando cópia anterior, se houver")
        return cat


def catalog_index(cat):
    by_vn, by_nc = {}, {}
    for v in (cat or {}).get("items", []):
        by_vn[v["vnum"]] = v
        by_nc.setdefault((norm(v["name"]), norm(v["country"])), []).append(v)
    return by_vn, by_nc


# ---------------------------------------------------------------- Smithsonian
MON = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                    "august", "september", "october", "november", "december"], 1)}
TITLE_RX = re.compile(r"^(.+?)\s*\((.+?)\)\s*-\s*Report for\s+(.+?\d{4})\s+-\s+(.+)$", re.I)
PERIOD_RX = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?\s*-\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$")


def parse_period(txt):
    m = PERIOD_RX.match(txt.strip())
    if not m:
        return None
    d1, m1, y1, d2, m2, y2 = int(m[1]), MON.get(m[2].lower()), m[3], int(m[4]), MON.get(m[5].lower()), int(m[6])
    if not m1 or not m2:
        return None
    y1 = int(y1) if y1 else (y2 - 1 if m1 > m2 else y2)
    try:
        start, end = dt.date(y1, m1, d1), dt.date(y2, m2, d2)
    except ValueError:
        return None
    if end < start or (end - start).days > 12:
        return None
    today = NOW.date()
    if end > today + dt.timedelta(days=2) or end < today - dt.timedelta(days=400):
        return None
    return start, end


def classify(raw):
    """Devolve (kind, phase, esquema). Nunca transforma categoria ambígua em erupção."""
    r = norm(raw)
    m = re.match(r"^(activity|unrest)\s*\((new|continuing)\)$", r)
    if m:
        return ("atividade" if m[1] == "activity" else "agitacao",
                "nova" if m[2] == "new" else "continua", "2026")
    if r == "other":
        return "outros", "nao_aplicavel", "2026"
    if r.startswith("new activity/unrest") or r == "new activity / unrest":
        return "desconhecido", "nova", "antigo"
    if r.startswith("continuing activity"):
        return "atividade", "continua", "antigo"
    return "desconhecido", "desconhecida", "desconhecido"


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def rss_items(body):
    """Lê os itens do RSS. Se o XML tiver erro de caracteres, faz leitura tolerante bloco a bloco,
    mas só aceita se o arquivo estiver completo e todo <item> aberto tiver sido fechado."""
    if not re.search(r"</rss>\s*$", body.strip(), re.I) and not re.search(r"</channel>", body, re.I):
        raise FetchError("resposta incompleta (sem </channel> nem </rss>)")
    out = []
    try:
        root = ET.fromstring(body.encode("utf-8"))
        for it in root.iter():
            if local(it.tag) != "item":
                continue
            d = {}
            for ch in it:
                d.setdefault(local(ch.tag), (ch.text or "").strip())
            out.append(d)
        return out, "xml"
    except ET.ParseError:
        pass
    opened = len(re.findall(r"<item[\s>]", body, re.I))
    blocks = re.findall(r"<item[\s>].*?</item>", body, re.S | re.I)
    if not blocks or len(blocks) != opened:
        raise FetchError(f"XML inválido e itens inconsistentes ({len(blocks)} completos de {opened})")
    for blk in blocks:
        d = {}
        for tag in ("title", "link", "guid", "category", "point", "lat", "long"):
            m = re.search(rf"<(?:\w+:)?{tag}[^>]*>(.*?)</(?:\w+:)?{tag}>", blk, re.S | re.I)
            if m:
                d[tag] = unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m[1], flags=re.S)).strip()
        out.append(d)
    return out, "tolerante"


def collect_gvp(prev):
    src = dict(prev or {})
    src["lastAttemptAt"] = ISO(NOW)
    try:
        st, ct, body = fetch(GVP_RSS, accept="application/rss+xml, application/xml, text/xml")
        if not re.search(r"<rss[\s>]|<feed[\s>]", body, re.I):
            raise FetchError(f"resposta não é RSS (Content-Type {ct or '?'})")
        raw_items, mode = rss_items(body)
        if not raw_items:
            raise FetchError("RSS sem itens")
        cat = load_catalog()
        by_vn, by_nc = catalog_index(cat)
        parsed, rejected, schemes = [], {}, set()
        for d in raw_items:
            title = unescape(d.get("title", "")).strip()
            m = TITLE_RX.match(title)
            if not m:
                rejected["título fora do padrão"] = rejected.get("título fora do padrão", 0) + 1
                continue
            per = parse_period(m[3])
            if not per:
                rejected["período não interpretável"] = rejected.get("período não interpretável", 0) + 1
                continue
            category_raw = m[4].strip() or d.get("category", "").strip()
            kind, phase, scheme = classify(category_raw)
            schemes.add(scheme)
            if scheme == "desconhecido":
                log(f"AVISO categoria não reconhecida: {category_raw!r} ({m[1].strip()})")
            ids = re.search(r"(?:vn=|-)(\d{6})(?!\d)", (d.get("link", "") + " " + d.get("guid", "")))
            vnum = ids[1] if ids else None
            lat = lon = None
            coord_src = "ausente"
            pt = d.get("point", "").split()
            if len(pt) >= 2:
                lat, lon = coord(pt[0], pt[1])
            if lat is None and d.get("lat") and d.get("long"):
                lat, lon = coord(d["lat"], d["long"])
            if lat is not None:
                coord_src = "rss"
            name, country = m[1].strip(), m[2].strip()
            if lat is None:
                hit = by_vn.get(vnum) if vnum else None
                if not hit:
                    cands = by_nc.get((norm(name), norm(country)), [])
                    hit = cands[0] if len(cands) == 1 else None   # só nome + país idênticos e únicos
                    if hit and not vnum:
                        vnum = hit["vnum"]
                if hit:
                    lat, lon = coord(hit["lat"], hit["lon"])
                    if lat is not None:
                        coord_src = "catalogo_gvp"
            parsed.append({"vnum": vnum, "name": name, "country": country,
                           "lat": lat, "lon": lon, "coordSource": coord_src,
                           "categoryRaw": category_raw, "kind": kind, "phase": phase, "_per": per})
        if not parsed:
            raise FetchError("nenhum item válido (" + ", ".join(f"{k}: {v}" for k, v in rejected.items()) + ")")
        freq = {}
        for p in parsed:
            freq[p["_per"]] = freq.get(p["_per"], 0) + 1
        per = max(freq, key=lambda k: (freq[k], k[1]))
        if freq[per] < DOMINANT_MIN_SHARE * len(parsed):
            raise FetchError(f"períodos misturados: o principal reúne só {freq[per]} de {len(parsed)} itens")
        sel = [p for p in parsed if p["_per"] == per]
        # deduplicação: pelo vnum quando existir; sem vnum, só nome + país idênticos
        seen, items = set(), []
        for p in sel:
            key = ("v", p["vnum"]) if p["vnum"] else ("n", norm(p["name"]), norm(p["country"]))
            if key in seen:
                continue
            seen.add(key)
            p.pop("_per")
            items.append(p)
        if len(items) < MIN_GVP_ITEMS:
            raise FetchError(f"apenas {len(items)} itens válidos no período (mínimo {MIN_GVP_ITEMS})")
        start, end = per
        prev_end = (prev or {}).get("periodEnd")
        if prev_end and end.isoformat() < prev_end:
            src.update(lastFetchStatus="rejeitado",
                       lastError=f"relatório recebido ({end}) mais antigo que o guardado ({prev_end})")
            log("GVP: " + src["lastError"])
            return src
        # mesmo período com MENOS itens que o guardado: só substitui se se repetir na coleta seguinte
        # (protege contra resposta parcial sem bloquear revisão oficial legítima)
        if prev_end and end.isoformat() == prev_end and len(items) < (prev or {}).get("itemCount", 0):
            pend = (prev or {}).get("pendingSmaller") or {}
            sig = f"{end.isoformat()}:{len(items)}"
            if pend.get("sig") != sig:
                src.update(lastFetchStatus="rejeitado", pendingSmaller={"sig": sig},
                           lastError=f"mesmo período com {len(items)} itens (guardado: {prev['itemCount']}); aguardando confirmação na próxima coleta")
                log("GVP: " + src["lastError"])
                return src
            log(f"GVP: versão menor do mesmo período confirmada em duas coletas seguidas; aceita como revisão")
        src.pop("pendingSmaller", None)
        if len(schemes) > 1:
            schemes.discard("desconhecido")
        scheme = schemes.pop() if len(schemes) == 1 else ("misto" if schemes else "desconhecido")
        src.update(lastFetchStatus="ok", lastSuccessAt=ISO(NOW), lastError=None,
                   categoryScheme=scheme, periodStart=start.isoformat(), periodEnd=end.isoformat(),
                   itemCount=len(items), items=items)
        cs = {k: sum(1 for i in items if i["coordSource"] == k) for k in ("rss", "catalogo_gvp", "ausente")}
        log(f"GVP ok (leitura {mode}): {len(items)} vulcões, período {start} a {end}, esquema {scheme}; "
            f"coordenadas: {cs['rss']} do RSS, {cs['catalogo_gvp']} do catálogo, {cs['ausente']} ausentes; "
            f"com vnum: {sum(1 for i in items if i['vnum'])}" + (f"; rejeitados: {rejected}" if rejected else ""))
    except Exception as e:
        src.update(lastFetchStatus="erro", lastError=str(e)[:300])
        log(f"GVP ERRO: {e}")
    return src


# ---------------------------------------------------------------- USGS HANS
def pick(d, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def collect_hans(prev):
    src = dict(prev or {})
    src["lastAttemptAt"] = ISO(NOW)
    try:
        st, ct, body = fetch(HANS, accept="application/json")
        data = json.loads(body)
        lst = data if isinstance(data, list) else None
        if lst is None and isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    lst = v
                    break
        if lst is None:
            raise FetchError("resposta sem lista de vulcões")
        items = []
        for v in lst:
            if not isinstance(v, dict):
                continue
            vn = pick(v, "vnum", "volcano_number")
            items.append({"vnum": str(vn) if vn else None,
                          "name": pick(v, "volcano_name", "volcanoName", "name"),
                          "alertLevel": pick(v, "alert_level", "alertLevel"),
                          "colorCode": pick(v, "color_code", "colorCode"),
                          "sentUtc": pick(v, "sent_utc", "sentUtc"),
                          "noticeUrl": pick(v, "notice_url", "noticeUrl")})
        items = [i for i in items if i["name"]]
        # resposta comprovadamente mais antiga: o mesmo vulcão com aviso anterior ao já guardado
        old = {i.get("vnum") or i.get("name"): i.get("sentUtc") for i in (prev or {}).get("items") or []}
        for i in items:
            o = old.get(i["vnum"] or i["name"])
            if o and i["sentUtc"] and str(i["sentUtc"]) < str(o):
                raise FetchError(f"resposta mais antiga que a guardada ({i['name']}: {i['sentUtc']} < {o})")
        sent = sorted(i["sentUtc"] for i in items if i["sentUtc"])
        src.update(lastFetchStatus="ok", lastSuccessAt=ISO(NOW), lastError=None,
                   itemCount=len(items), dataUpdatedAt=sent[-1] if sent else None, items=items)
        log(f"USGS HANS ok: {len(items)} vulcões em alerta elevado")
    except Exception as e:
        src.update(lastFetchStatus="erro", lastError=str(e)[:300])
        log(f"USGS HANS ERRO: {e}")
    return src


# ---------------------------------------------------------------- GeoNet
def collect_geonet(prev):
    src = dict(prev or {})
    src["lastAttemptAt"] = ISO(NOW)
    try:
        st, ct, body = fetch(GEONET, accept="application/vnd.geo+json;version=2")
        feats = json.loads(body).get("features", [])
        items = []
        for f in feats:
            p = f.get("properties") or {}
            c = (f.get("geometry") or {}).get("coordinates") or [None, None]
            lat, lon = coord(c[1], c[0]) if len(c) >= 2 else (None, None)
            items.append({"volcanoId": p.get("volcanoID"), "name": p.get("volcanoTitle"),
                          "level": p.get("level"), "activity": p.get("activity"),
                          "lat": lat, "lon": lon})
        items = [i for i in items if i["volcanoId"] and isinstance(i["level"], int)]
        items = [i for i in items if 0 <= i["level"] <= 5]
        if len(items) < MIN_GEONET_ITEMS:
            raise FetchError(f"apenas {len(items)} vulcões (esperado ≥{MIN_GEONET_ITEMS})")
        src.update(lastFetchStatus="ok", lastSuccessAt=ISO(NOW), lastError=None,
                   itemCount=len(items), dataUpdatedAt=None, items=items)
        log(f"GeoNet ok: {len(items)} vulcões; nível ≥2: "
            + (", ".join(f"{i['name']} ({i['level']})" for i in items if i["level"] >= 2) or "nenhum"))
    except Exception as e:
        src.update(lastFetchStatus="erro", lastError=str(e)[:300])
        log(f"GeoNet ERRO: {e}")
    return src


# ---------------------------------------------------------------- gravação
VOLATILE = {"writtenAt", "lastRunAt", "lastAttemptAt", "lastSuccessAt"}  # datas que mudam a cada execução


def stable(o):
    if isinstance(o, dict):
        return {k: stable(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [stable(v) for v in o]
    return o


def main():
    try:
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
        if prev.get("schema") != 1:
            prev = {}
    except Exception:
        prev = {}
    ps = prev.get("sources", {})
    doc = {"schema": 1, "writtenAt": None, "lastRunAt": ISO(NOW), "sources": {
        "gvp_weekly": collect_gvp(ps.get("gvp_weekly")),
        "usgs_hans": collect_hans(ps.get("usgs_hans")),
        "geonet_val": collect_geonet(ps.get("geonet_val")),
    }}
    changed = stable(doc) != stable(prev)
    last_run = prev.get("lastRunAt")
    stale = True
    if last_run:
        age_h = (NOW - dt.datetime.fromisoformat(last_run.replace("Z", "+00:00"))).total_seconds() / 3600
        stale = age_h >= HEARTBEAT_H
    if changed or stale:
        doc["writtenAt"] = ISO(NOW)
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        log(f"JSON gravado ({os.path.getsize(OUT)/1024:.1f} KB) — motivo: "
            + ("conteúdo mudou" if changed else f"regravação periódica ({HEARTBEAT_H} h)"))
    else:
        log("Conteúdo igual ao anterior; JSON não regravado.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Coletor de vulcões\n\n" + "\n".join(f"- {l}" for l in LOG) + "\n")
    if all(s.get("lastFetchStatus") == "erro" for s in doc["sources"].values()):
        sys.exit(1)   # tudo falhou: a execução aparece em vermelho no histórico


if __name__ == "__main__":
    main()
