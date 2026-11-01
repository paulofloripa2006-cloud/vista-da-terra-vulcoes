#!/usr/bin/env python3
"""Testes do coletor VAAC experimental com os 5 XML OFICIAIS da NOAA (01/11/2026).

Casos marcados [DERIVADO] sao gerados por programa a partir de um XML oficial,
alterando um unico ponto, para cobrir situacoes raras (correcao com mesma
emissao, versao desconhecida, linha de data etc.). Nenhum caso usa XML inventado
do zero. Rodar:  python3 -m unittest -v tests/test_vaac_collector.py
"""
import datetime as dt
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import vaac_collector as v  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "oficiais")
BASE = "https://prod-east-goes.woc.noaa.gov/products/atmosphere/vaac/volcanoes/xml_files/"
UTC = dt.timezone.utc
FILES = ["FVXX22_20261101_1559.xml", "FVXX23_20261101_1025.xml", "FVXX23_20261101_1031.xml",
         "FVXX21_20261101_0814.xml", "FVXX21_20261101_0818.xml"]
NOW = dt.datetime(2026, 11, 1, 16, 30, tzinfo=UTC)   # Fuego, Reventador e Santa Maria coexistem


def raw(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def index(names):
    """Pagina indice no mesmo layout da NOAA (secao de 24 h + secao de 15 dias)."""
    links = "".join('<a href="%s">(XML)</a><br>' % (BASE + n) for n in names)
    return ("<html><body><h1>Current Volcanic Ash Advisories</h1>"
            "<p>Advisories from the past 24 hours</p>%s"
            "<p>Advisories from the past 15 days</p>%s</body></html>" % (links, links)).encode()


class Fake:
    """Simula a rede: url -> bytes, ou excecao a lancar (pode ser lista por tentativa)."""
    def __init__(self, files, extra=None):
        self.map = {v.PAGE: index(files)}
        for n in files:
            self.map[BASE + n] = raw(n) if os.path.exists(os.path.join(FIX, n)) else b""
        self.map.update(extra or {})
        self.calls = []

    def __call__(self, url, timeout=None, maxbytes=None):
        self.calls.append(url)
        r = self.map[url]
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        return r


def derive(name, *subs):
    """[DERIVADO] aplica substituicoes exatas no XML oficial; falha se nao encontrar."""
    s = raw(name).decode()
    for a, b in subs:
        assert a in s, "trecho nao encontrado: " + a[:60]
        s = s.replace(a, b, 1)
    return s.encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "vaac_experimental.json")

    def run_with(self, fake, now=NOW, clock=None):
        kw = {"fetcher": fake, "now": now}
        if clock:
            kw["clock"] = clock
        res = v.run(self.out, None, **kw)
        with open(self.out, encoding="utf8") as f:
            return res, json.load(f)


# ---------------------------------------------------------------- leitura
class T01_LeituraOficial(Base):
    def test_cinco_envelopes_oficiais_aceitos(self):
        for n in FILES:
            rows, errs = v.parse_document(raw(n), BASE + n, NOW)
            self.assertEqual(len(rows), 1, n)
            self.assertEqual(errs, [], n)

    def test_fuego_observado(self):
        r = v.parse_document(raw(FILES[0]), BASE + FILES[0], NOW)[0][0]
        self.assertEqual(r["volcanoKey"], "342090")
        self.assertEqual(r["cloud"]["kind"], "observada")
        self.assertIs(r["cloud"]["isEstimated"], False)
        L = r["cloud"]["layers"][0]
        self.assertEqual((L["lower"], L["upperFL"], L["vertices"]), ("GND", 140.0, 5))
        self.assertEqual((L["motionDeg"], L["speedKt"]), (315.0, 15.0))
        self.assertTrue(r["cloud"]["displayable"])
        a = r["advisory"]
        self.assertEqual(a["observationUtc"], "2026-11-01T15:20:00Z")
        self.assertEqual(a["expiresAtUtc"], "2026-11-02T01:00:00Z")
        self.assertEqual(a["forecastsIgnored"], 3)
        self.assertEqual(r["volcano"]["summit"], {"value": 12346.0, "uom": "[ft_i]", "meters": 3763})
        self.assertNotIn("14.283", json.dumps(r))   # vertice da previsao +6 h nunca entra

    def test_reventador_estimado_e_correcao(self):
        a = v.parse_document(raw(FILES[3]), BASE + FILES[3], NOW)[0][0]
        b = v.parse_document(raw(FILES[4]), BASE + FILES[4], NOW)[0][0]
        for r in (a, b):
            self.assertIs(r["cloud"]["isEstimated"], True)
            self.assertEqual(r["cloud"]["kind"], "estimada")
        self.assertEqual(a["advisory"]["reportStatus"], "NORMAL")
        self.assertEqual(b["advisory"]["reportStatus"], "CORRECTION")
        self.assertIsNone(a["advisory"]["bulletinBBB"])
        self.assertEqual(b["advisory"]["bulletinBBB"], "CCA")
        self.assertNotEqual(a["key"], b["key"])

    def test_santa_maria_sem_poligono_e_serie_encerrada(self):
        a = v.parse_document(raw(FILES[1]), BASE + FILES[1], NOW)[0][0]
        b = v.parse_document(raw(FILES[2]), BASE + FILES[2], NOW)[0][0]
        for r in (a, b):
            self.assertEqual(r["cloud"]["status"], "NOT_IDENTIFIABLE")
            self.assertEqual(r["cloud"]["layers"], [])
            self.assertFalse(r["cloud"]["displayable"])
            self.assertTrue(r["advisory"]["noFurtherAdvisoryScheduled"])
        self.assertFalse(a["advisory"]["ashEmissionEndedReported"])   # 10:25 sem detalhe
        self.assertTrue(b["advisory"]["ashEmissionEndedReported"])    # 10:31 "VA ENS ENDED"


