# -*- coding: utf-8 -*-
"""
Confere NFS-e - servidor local (Totali Contabilidade)

Serve a interface (index.html) e faz o OCR dos PDFs digitalizados.
Motores de OCR:
  - preciso : RapidOCR (onnxruntime). Lento (~10-20 s por pagina), mas le numeros com precisao.
  - rapido  : OCR nativo do Windows 11 (winocr). Instantaneo, porem troca digitos em scans ruins.
  - duplo   : roda o preciso e completa os buracos com o rapido; le tambem o QR code da nota. Padrao.
O resultado de cada PDF fica em cache (pasta cache/), entao o mesmo arquivo nunca e lido duas vezes.

Uso:  python servidor.py [porta]              (padrao 8131)
      python servidor.py --teste arquivo.pdf 1-3 [--motor rapido]
"""
import http.server, json, hashlib, io, os, sys, threading, time, re, urllib.parse, webbrowser, zipfile

# O onnxruntime precisa ser carregado ANTES do winocr (winrt), senao a DLL dele falha ao iniciar.
try:
    import onnxruntime  # noqa: F401
except Exception:
    pass
from datetime import datetime

# O atalho sem janela preta roda em pythonw.exe, que nao tem console: sys.stdout e
# sys.stderr chegam como None. Sem isto, o log de cada pedido estoura em AttributeError
# e o servidor aceita a conexao e fecha sem responder.
for _fluxo in ("stdout", "stderr"):
    if getattr(sys, _fluxo, None) is None:
        setattr(sys, _fluxo, open(os.devnull, "w", encoding="utf-8"))

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
MODELOS = os.path.join(BASE, "modelos.json")
SESSAO = os.path.join(BASE, "sessao.json")
PORTA_PADRAO = 8131
MAX_UPLOAD = 300 * 1024 * 1024          # 300 MB por PDF enviado
MAX_MODELOS = 20 * 1024 * 1024          # 20 MB de JSON (modelos / linhas do Excel)
MAX_SESSAO = 120 * 1024 * 1024          # 120 MB do trabalho em andamento (XMLs + decisões)
# O servidor nunca entrega estes arquivos como estatico: documentos fiscais, modelos,
# certificado e o proprio codigo.
PROIBIDO = ("cache/", "cache\\", "modelos.json", "portal.json", "sessao.json", ".py", ".bak", ".pyc",
            ".pfx", ".p12", ".key", ".env", ".git", "__pycache__")
os.makedirs(CACHE, exist_ok=True)

# ------------------------------------------------------------------ OCR
_lock = threading.Lock()
_winocr = None


def _pdfium():
    import pypdfium2 as pdfium
    return pdfium


_rapids = {}


def motor_preciso(lado=1500):
    if lado not in _rapids:
        from rapidocr_onnxruntime import RapidOCR
        _rapids[lado] = RapidOCR(det_limit_side_len=lado, det_limit_type="max")
    return _rapids[lado]


def motor_rapido():
    global _winocr
    if _winocr is None:
        import winocr
        _winocr = winocr
    return _winocr


def motores_disponiveis():
    m = []
    try:
        motor_rapido(); m.append("rapido")
    except Exception:
        pass
    try:
        import rapidocr_onnxruntime  # noqa
        m.append("preciso")
        if "rapido" in m:
            m.insert(0, "duplo")
    except Exception:
        pass
    return m


def _pontuar_texto(txt):
    toks = re.findall(r"[A-Za-zÀ-ú]{3,}|\d{2,}", txt)
    return len(toks)


def detectar_rotacao(img):
    """Devolve o angulo (0/90/180/270) que deixa a pagina em pe, usando o OCR rapido do Windows."""
    try:
        w = motor_rapido()
    except Exception:
        return 0
    base = img.convert("RGB")
    base.thumbnail((1400, 1400))
    melhor, ang = -1, 0
    for a in (0, 90, 270, 180):
        im = base.rotate(a, expand=True) if a else base
        try:
            r = w.recognize_pil_sync(im, "pt")
            s = _pontuar_texto("\n".join(l["text"] for l in r["lines"]))
        except Exception:
            s = 0
        if s > melhor:
            melhor, ang = s, a
    return ang


def _variantes(img):
    """Versoes da imagem para o OCR: original tratada e uma binarizada (scan fraco)."""
    from PIL import ImageOps, ImageFilter
    g = img.convert("L")
    tratada = ImageOps.autocontrast(g, cutoff=2).filter(ImageFilter.UnsharpMask(radius=2, percent=120))
    return tratada.convert("RGB")