# ------------------------------------------------------------- execucao
class T02_ExecucaoCompleta(Base):
    def test_cinco_oficiais(self):
        res, d = self.run_with(Fake(FILES))
        self.assertEqual(res, "OK")
        self.assertEqual(sorted(d["active"]), ["342090", "352010"])
        self.assertEqual(d["active"]["352010"]["advisory"]["reportStatus"], "CORRECTION")
        self.assertEqual(d["active"]["352010"]["advisory"]["replaces"].split("|")[4], "NORMAL")
        sm = [r for r in d["closed"] if r["volcanoKey"] == "342030"]
        self.assertTrue(any(r["reason"] == "serie_encerrada_emissao_de_cinzas_encerrada_relatada" for r in sm))
        self.assertNotIn("342030", d["active"])
        c = d["collector"]["counts"]
        self.assertEqual((c["found"], c["downloaded"], c["accepted"], c["active"]), (5, 5, 5, 2))
        self.assertIsNotNone(d["collector"]["lastSuccessUtc"])

    def test_ordem_invertida_entre_execucoes(self):
        self.run_with(Fake([FILES[4]]))                       # correcao chega primeiro
        res, d = self.run_with(Fake([FILES[4], FILES[3]]))    # original chega depois
        self.assertEqual(d["active"]["352010"]["advisory"]["reportStatus"], "CORRECTION")
        self.assertEqual(d["collector"]["counts"]["older"], 1)

    def test_nunca_ressuscita(self):
        self.run_with(Fake(FILES))
        with open(self.out) as f:
            d = json.load(f)
        d["seen"] = {}                                        # simula perda do 'seen'
        with open(self.out, "w") as f:
            json.dump(d, f)
        res, d = self.run_with(Fake(FILES), now=NOW + dt.timedelta(minutes=30))
        self.assertNotIn("342030", d["active"])               # Santa Maria segue encerrado
        self.assertEqual(d["active"]["352010"]["advisory"]["reportStatus"], "CORRECTION")

    def test_ja_vistos_nao_rebaixam(self):
        self.run_with(Fake(FILES))
        fake = Fake(FILES)
        self.run_with(fake, now=NOW + dt.timedelta(hours=1))
        self.assertEqual(fake.calls, [v.PAGE])


class T03_Expiracao(Base):
    def test_expira_pelo_relogio(self):
        self.run_with(Fake(FILES))
        res, d = self.run_with(Fake(FILES), now=dt.datetime(2026, 11, 2, 1, 1, tzinfo=UTC))
        self.assertNotIn("342090", d["active"])               # Fuego venceu 01:00Z
        self.assertNotIn("352010", d["active"])               # Reventador venceu 17:15Z
        self.assertGreaterEqual(d["collector"]["counts"]["expired"], 2)

    def test_falha_da_pagina_mantem_estado_e_expira(self):
        self.run_with(Fake(FILES))
        fake = Fake(FILES, {v.PAGE: v.TempError("rede", "sem rede")})
        res, d = self.run_with(fake, now=dt.datetime(2026, 11, 1, 19, 0, tzinfo=UTC))
        self.assertEqual(res, "FALHA")
        self.assertIn("342090", d["active"])                  # ainda valido
        self.assertNotIn("352010", d["active"])               # venceu 17:15Z mesmo sem coleta
        self.assertEqual(d["collector"]["lastSuccessUtc"], "2026-11-01T16:30:00Z")

    def test_virada_utc(self):
        x = derive(FILES[0], ("2026-11-01T15:59:00Z", "2026-11-01T23:55:00Z"),
                   ("2026-11-01T15:20:00Z", "2026-11-01T23:20:00Z"),
                   ("2026-11-01T22:00:00Z", "2026-11-02T05:55:00Z"))   # [DERIVADO]
        name = "FVXX22_20261101_2355.xml"
        res, d = self.run_with(Fake([], {v.PAGE: index([name]), BASE + name: x}),
                               now=dt.datetime(2026, 11, 2, 0, 30, tzinfo=UTC))
        self.assertEqual(d["active"]["342090"]["advisory"]["expiresAtUtc"], "2026-11-02T08:55:00Z")


class T04_ErrosDeRede(Base):
    def test_404_tenta_ate_6_vezes(self):
        n = FILES[0]
        for i in range(1, 7):
            fake = Fake([n], {BASE + n: v.TempError("http404", "HTTP 404")})
            res, d = self.run_with(fake, now=NOW + dt.timedelta(hours=i))
            if i < 6:
                self.assertEqual(d["pending"][BASE + n]["attempts"], i)
        self.assertNotIn(BASE + n, d["pending"])
        self.assertEqual(d["seen"][BASE + n]["result"], "invalido")
        self.assertIn("404 em 6 tentativas", d["rejected"][-1]["reason"])

    def test_404_depois_disponivel(self):
        n = FILES[0]
        self.run_with(Fake([n], {BASE + n: v.TempError("http404", "HTTP 404")}))
        res, d = self.run_with(Fake([n]), now=NOW + dt.timedelta(hours=1))
        self.assertIn("342090", d["active"])
        self.assertEqual(d["pending"], {})

    def test_5xx_e_rede_ficam_pendentes(self):
        for err in (v.TempError("http5xx", "HTTP 503"), v.TempError("rede", "timeout")):
            self.setUp()
            res, d = self.run_with(Fake([FILES[0]], {BASE + FILES[0]: err}))
            self.assertEqual(res, "PARCIAL")
            self.assertIn(BASE + FILES[0], d["pending"])
            self.assertNotIn(BASE + FILES[0], d["seen"])

    def test_malformado_exige_duas_leituras_iguais(self):
        n = FILES[0]
        cut = v.body_of(raw(n)[:3000], True)                  # [DERIVADO] tamanho conferido
        self.run_with(Fake([n], {BASE + n: cut}))
        cut2 = v.body_of(bytes(cut) + b"x", True)
        res, d = self.run_with(Fake([n], {BASE + n: cut2}), now=NOW + dt.timedelta(hours=1))
        self.assertIn(BASE + n, d["pending"])                 # conteudo diferente: segue pendente
        res, d = self.run_with(Fake([n], {BASE + n: cut2}), now=NOW + dt.timedelta(hours=2))
        self.assertEqual(d["seen"][BASE + n]["result"], "invalido")

    def test_pendente_que_sai_do_indice(self):
        n = FILES[0]
        self.run_with(Fake([n], {BASE + n: v.TempError("http5xx", "HTTP 502")}))
        res, d = self.run_with(Fake([FILES[3]]), now=NOW + dt.timedelta(hours=1))
        self.assertNotIn(BASE + n, d["pending"])
        self.assertTrue(any("saiu do indice" in r["reason"] for r in d["rejected"]))


class T05_FetchReal(unittest.TestCase):
    """fetch() verdadeiro contra servidor HTTP local servindo o XML oficial."""
    @classmethod
    def setUpClass(cls):
        body = raw(FILES[0])

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                if self.path == "/ok.xml":
                    self.send_response(200); self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                elif self.path == "/trunc.xml":
                    self.send_response(200); self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body[:2000])
                elif self.path == "/503.xml":
                    self.send_response(503); self.end_headers()
                else:
                    self.send_response(404); self.end_headers()
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.allow = (("http", "127.0.0.1"),)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def test_ok(self):
        self.assertEqual(v.fetch(self.url + "/ok.xml", 5, allowed=self.allow), raw(FILES[0]))

    def test_truncado_e_temporario(self):
        with self.assertRaises(v.TempError) as c:
            v.fetch(self.url + "/trunc.xml", 5, allowed=self.allow)
        self.assertEqual(c.exception.kind, "truncado")

    def test_404_e_503(self):
        for p, k in (("/x.xml", "http404"), ("/503.xml", "http5xx")):
            with self.assertRaises(v.TempError) as c:
                v.fetch(self.url + p, 5, allowed=self.allow)
            self.assertEqual(c.exception.kind, k)

    def test_host_nao_permitido(self):
        with self.assertRaises(v.PermError):
            v.fetch(self.url + "/ok.xml", 5)