def _preparar(img):
    return _variantes(img)


def _agrupar_linhas(itens, altura_media):
    """itens: [{x, y, w, h, str}] em px -> linhas ordenadas de cima para baixo."""
    itens = sorted(itens, key=lambda i: (i["y"], i["x"]))
    tol = max(6, altura_media * 0.55)
    linhas = []
    for it in itens:
        if linhas and abs(linhas[-1]["y"] - it["y"]) <= tol:
            l = linhas[-1]; l["itens"].append(it)
            l["y"] = (l["y"] * (len(l["itens"]) - 1) + it["y"]) / len(l["itens"])
        else:
            linhas.append({"y": it["y"], "itens": [it]})
    for l in linhas:
        l["itens"].sort(key=lambda i: i["x"])
    return linhas


def _itens_rapido(img):
    r = motor_rapido().recognize_pil_sync(img.convert("RGB"), "pt")
    out = []
    for l in r["lines"]:
        for wd in l["words"]:
            b = wd["bounding_rect"]
            out.append({"x": b["x"], "y": b["y"] + b["height"] / 2, "w": b["width"], "h": b["height"], "str": wd["text"], "m": "r"})
    return out


def _itens_preciso(img, lado=1500):
    import numpy as np
    eng = motor_preciso(lado)
    res, _ = eng(np.array(_variantes(img)))
    out = []
    for box, txt, conf in (res or []):
        xs = [q[0] for q in box]; ys = [q[1] for q in box]
        h = (abs(box[3][1] - box[0][1]) + abs(box[2][1] - box[1][1])) / 2
        out.append({"x": min(xs), "y": (min(ys) + max(ys)) / 2, "w": max(xs) - min(xs), "h": h,
                    "str": txt, "conf": round(float(conf), 2), "m": "p"})
    return out


def _funde(base, extra):
    """Acrescenta a `base` os itens de `extra` que nao coincidem com nada ja lido (preenche buracos)."""
    saida = list(base)
    for e in extra:
        centro_x, centro_y = e["x"] + e["w"] / 2, e["y"]
        colide = False
        for b in base:
            if abs(b["y"] - centro_y) <= max(6, b["h"] * 0.7) and b["x"] - 4 <= centro_x <= b["x"] + b["w"] + 4:
                colide = True; break
        if not colide and e["str"].strip():
            saida.append(e)
    return saida


def _pontos_uteis(itens):
    """Quanto de informacao aproveitavel a pagina tem (numeros e palavras)."""
    txt = " ".join(i["str"] for i in itens)
    return len(re.findall(r"[A-Za-zÀ-ú]{3,}|\d{2,}", txt))


def ler_qrcodes(img):
    """QR code da nota: quando o scan preserva o codigo, a chave vem sem erro de leitura."""
    try:
        import cv2, numpy as np
    except Exception:
        return []
    arr = np.array(img.convert("L"))
    det = cv2.QRCodeDetector()
    saida = []
    tentativas = [arr, cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]]
    for v in tentativas:
        try:
            ok, textos, *_ = det.detectAndDecodeMulti(v)
            if ok:
                for t in textos:
                    if t and t not in saida:
                        saida.append(t)
        except Exception:
            pass
        if saida:
            break
    return saida