# ------------------------------------------------------- casos derivados
class T06_Estrutura(Base):
    def test_dois_avisos_um_invalido(self):
        fu, re_ = raw(FILES[0]).decode(), raw(FILES[3]).decode()
        info = lambda s: re.search(r"<meteorologicalInformation>.*</meteorologicalInformation>", s, re.S).group(0)
        bad = info(re_).replace("<advisoryNumber>2026/1151</advisoryNumber>", "")
        x = fu.replace(info(fu), info(fu) + bad).encode()     # [DERIVADO]
        rows, errs = v.parse_document(x, "u", NOW)
        self.assertEqual([r["volcanoKey"] for r in rows], ["342090"])
        self.assertEqual(len(errs), 1)
        self.assertIn("Numero do aviso ausente", errs[0]["reason"])

    def test_versao_nao_testada_em_quarentena(self):
        x = derive(FILES[0], ('xmlns="http://icao.int/iwxxm/3.0"', 'xmlns="http://icao.int/iwxxm/2023-1"'))
        with self.assertRaises(v.PermError) as c:
            v.parse_document(x, "u", NOW)
        self.assertIn("nao testada", str(c.exception))

    def test_count_divergente(self):
        x = derive(FILES[0], ('posList count="6"', 'posList count="7"'))
        r = v.parse_document(x, "u", NOW)[0][0]
        self.assertEqual(r["cloud"]["layers"], [])
        self.assertFalse(r["cloud"]["displayable"])
        self.assertIn("count declarado", r["cloud"]["rejectedLayerReasons"][0])

    def test_poligono_degenerado(self):
        x = derive(FILES[0], ("14.750 -91.583 14.717 -91.817 14.500 -91.717 14.450 -90.867 14.483 -90.867 14.750 -91.583",
                              "14.750 -91.583 14.750 -91.583 14.750 -91.583 14.750 -91.584 14.750 -91.585 14.750 -91.583"))
        r = v.parse_document(x, "u", NOW)[0][0]
        self.assertFalse(r["cloud"]["displayable"])
        self.assertTrue(r["cloud"]["rejectedLayerReasons"])

    def test_camada_invalida_ainda_substitui_aviso_antigo(self):
        x = derive(FILES[4], ('posList count="5">0.067 -77.884', 'posList count="9">0.067 -77.884'))
        name = "FVXX21_20261101_0818.xml"
        self.run_with(Fake([FILES[3]]))
        res, d = self.run_with(Fake([], {v.PAGE: index([name]), BASE + name: x}), now=NOW + dt.timedelta(minutes=5))
        a = d["active"]["352010"]
        self.assertEqual(a["advisory"]["reportStatus"], "CORRECTION")
        self.assertFalse(a["cloud"]["displayable"])            # nao desenha, mas o antigo saiu

    def test_linha_de_data(self):
        x = derive(FILES[0], ("14.750 -91.583 14.717 -91.817 14.500 -91.717 14.450 -90.867 14.483 -90.867 14.750 -91.583",
                              "52.000 179.500 52.200 -179.600 51.800 -179.700 51.700 179.400 52.000 179.500 52.000 179.500"),
                   ('posList count="6"', 'posList count="6"'))
        x = x.replace(b"52.000 179.500 52.000 179.500", b"52.000 179.500", 1)
        x = x.replace(b'posList count="6">52.000', b'posList count="5">52.000', 1)
        r = v.parse_document(x, "u", NOW)[0][0]
        L = r["cloud"]["layers"][0]
        self.assertTrue(L["crossesDateline"])
        self.assertLess(L["areaKm2"], 10000)                  # sem "volta ao mundo"

    def test_unknown_isolado(self):
        a = derive(FILES[0], ("FUEGO 342090", "UNKNOWN"))
        b = derive(FILES[3], ("REVENTADOR 352010", "UNKNOWN"))
        n1, n2 = "FVXX22_20261101_1559.xml", "FVXX21_20261101_0814.xml"
        res, d = self.run_with(Fake([], {v.PAGE: index([n1, n2]), BASE + n1: a, BASE + n2: b}))
        keys = sorted(d["active"])
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(k.startswith("unverified:unknown:") for k in keys))


class T07_Correcoes(Base):
    def _pair(self, bbb1, bbb2, st1="CORRECTION", st2="CORRECTION"):
        base = raw(FILES[4]).decode()
        mk = lambda bbb, st: base.replace("KNES010818CCA_C_KNES_20261101082108",
                                          "KNES010818%s_C_KNES_2026110108%s" % (bbb, "2108" if bbb != "CCB" else "2500")
                                          ).replace('reportStatus="CORRECTION"', 'reportStatus="%s"' % st).encode()
        return mk(bbb1, st1), mk(bbb2, st2)

    def _run_pair(self, x1, x2):
        n1, n2 = "FVXX21_20261101_0818.xml", "FVXX21_20261101_0819.xml"
        self.run_with(Fake([], {v.PAGE: index([n1]), BASE + n1: x1}))
        return self.run_with(Fake([], {v.PAGE: index([n1, n2]), BASE + n1: x1, BASE + n2: x2}),
                             now=NOW + dt.timedelta(minutes=5))[1]

    def test_mesma_emissao_cca_cab_ccb(self):          # [DERIVADO]
        cca, ccb = self._pair("CCA", "CCB")
        for first, second in ((cca, ccb), (ccb, cca)):
            self.setUp()
            d = self._run_pair(first, second)
            self.assertEqual(d["active"]["352010"]["advisory"]["bulletinBBB"], "CCB")

    def test_mesma_emissao_correction_vence_normal(self):
        x = derive(FILES[4], ("2026-11-01T08:18:00Z", "2026-11-01T08:14:00Z"),
                   ("KNES010818CCA", "KNES010814CCA"))          # [DERIVADO]
        n1, n2 = FILES[3], "FVXX21_20261101_0814B.xml".replace("B", "")
        d = self._run_pair(raw(FILES[3]), x)
        self.assertEqual(d["active"]["352010"]["advisory"]["reportStatus"], "CORRECTION")

    def test_empate_sem_encerramento_mantem_anterior(self):
        a = raw(FILES[3])
        b = derive(FILES[3], ("_C_KNES_20261101081638", "_C_KNES_20261101081900"))   # [DERIVADO]
        d = self._run_pair(a, b)
        self.assertIn("352010", d["conflicts"])
        self.assertNotIn("conflito_de_versoes", d["active"]["352010"]["cloud"]["warnings"])  # conflito nao bloqueou
        self.assertIn("20261101081638", d["active"]["352010"]["advisory"]["bulletinId"])

    def test_empate_com_encerramento_bloqueia_exibicao(self):
        a = raw(FILES[3])
        b = derive(FILES[3], ("_C_KNES_20261101081638", "_C_KNES_20261101081900"),
                   ('<nextAdvisoryTime><gml:TimeInstant gml:id="uuid.96785b7c-5e6a-45bf-bee1-53dfc08dbceb">'
                    '<gml:timePosition indeterminatePosition="before">2026-11-01T14:15:00Z</gml:timePosition>'
                    '</gml:TimeInstant></nextAdvisoryTime>',
                    '<nextAdvisoryTime nilReason="http://codes.wmo.int/common/nil/inapplicable" />'))   # [DERIVADO]
        d = self._run_pair(a, b)
        self.assertIn("352010", d["conflicts"])
        self.assertFalse(d["active"]["352010"]["cloud"]["displayable"])
        self.assertIn("conflito_de_versoes", d["active"]["352010"]["cloud"]["warnings"])


class T08_Orcamento(Base):
    def test_parcial_e_retomada(self):
        t = {"v": 0.0}
        def clock():
            t["v"] += 40.0
            return t["v"]
        res, d = self.run_with(Fake(FILES), clock=clock)
        self.assertEqual(res, "PARCIAL")
        self.assertGreater(d["collector"]["counts"]["notStartedBudget"], 0)
        self.assertEqual(d["pending"], {})                    # nao iniciados nao viram pendencia
        res, d = self.run_with(Fake(FILES), now=NOW + dt.timedelta(minutes=10))
        self.assertEqual(sorted(d["active"]), ["342090", "352010"])

    def test_mais_novos_primeiro(self):
        fake = Fake(FILES)
        self.run_with(fake)
        got = [c.rsplit("/", 1)[-1] for c in fake.calls[1:]]
        self.assertEqual(got, sorted(FILES, key=lambda n: n.split("_", 1)[1], reverse=True))


class T09_Integridade(Base):
    def test_json_existente_invalido_nao_e_sobrescrito(self):
        with open(self.out, "w") as f:
            f.write('{"schema":"outro"}')
        with self.assertRaises(ValueError):
            v.run(self.out, None, fetcher=Fake(FILES), now=NOW)
        with open(self.out) as f:
            self.assertEqual(f.read(), '{"schema":"outro"}')

    def test_previsoes_nunca_gravadas(self):
        res, d = self.run_with(Fake(FILES))
        s = json.dumps(d)
        for fc in ("14.283", "14.117", "0.117"):              # vertices so existentes em previsoes
            self.assertNotIn(fc, s)


# ------------------------------------------------- achados da revisao do GPT
class T10_RevisaoGPT(Base):
    def test_g1_dois_avisos_no_mesmo_meteorologicalInformation(self):
        fu, re_ = raw(FILES[0]).decode(), raw(FILES[3]).decode()
        adv = lambda s: re.search(r"<VolcanicAshAdvisory .*</VolcanicAshAdvisory>", s, re.S).group(0)
        x = fu.replace(adv(fu), adv(fu) + adv(re_)).encode()          # [DERIVADO]
        rows, errs = v.parse_document(x, "u", NOW)
        self.assertEqual(sorted(r["volcanoKey"] for r in rows), ["342090", "352010"])
        self.assertEqual(errs, [])
        self.assertTrue(all("fora do padrao OMM" in r["advisory"]["structureWarnings"][0] for r in rows))

    def test_g2_seis_tentativas_mistas_nao_sao_seis_404(self):
        n = FILES[0]
        seq = [v.TempError("http404", "404"), v.TempError("http5xx", "503"), v.TempError("rede", "x"),
               v.TempError("rede", "x"), v.TempError("http5xx", "503"), v.TempError("http404", "404")]
        for i, e in enumerate(seq, 1):
            res, d = self.run_with(Fake([n], {BASE + n: e}), now=NOW + dt.timedelta(minutes=i))
        self.assertIn(BASE + n, d["pending"])
        self.assertEqual(d["pending"][BASE + n]["count404"], 2)
        self.assertEqual(d["pending"][BASE + n]["attempts"], 6)
        self.assertNotIn(BASE + n, d["seen"])

    def test_g3_poligono_autointersectante(self):
        x = derive(FILES[0], ("14.750 -91.583 14.717 -91.817 14.500 -91.717 14.450 -90.867 14.483 -90.867 14.750 -91.583",
                              "0.000 0.000 3.000 3.000 0.000 4.000 4.000 0.000 0.000 0.000"),
                   ('posList count="6"', 'posList count="5"'))          # [DERIVADO] caso do GPT
        r = v.parse_document(x, "u", NOW)[0][0]
        self.assertEqual(r["cloud"]["layers"], [])
        self.assertFalse(r["cloud"]["displayable"])
        self.assertIn("autointersectante", r["cloud"]["rejectedLayerReasons"][0])

    def test_g3b_poligonos_oficiais_nao_sao_autointersectantes(self):
        for n in (FILES[0], FILES[3], FILES[4]):
            r = v.parse_document(raw(n), "u", NOW)[0][0]
            self.assertEqual(len(r["cloud"]["layers"]), 1, n)

    def test_g4_sem_horario_de_observacao(self):
        x = re.sub(rb'(<VolcanicAshObservedOrEstimatedConditions[^>]*>)<phenomenonTime>.*?</phenomenonTime>',
                   rb"\1", raw(FILES[0]), count=1, flags=re.S)           # [DERIVADO]
        r = v.parse_document(x, "u", NOW)[0][0]
        self.assertIsNone(r["advisory"]["observationUtc"])
        self.assertFalse(r["cloud"]["displayable"])
        self.assertIn("sem_horario_de_observacao", r["cloud"]["warnings"])

    def test_g4b_horario_de_observacao_incoerente(self):
        x = derive(FILES[0], ("2026-11-01T15:20:00Z", "2026-10-28T15:20:00Z"))   # [DERIVADO] 4 dias antes
        r = v.parse_document(x, "u", NOW)[0][0]
        self.assertFalse(r["cloud"]["displayable"])
        self.assertIn("horario_de_observacao_incoerente", r["cloud"]["warnings"])

    def test_g5_catalogo_ausente_e_fora_do_catalogo(self):
        r = v.parse_document(raw(FILES[0]), "u", NOW, None)[0][0]
        self.assertEqual((r["volcano"]["gvpStatus"], r["volcano"]["gvpValid"]), ("declarado_nao_conferido", False))
        self.assertEqual(r["volcanoKey"], "342090")
        r = v.parse_document(raw(FILES[0]), "u", NOW, {"342090"})[0][0]
        self.assertEqual((r["volcano"]["gvpStatus"], r["volcano"]["gvpValid"]),
                         ("numero_no_catalogo_sem_conferencia", False))
        r = v.parse_document(raw(FILES[0]), "u", NOW, {"111111"})[0][0]
        self.assertEqual(r["volcano"]["gvpStatus"], "fora_do_catalogo")
        self.assertTrue(r["volcanoKey"].startswith("unverified:"))
        x = derive(FILES[0], ("FUEGO 342090", "FUEGO"))
        r = v.parse_document(x, "u", NOW, {"342090"})[0][0]
        self.assertEqual(r["volcano"]["gvpStatus"], "ausente")

    def test_g6_malformado_sem_tamanho_nunca_vira_permanente(self):
        n = FILES[0]
        cut = raw(n)[:3000]                                    # [DERIVADO] sem Content-Length
        for i in range(4):
            res, d = self.run_with(Fake([n], {BASE + n: cut}), now=NOW + dt.timedelta(hours=i))
        self.assertIn(BASE + n, d["pending"])
        self.assertEqual(d["pending"][BASE + n]["kind"], "malformado_sem_tamanho")
        self.assertNotIn(BASE + n, d["seen"])
        res, d = self.run_with(Fake([FILES[3]]), now=NOW + dt.timedelta(hours=5))   # saiu do indice
        self.assertTrue(any("saiu do indice" in r["reason"] for r in d["rejected"]))

    def test_g7_isEstimated_fora_de_true_false(self):
        for val in ("maybe", "", "TRUE "):
            x = derive(FILES[0], ('isEstimated="false"', 'isEstimated="%s"' % val))   # [DERIVADO]
            r = v.parse_document(x, "u", NOW)[0][0]
            if val == "TRUE ":
                self.assertIs(r["cloud"]["isEstimated"], True)
                continue
            self.assertIsNone(r["cloud"]["isEstimated"])
            self.assertFalse(r["cloud"]["displayable"])
            self.assertTrue(any(w.startswith("isEstimated_invalido") for w in r["cloud"]["warnings"]))

    def test_g7b_isEstimated_ausente(self):
        x = derive(FILES[0], (' isEstimated="false"', ""))
        r = v.parse_document(x, "u", NOW)[0][0]
        self.assertIn("isEstimated_ausente", r["cloud"]["warnings"])
        self.assertFalse(r["cloud"]["displayable"])