def ocr_pagina(img, motor):
    """Devolve linhas com coordenadas normalizadas (0..1000) para a largura/altura da imagem.
    motor 'duplo' (padrao) roda o motor preciso e completa os buracos com o rapido do Windows."""
    W, H = img.size
    if motor == "rapido":
        itens = _itens_rapido(img)
    elif motor == "preciso":
        itens = _itens_preciso(img)
    else:  # duplo
        itens = _itens_preciso(img)
        if _pontos_uteis(itens) < 25:          # scan fraco: tenta de novo com mais detalhe
            reforco = _itens_preciso(img, lado=2200)
            if _pontos_uteis(reforco) > _pontos_uteis(itens):
                itens = reforco
        try:
            itens = _funde(itens, _itens_rapido(img))
        except Exception:
            pass
    alturas = [i["h"] for i in itens] or [20]
    alt = sorted(alturas)[len(alturas) // 2]
    linhas = _agrupar_linhas(itens, alt)
    saida = []
    for l in linhas:
        saida.append({
            "y": round(l["y"] * 1000 / H, 1),
            "itens": [{"x": round(i["x"] * 1000 / W, 1), "w": round(i["w"] * 1000 / W, 1), "str": i["str"],
                       **({"conf": i["conf"]} if "conf" in i else {}), **({"m": i["m"]} if i.get("m") == "r" else {})} for i in l["itens"]],
        })
    return saida


def _abrir_plumber(caminho_pdf):
    try:
        import pdfplumber
        return pdfplumber.open(caminho_pdf)
    except Exception:
        return None


def _linhas_do_texto_embutido(doc, indice):
    """Palavras que já estão no PDF (quando ele não é digitalizado), com posição.
    Devolve None quando a pagina nao tem texto aproveitavel."""
    if doc is None:
        return None
    try:
        pg = doc.pages[indice]
        palavras = pg.extract_words(use_text_flow=False, keep_blank_chars=False) or []
        if sum(len(w["text"].strip()) for w in palavras) < 40:
            pg.close()
            return None
        W, H = float(pg.width), float(pg.height)
        itens = [{"x": float(w["x0"]), "y": (float(w["top"]) + float(w["bottom"])) / 2,
                  "w": float(w["x1"]) - float(w["x0"]),
                  "h": float(w["bottom"]) - float(w["top"]), "str": w["text"]} for w in palavras]
        pg.close()          # libera a pagina: sem isto o pdfplumber guarda tudo e estoura a memoria
    except Exception:
        return None
    alturas = [i["h"] for i in itens] or [10]
    linhas = _agrupar_linhas(itens, sorted(alturas)[len(alturas) // 2])
    return [{"y": round(l["y"] * 1000 / H, 1),
             "itens": [{"x": round(i["x"] * 1000 / W, 1), "w": round(i["w"] * 1000 / W, 1), "str": i["str"]} for i in l["itens"]]}
            for l in linhas], W, H


def ocr_pdf(caminho_pdf, motor="duplo", paginas=None, progresso=None, escala=3.0, qr=True):
    pdfium = _pdfium()
    pdf = pdfium.PdfDocument(caminho_pdf)
    total = len(pdf)
    idx = paginas if paginas is not None else range(total)
    plumber = _abrir_plumber(caminho_pdf)
    saida = []
    for n, i in enumerate(idx):
        # 1) o PDF já tem texto nesta página? entao nao ha o que reconhecer
        embutido = _linhas_do_texto_embutido(plumber, i)
        if embutido:
            linhas_t, W, H = embutido
            saida.append({"numero": i + 1, "largura": int(W), "altura": int(H), "rotacao": 0,
                          "linhas": linhas_t, "fonte": "texto"})
            if progresso:
                progresso(n + 1, len(idx))
            continue
        pg = pdf[i]
        img = pg.render(scale=escala).to_pil()
        rot = detectar_rotacao(img)
        if rot:
            img = img.rotate(rot, expand=True)
        linhas = ocr_pagina(img, motor)
        pagina = {"numero": i + 1, "largura": img.size[0], "altura": img.size[1], "rotacao": rot,
                  "linhas": linhas, "fonte": "ocr"}
        if qr and motor != "rapido":
            codigos = ler_qrcodes(img)
            if not codigos:  # o QR de scan comprimido costuma so aparecer com mais resolucao
                try:
                    codigos = ler_qrcodes(pg.render(scale=4.5).to_pil().rotate(rot, expand=True) if rot else pg.render(scale=4.5).to_pil())
                except Exception:
                    codigos = []
            if codigos:
                pagina["qr"] = codigos
        saida.append(pagina)
        if progresso:
            progresso(n + 1, len(idx))
    if plumber is not None:
        try:
            plumber.close()
        except Exception:
            pass
    return {"paginas": saida, "total": total, "motor": motor, "gerado": datetime.now().isoformat(timespec="seconds")}


def imagem_pagina(caminho_pdf, p, rotacao=0, escala=1.6):
    pdfium = _pdfium()
    pdf = pdfium.PdfDocument(caminho_pdf)
    img = pdf[p - 1].render(scale=escala).to_pil()
    if rotacao:
        img = img.rotate(rotacao, expand=True)
    buf = io.BytesIO(); img.convert("RGB").save(buf, "JPEG", quality=80)
    return buf.getvalue()


# ------------------------------------------------------------------ trabalhos
JOBS = {}


def _caminhos(h, motor):
    return os.path.join(CACHE, h + ".pdf"), os.path.join(CACHE, f"{h}.{motor}.json")


def iniciar_job(dados, nome, motor="duplo"):
    h = hashlib.sha1(dados).hexdigest()
    pdf_path, json_path = _caminhos(h, motor)
    if not os.path.exists(pdf_path):
        with open(pdf_path, "wb") as f:
            f.write(dados)
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as f:
            return {"pronto": True, "hash": h, "resultado": json.load(f)}
    job = {"hash": h, "nome": nome, "motor": motor, "feito": 0, "total": 0, "pronto": False, "erro": None}
    JOBS[h + motor] = job

    def rodar():
        try:
            def prog(f, t):
                job["feito"], job["total"] = f, t
            with _lock:  # um OCR por vez: o onnxruntime ja usa todos os nucleos
                res = ocr_pdf(pdf_path, motor, progresso=prog)
            res["nome"] = nome
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
            job["resultado"] = res
        except Exception as e:  # noqa
            job["erro"] = str(e)
        job["pronto"] = True

    threading.Thread(target=rodar, daemon=True).start()
    return {"pronto": False, "hash": h, "job": h + motor}



# ------------------------------------------------------------------ Excel do Domínio (modelo .xlsm com a macro preservada)
MODELO_DOMINIO = os.path.join(BASE, "modelos-dominio", "Modelo Notas de Servicos Tomados.xlsm")


def _col_letra(n):
    t = ""
    while n:
        n, r = divmod(n - 1, 26); t = chr(65 + r) + t
    return t


def _xml_esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def gerar_excel_dominio(linhas, extras):
    """Preenche o modelo do Domínio a partir da linha 7 editando o XML da planilha dentro do .xlsm.
    Mantém macro, botão, estilos e tudo o mais do arquivo original. Datas vêm como 'AAAA-MM-DD'."""
    from datetime import date
    zin = zipfile.ZipFile(MODELO_DOMINIO)
    nome_sheet = "xl/worksheets/sheet1.xml"
    xml = zin.read(nome_sheet).decode("utf-8")
    estilo = lambda ref, padrao="0": (re.search(r'<c r="%s" s="(\d+)"' % ref, xml) or [None, padrao])[1]
    s_txt, s_num, s_data, s_val, s_cab = estilo("A7"), estilo("F7"), estilo("H7"), estilo("M7"), estilo("F6", estilo("A6"))
    n_cols = 28 + len(extras)

    def celula(col, row, v, i):
        ref = f"{_col_letra(col)}{row}"
        if v is None or v == "":
            return ""
        if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            y, m, d = map(int, v.split("-"))
            serial = (date(y, m, d) - date(1899, 12, 30)).days
            return f'<c r="{ref}" s="{s_data}"><v>{serial}</v></c>'
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, (int, float)):
            st = s_val if 12 <= i <= 27 else s_num
            return f'<c r="{ref}" s="{st}"><v>{v}</v></c>'
        return f'<c r="{ref}" s="{s_txt}" t="inlineStr"><is><t xml:space="preserve">{_xml_esc(v)}</t></is></c>'

    # cabeçalhos das colunas de apoio (AC6...) e o título AC5
    def acrescenta_na_linha(xml, r, celulas):
        m = re.search(r'(<row r="%d"[^>]*>)(.*?)(</row>)' % r, xml, re.S)
        if not m:
            return xml.replace("</sheetData>", f'<row r="{r}">{celulas}</row></sheetData>')
        return xml[:m.end(2)] + celulas + xml[m.end(2):]
    xml = acrescenta_na_linha(xml, 5, f'<c r="AC5" s="{s_cab}" t="inlineStr"><is><t>APOIO (fora do leiaute: a macro nao exporta estas colunas)</t></is></c>')
    xml = acrescenta_na_linha(xml, 6, "".join(f'<c r="{_col_letra(29 + i)}6" s="{s_cab}" t="inlineStr"><is><t>{_xml_esc(h)}</t></is></c>' for i, h in enumerate(extras)))
    # remove as linhas de dados existentes (7 em diante) e insere as novas
    xml = re.sub(r'<row r="(?:[7-9]|[1-9]\d+)"[^>]*>.*?</row>', "", xml, flags=re.S)
    xml = re.sub(r'<row r="(?:[7-9]|[1-9]\d+)"[^>]*/>', "", xml)
    novas = []
    for k, lin in enumerate(linhas):
        r = 7 + k
        cels = "".join(celula(j + 1, r, v, j) for j, v in enumerate(lin[:n_cols]))
        novas.append(f'<row r="{r}" spans="1:{n_cols}">{cels}</row>')
    xml = xml.replace("</sheetData>", "".join(novas) + "</sheetData>")
    xml = re.sub(r'<dimension ref="[^"]*"/>', f'<dimension ref="A1:{_col_letra(n_cols)}{6 + max(1, len(linhas))}"/>', xml)
    # largura das colunas de apoio
    if "<cols>" in xml and f'min="29"' not in xml:
        xml = xml.replace("</cols>", f'<col min="29" max="{n_cols}" width="20" customWidth="1"/></cols>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            dados = xml.encode("utf-8") if item.filename == nome_sheet else zin.read(item.filename)
            zout.writestr(item, dados)
    return out.getvalue()


# ------------------------------------------------------------------ HTTP
class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "ConfereNFSe"
    sys_version = ""

    def __init__(self, *a, **k):
        super().__init__(*a, directory=BASE, **k)

    def origem_valida(self):
        """So aceita pedidos da propria pagina. Sem isto, um site aberto noutra aba do
        navegador poderia disparar OCR ou reescrever os modelos (CSRF)."""
        origem = self.headers.get("Origin") or self.headers.get("Referer")
        if not origem:
            return True                      # navegacao direta ou ferramenta local
        try:
            u = urllib.parse.urlparse(origem)
        except Exception:
            return False
        return u.hostname in ("localhost", "127.0.0.1", "::1")

    def caminho_permitido(self, caminho):
        c = urllib.parse.unquote(caminho).lower().replace("\\", "/").lstrip("/")
        return not any(x in c for x in PROIBIDO)

    def ler_corpo(self, maximo):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n <= 0 or n > maximo:
            return None
        return self.rfile.read(n)

    def log_message(self, fmt, *args):
        if any(x in (args[0] if args else "") for x in ("/api/ocr/status", "/api/portal/status")):
            return
        super().log_message(fmt, *args)

    def _json(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def end_headers(self):
        if not self.path.startswith("/api/pagina"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/ping":
            return self._json({"ok": True, "motores": motores_disponiveis(), "versao": 5, "sessao": os.path.exists(SESSAO), "modeloDominio": os.path.exists(MODELO_DOMINIO), "portal": os.path.exists(os.path.join(BASE, "portal_nacional.py"))})
        if u.path == "/api/portal/status":
            return self._json(PORTAL_PROG)
        if u.path == "/api/ocr/status":
            job = JOBS.get(re.sub(r"[^0-9a-z]", "", q.get("job", [""])[0])[:64])
            if not job:
                return self._json({"erro": "trabalho desconhecido"}, 404)
            out = {k: v for k, v in job.items() if k != "resultado"}
            if job["pronto"] and job.get("resultado") is not None:
                out["resultado"] = job["resultado"]
            return self._json(out)
        if u.path == "/api/pagina":
            if not self.origem_valida():
                return self._json({"erro": "origem nao autorizada"}, 403)
            h = re.sub(r"[^0-9a-f]", "", q.get("hash", [""])[0])
            if len(h) != 40:
                return self._json({"erro": "identificador invalido"}, 400)
            try:
                p = max(1, int(q.get("p", ["1"])[0]))
                rot = int(q.get("rot", ["0"])[0]) % 360
                esc = min(6.0, max(0.5, float(q.get("escala", ["1.6"])[0])))
            except ValueError:
                return self._json({"erro": "parametro invalido"}, 400)
            pdf_path = os.path.join(CACHE, h + ".pdf")
            if not os.path.exists(pdf_path):
                return self._json({"erro": "pdf nao encontrado"}, 404)
            try:
                b = imagem_pagina(pdf_path, p, rot, esc)
            except Exception:
                return self._json({"erro": "pagina inexistente"}, 404)
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "max-age=86400"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if u.path == "/api/portal":
            if not self.origem_valida():
                return self._json({"erro": "origem nao autorizada"}, 403)
            try:
                import portal_nacional
                return self._json(portal_nacional.situacao())
            except Exception as e:  # noqa
                return self._json({"disponivel": False, "erro": str(e)})
        if u.path == "/api/sessao":
            if not self.origem_valida():
                return self._json({"erro": "origem nao autorizada"}, 403)
            if os.path.exists(SESSAO):
                try:
                    with open(SESSAO, encoding="utf-8") as f:
                        return self._json(json.load(f))
                except Exception:
                    pass
            return self._json({"vazia": True})
        if u.path == "/api/ocr/cache":
            # devolve o OCR ja feito de um PDF, sem precisar enviar o arquivo de novo
            if not self.origem_valida():
                return self._json({"erro": "origem nao autorizada"}, 403)
            h = re.sub(r"[^0-9a-f]", "", q.get("hash", [""])[0])
            motor = q.get("motor", ["duplo"])[0]
            if len(h) != 40 or motor not in ("preciso", "rapido", "duplo"):
                return self._json({"erro": "parametro invalido"}, 400)
            caminho = os.path.join(CACHE, f"{h}.{motor}.json")
            if not os.path.exists(caminho):
                # o PDF pode ter sido lido com outro motor: serve o que houver
                for alt in ("duplo", "preciso", "rapido"):
                    tentativa = os.path.join(CACHE, f"{h}.{alt}.json")
                    if os.path.exists(tentativa):
                        caminho = tentativa
                        break
                else:
                    return self._json({"erro": "sem leitura guardada"}, 404)
            with open(caminho, encoding="utf-8") as f:
                return self._json({"pronto": True, "hash": h, "resultado": json.load(f)})
        if u.path == "/api/modelos":
            if not self.origem_valida():
                return self._json({"erro": "origem nao autorizada"}, 403)
            if os.path.exists(MODELOS):
                with open(MODELOS, encoding="utf-8") as f:
                    return self._json(json.load(f))
            return self._json({"modelos": []})
        if not self.caminho_permitido(u.path):
            return self._json({"erro": "acesso negado"}, 403)
        return super().do_GET()

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except Exception as e:  # noqa
            print("erro no pedido:", e)
            self.close_connection = True

    def do_POST(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if not self.origem_valida():
            return self._json({"erro": "origem nao autorizada"}, 403)
        dados = self.ler_corpo(MAX_MODELOS if u.path == "/api/dominio" else MAX_UPLOAD)
        if dados is None:
            return self._json({"erro": "corpo ausente ou grande demais"}, 413)
        if u.path == "/api/dominio":
            try:
                obj = json.loads(dados.decode("utf-8"))
                b = gerar_excel_dominio(obj.get("linhas", []), obj.get("extras", []))
            except Exception as e:  # noqa
                return self._json({"erro": str(e)}, 500)
            self.send_response(200); self.send_header("Content-Type", "application/vnd.ms-excel.sheet.macroEnabled.12")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        if u.path.startswith("/api/portal/"):
            try:
                import portal_nacional
                acao = u.path.rsplit("/", 1)[-1]
                corpo = json.loads(dados.decode("utf-8")) if dados else {}
                if acao == "config":
                    cfg = portal_nacional.ler_config()
                    for k in ("certificado", "ambiente", "cnpj", "origem", "thumbprint"):
                        if k in corpo:
                            cfg[k] = corpo[k]
                    if corpo.get("senha"):
                        cfg["senha"] = corpo["senha"]
                    if corpo.get("ultimoNSU") is not None:
                        cfg["ultimoNSU"] = int(corpo["ultimoNSU"])
                    portal_nacional.gravar_config(cfg)
                    return self._json(portal_nacional.situacao())
                if acao == "testar":
                    return self._json(portal_nacional.testar())
                if acao == "consultar":
                    inicio_nsu = int(corpo.get("nsu") or 0)
                    cfg_p = portal_nacional.ler_config()
                    PORTAL_PROG.update({
                        "ativo": True, "lidos": 0, "nsu": inicio_nsu, "nsuInicial": inicio_nsu,
                        "alvo": int(cfg_p.get("fimEsteira") or 0), "inicio": time.time(), "segundos": 0,
                    })

                    def prog(lidos, nsu):
                        PORTAL_PROG["lidos"] = lidos
                        PORTAL_PROG["nsu"] = nsu
                        PORTAL_PROG["segundos"] = round(time.time() - PORTAL_PROG["inicio"], 1)

                    try:
                        res = portal_nacional.consultar_dfe(
                            corpo.get("nsu"), competencia=corpo.get("competencias") or corpo.get("competencia"),
                            papel=corpo.get("papel") or None, pistas=corpo.get("pistas") or None,
                            progresso=prog)
                    finally:
                        PORTAL_PROG["ativo"] = False
                    return self._json(res)
                return self._json({"erro": "acao desconhecida"}, 404)
            except Exception as e:  # noqa
                return self._json({"erro": str(e)}, 500)
        if u.path == "/api/ocr":
            if dados[:5] != b"%PDF-":
                return self._json({"erro": "o conteudo enviado nao e um PDF"}, 400)
            nome = os.path.basename(urllib.parse.unquote(q.get("nome", ["arquivo.pdf"])[0]))[:180]
            motor = q.get("motor", ["duplo"])[0]
            if motor not in ("preciso", "rapido", "duplo"):
                motor = "duplo"
            return self._json(iniciar_job(dados, nome, motor))
        return self._json({"erro": "rota desconhecida"}, 404)

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        if not self.origem_valida():
            return self._json({"erro": "origem nao autorizada"}, 403)
        if u.path == "/api/sessao":
            if os.path.exists(SESSAO):
                try:
                    os.remove(SESSAO)
                except Exception:
                    pass
            return self._json({"ok": True})
        return self._json({"erro": "rota desconhecida"}, 404)

    def do_PUT(self):
        u = urllib.parse.urlparse(self.path)
        if not self.origem_valida():
            return self._json({"erro": "origem nao autorizada"}, 403)
        dados = self.ler_corpo(MAX_SESSAO if u.path == "/api/sessao" else MAX_MODELOS)
        if dados is None:
            return self._json({"erro": "corpo ausente ou grande demais"}, 413)
        if u.path == "/api/sessao":
            try:
                obj = json.loads(dados.decode("utf-8"))
            except Exception:
                return self._json({"erro": "json invalido"}, 400)
            if not isinstance(obj, dict):
                return self._json({"erro": "formato inesperado"}, 400)
            tmp = SESSAO + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, SESSAO)
            return self._json({"ok": True, "bytes": len(dados)})
        if u.path == "/api/modelos":
            try:
                obj = json.loads(dados.decode("utf-8"))
            except Exception:
                return self._json({"erro": "json invalido"}, 400)
            if not isinstance(obj, dict) or not isinstance(obj.get("modelos"), list):
                return self._json({"erro": "formato inesperado"}, 400)
            if os.path.exists(MODELOS):
                os.replace(MODELOS, MODELOS + ".bak")
            with open(MODELOS, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)
            return self._json({"ok": True, "quantidade": len(obj.get("modelos", []))})
        return self._json({"erro": "rota desconhecida"}, 404)


# como vai a varredura do portal: a tela pergunta de segundo em segundo
PORTAL_PROG = {"ativo": False, "lidos": 0, "nsu": 0, "nsuInicial": 0, "alvo": 0,
               "segundos": 0, "inicio": 0, "maxSegundos": 240}


def main():
    args = sys.argv[1:]
    if args and args[0] == "--teste":
        pdf = args[1]; faixa = args[2] if len(args) > 2 else "1"
        motor = args[args.index("--motor") + 1] if "--motor" in args else "duplo"
        a, _, b = faixa.partition("-"); pags = list(range(int(a) - 1, int(b or a)))
        t = time.time()
        res = ocr_pdf(pdf, motor, pags, progresso=lambda f, t_: print(f"  pagina {f}/{t_}", file=sys.stderr))
        for p in res["paginas"]:
            print(f"===== pagina {p['numero']} rotacao={p['rotacao']} {p['largura']}x{p['altura']}")
            for l in p["linhas"]:
                print(f"{l['y']:6.1f} | " + "   ".join(f"[{i['x']:.0f}] {i['str']}" for i in l["itens"]))
        print(f"tempo total {time.time() - t:.1f}s", file=sys.stderr)
        return
    porta = PORTA_PADRAO
    for a in args:
        if a.isdigit():
            porta = int(a); break
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", porta), Handler)
    except OSError as e:
        print(f"Nao consegui abrir a porta {porta}: {e}")
        print("Ja existe um Confere NFS-e aberto? Feche a outra janela preta e tente de novo.")
        try:
            input("Enter para fechar...")
        except (EOFError, RuntimeError):   # sem janela preta nao ha quem responda
            pass
        return
    print(f"Confere NFS-e rodando em http://localhost:{porta}  (motores OCR: {', '.join(motores_disponiveis()) or 'nenhum'})")
    if "--sem-navegador" not in args:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{porta}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