class T11_FetchSemTamanho(unittest.TestCase):
    """fetch() real: resposta HTTP 200 sem Content-Length (fechamento da conexao)."""
    @classmethod
    def setUpClass(cls):
        body = raw(FILES[0])

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(200)
                if self.path == "/ok.xml":
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body if self.path in ("/full.xml", "/ok.xml") else body[:2000])
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.allow = (("http", "127.0.0.1"),)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def test_sem_content_length_nao_comprovado(self):
        b = v.fetch(self.url + "/full.xml", 5, allowed=self.allow)
        self.assertEqual(bytes(b), raw(FILES[0]))
        self.assertFalse(b.length_verified)
        b = v.fetch(self.url + "/cut.xml", 5, allowed=self.allow)
        self.assertFalse(b.length_verified)
        with self.assertRaises(v.Malformed):
            v.parse_document(b, "u", NOW)

    def test_com_content_length_comprovado(self):
        b = v.fetch(self.url + "/ok.xml", 5, allowed=self.allow)
        self.assertTrue(b.length_verified)


# ----------------------------------------- achados da auditoria R3 do GPT
CAT = {"342090": {"name": "Fuego", "lat": 14.4748, "lon": -90.8806},       # valores do gvp_catalogo.json
       "352010": {"name": "Reventador", "lat": -0.077, "lon": -77.656},
       "342030": {"name": "Santa Maria", "lat": 14.757, "lon": -91.552}}


class T12_AuditoriaR3(Base):
    def test_a_proximo_aviso_absurdo_tem_teto(self):
        x = derive(FILES[0], ("2026-11-01T22:00:00Z", "2027-11-01T22:00:00Z"))   # [DERIVADO] caso do GPT
        r = v.parse_document(x, "u", NOW)[0][0]
        a = r["advisory"]
        self.assertEqual(a["nextAdvisoryState"], "incoerente")
        self.assertTrue(a["expiryFallback"])
        self.assertEqual(a["expiresAtUtc"], "2026-11-02T00:59:00Z")             # emissao + 9 h
        self.assertIn("proximo_aviso_incoerente", r["cloud"]["warnings"])

    def test_a2_teto_absoluto_15h(self):
        x = derive(FILES[0], ("2026-11-01T22:00:00Z", "2026-11-02T03:30:00Z"))   # [DERIVADO] 11h31 depois: plausivel
        a = v.parse_document(x, "u", NOW)[0][0]["advisory"]
        self.assertEqual(a["nextAdvisoryState"], "programado")
        self.assertEqual(a["expiresAtUtc"], "2026-11-02T06:30:00Z")            # 03:30 + 3 h = emissao + 14h31
        x = derive(FILES[0], ("2026-11-01T22:00:00Z", "2026-11-02T03:59:00Z"))   # 12h depois: limite
        r = v.parse_document(x, "u", NOW)[0][0]
        self.assertEqual(r["advisory"]["expiresAtUtc"], "2026-11-02T06:59:00Z")  # exatamente emissao + 15 h
        x = derive(FILES[0], ("2026-11-01T22:00:00Z", "2026-11-02T04:00:00Z"))   # 12h01: fora do plausivel
        a = v.parse_document(x, "u", NOW)[0][0]["advisory"]
        self.assertEqual((a["nextAdvisoryState"], a["expiresAtUtc"]), ("incoerente", "2026-11-02T00:59:00Z"))
        old = v.MAX_NEXT_H
        try:                                                  # teto continua valendo se a regra mudar
            v.MAX_NEXT_H = 48
            x = derive(FILES[0], ("2026-11-01T22:00:00Z", "2026-11-03T00:00:00Z"))
            r = v.parse_document(x, "u", NOW)[0][0]
            self.assertEqual(r["advisory"]["expiresAtUtc"], "2026-11-02T06:59:00Z")
            self.assertIn("validade_limitada_ao_teto", r["cloud"]["warnings"])
        finally:
            v.MAX_NEXT_H = old

    def test_a3_oficiais_dentro_do_plausivel(self):
        for n in (FILES[0], FILES[3], FILES[4]):
            a = v.parse_document(raw(n), "u", NOW)[0][0]["advisory"]
            self.assertEqual(a["nextAdvisoryState"], "programado", n)
            self.assertFalse(a["expiryFallback"], n)

    def test_b_identidade_divergente_nao_mescla(self):
        x = derive(FILES[0], ("FUEGO 342090", "REVENTADOR 342090"))             # [DERIVADO] caso do GPT
        r = v.parse_document(x, "u", NOW, CAT)[0][0]
        self.assertEqual(r["volcano"]["gvpStatus"], "identidade_divergente")
        self.assertFalse(r["volcano"]["gvpValid"])
        self.assertTrue(r["volcanoKey"].startswith("identity-conflict:342090:"))
        self.assertIn("identidade_do_vulcao_divergente", r["cloud"]["warnings"])

    def test_b2_coordenadas_distantes(self):
        x = derive(FILES[0], ("<gml:pos>14.467 -90.867</gml:pos>", "<gml:pos>15.500 -90.867</gml:pos>"))  # ~115 km
        r = v.parse_document(x, "u", NOW, CAT)[0][0]
        self.assertEqual(r["volcano"]["gvpStatus"], "identidade_divergente")
        self.assertGreater(r["volcano"]["identity"]["distanceKm"], 100)

    def test_b3_oficiais_com_identidade_conferida(self):
        for n, gvp in ((FILES[0], "342090"), (FILES[1], "342030"), (FILES[3], "352010")):
            r = v.parse_document(raw(n), "u", NOW, CAT)[0][0]
            self.assertEqual(r["volcano"]["gvpStatus"], "identidade_conferida", n)
            self.assertEqual(r["volcanoKey"], gvp)
            self.assertLess(r["volcano"]["identity"]["distanceKm"], 5)

    def test_b4_variacao_de_nome_aceita(self):
        x = derive(FILES[1], ("SANTA MARIA 342030", "SANTA MARÍA 342030"))
        r = v.parse_document(x, "u", NOW, CAT)[0][0]
        self.assertEqual(r["volcano"]["gvpStatus"], "identidade_conferida")

    def test_c_nilreason_unknown_com_hora_nao_encerra(self):
        x = derive(FILES[0], ("<nextAdvisoryTime>",
                              '<nextAdvisoryTime nilReason="http://codes.wmo.int/common/nil/unknown">'))   # [DERIVADO] caso do GPT
        a = v.parse_document(x, "u", NOW)[0][0]["advisory"]
        self.assertFalse(a["noFurtherAdvisoryScheduled"])
        self.assertEqual(a["nextAdvisoryState"], "contraditorio")

    def test_c2_nilreason_unknown_sem_hora_indeterminado(self):
        x = derive(FILES[1], ("nil/inapplicable", "nil/unknown"))               # [DERIVADO]
        res, d = self.run_with(Fake([], {v.PAGE: index([FILES[1]]), BASE + FILES[1]: x}))
        a = d["active"]["342030"]["advisory"]
        self.assertEqual(a["nextAdvisoryState"], "indeterminado")
        self.assertFalse(a["noFurtherAdvisoryScheduled"])
        self.assertTrue(a["expiryFallback"])

    def test_c3_inapplicable_oficial_encerra(self):
        for n in (FILES[1], FILES[2]):
            a = v.parse_document(raw(n), "u", NOW)[0][0]["advisory"]
            self.assertEqual(a["nextAdvisoryState"], "sem_proximo_aviso")
            self.assertTrue(a["noFurtherAdvisoryScheduled"])

    def test_d_observacao_23h_antes_nao_exibivel(self):
        x = derive(FILES[0], ("2026-11-01T15:20:00Z", "2026-10-31T16:05:00Z"))   # [DERIVADO] caso do GPT
        r = v.parse_document(x, "u", NOW)[0][0]
        self.assertFalse(r["cloud"]["displayable"])
        self.assertIn("observacao_antiga_para_exibicao", r["cloud"]["warnings"])
        self.assertEqual(r["cloud"]["observationAgeAtIssueMin"], 1434)

    def test_d2_displayable_until_e_envelhecimento(self):
        r = v.parse_document(raw(FILES[0]), "u", NOW)[0][0]
        self.assertEqual(r["cloud"]["displayableUntilUtc"], "2026-11-01T21:20:00Z")   # obs 15:20 + 6 h
        self.run_with(Fake([FILES[0]]))
        res, d = self.run_with(Fake([FILES[0]]), now=dt.datetime(2026, 11, 1, 21, 30, tzinfo=UTC))
        c = d["active"]["342090"]["cloud"]
        self.assertFalse(c["displayable"])                    # aviso ainda ativo, poligono nao
        self.assertIn("observacao_envelhecida", c["warnings"])

    def test_e_conflito_resolvido_por_expiracao(self):
        a = raw(FILES[3])
        b = derive(FILES[3], ("_C_KNES_20261101081638", "_C_KNES_20261101081900"),
                   ('<nextAdvisoryTime><gml:TimeInstant gml:id="uuid.96785b7c-5e6a-45bf-bee1-53dfc08dbceb">'
                    '<gml:timePosition indeterminatePosition="before">2026-11-01T14:15:00Z</gml:timePosition>'
                    '</gml:TimeInstant></nextAdvisoryTime>',
                    '<nextAdvisoryTime nilReason="http://codes.wmo.int/common/nil/inapplicable" />'))   # [DERIVADO]
        n1, n2 = "FVXX21_20261101_0814.xml", "FVXX21_20261101_0815.xml"
        self.run_with(Fake([], {v.PAGE: index([n1]), BASE + n1: a}))
        res, d = self.run_with(Fake([], {v.PAGE: index([n1, n2]), BASE + n1: a, BASE + n2: b}),
                               now=NOW + dt.timedelta(minutes=5))
        self.assertIn("352010", d["conflicts"])
        res, d = self.run_with(Fake([], {v.PAGE: index([n1, n2]), BASE + n1: a, BASE + n2: b}),
                               now=dt.datetime(2026, 11, 1, 18, 0, tzinfo=UTC))
        self.assertNotIn("352010", d["active"])
        self.assertNotIn("352010", d["conflicts"])
        self.assertTrue(any(r["reason"] == "conflito_resolvido_por_expiracao" for r in d["closed"]))

    def test_oficiais_com_catalogo_resultado_completo(self):
        res, d = v.run(self.out, CAT, fetcher=Fake(FILES), now=NOW), json.load(open(self.out))
        self.assertEqual(sorted(d["active"]), ["342090", "352010"])
        self.assertTrue(d["active"]["342090"]["cloud"]["displayable"])
        self.assertFalse(d["active"]["352010"]["cloud"]["displayable"])   # observacao 07:40: mais de 6 h
        self.assertIn("observacao_envelhecida", d["active"]["352010"]["cloud"]["warnings"])


# ------------------------------------------------------ ajustes finais (R5)
class T13_AjustesFinais(Base):
    def test_qualificador_before_gravado(self):
        for n in (FILES[0], FILES[3], FILES[4]):
            a = v.parse_document(raw(n), "u", NOW)[0][0]["advisory"]
            self.assertEqual(a["nextAdvisoryQualifier"], "before", n)
        a = v.parse_document(raw(FILES[0]), "u", NOW)[0][0]["advisory"]
        self.assertEqual(a["expiresAtUtc"], "2026-11-02T01:00:00Z")   # logica de validade inalterada

    def test_qualificador_ausente_quando_sem_horario(self):
        a = v.parse_document(raw(FILES[1]), "u", NOW)[0][0]["advisory"]
        self.assertIsNone(a["nextAdvisoryQualifier"])

    def test_notas_obrigatorias_no_json(self):
        res, d = self.run_with(Fake(FILES))
        notes = " ".join(d["rules"]["notes"])
        self.assertIn("NAO significa que as cinzas desapareceram", notes)
        self.assertIn("proprio relogio UTC", notes)
        self.assertTrue(d["rules"]["consumerMustCheckExpiry"])
        self.assertTrue(d["rules"]["consumerMustCheckDisplayableUntil"])

    def test_json_inicial_da_branch_vaac_data_e_aceito(self):
        inicial = {"schema": "vaac-exp-1", "experimental": True,
                   "source": {"name": "NOAA Washington VAAC", "url": v.PAGE},
                   "collector": {"lastAttemptUtc": None, "lastSuccessUtc": None, "result": "NAO_INICIADO"},
                   "rules": {"toleranceHours": 3, "acceptedIWXXM": ["3.0"], "seenRetentionDays": 15},
                   "active": {}, "closed": [], "rejected": [], "seen": {}, "lastState": {}}
        with open(self.out, "w") as f:                       # copia fiel do arquivo hoje na vaac-data
            json.dump(inicial, f)
        res, d = self.run_with(Fake(FILES))
        self.assertEqual(res, "OK")
        self.assertEqual(d["revision"], v.REVISION)
        self.assertEqual(sorted(d["active"]), ["342090", "352010"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
