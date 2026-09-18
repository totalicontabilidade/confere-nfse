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
SESSAO = os.path.join(BASE, "sessao.json")            # a antiga, de antes de haver uma por computador
SESSOES = os.path.join(BASE, "sessoes")                 # uma por navegador: dois computadores nao se atropelam
ACESSO = os.path.join(BASE, "acesso.json")              # acesso pela rede: ligado ou nao, e o codigo
PLANOS = os.path.join(BASE, "planos")                   # plano de contas e memoria de classificacao, por empresa
PORTA_PADRAO = 8131
MAX_UPLOAD = 300 * 1024 * 1024          # 300 MB por PDF enviado
MAX_MODELOS = 20 * 1024 * 1024          # 20 MB de JSON (modelos / linhas do Excel)
MAX_SESSAO = 120 * 1024 * 1024          # 120 MB do trabalho em andamento (XMLs + decisões)
# O servidor nunca entrega estes arquivos como estatico: documentos fiscais, modelos,
# certificado e o proprio codigo.
PROIBIDO = ("cache/", "cache\\", "modelos.json", "portal.json", "sessao.json", ".py", ".bak", ".pyc",
            ".pfx", ".p12", ".key", ".env", ".git", "__pycache__", "sessoes/", "sessoes\\",
            "acesso.json", "servidor.log", ".bat", ".vbs", "planos/", "planos\\")
os.makedirs(CACHE, exist_ok=True)
os.makedirs(SESSOES, exist_ok=True)
os.makedirs(PLANOS, exist_ok=True)


# ------------------------------------------------------------------ classificacao contabil
def _cl():
    import classificacao
    return classificacao


def _arq_plano(cnpj):
    return os.path.join(PLANOS, cnpj + ".json")


def _arq_memoria(cnpj):
    return os.path.join(PLANOS, cnpj + ".memoria.json")


def _ler_json(caminho, padrao):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return padrao


def _gravar_json(caminho, obj):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, caminho)


def situacao_plano(cnpj):
    plano = _ler_json(_arq_plano(cnpj), None) if cnpj else None
    mem = _ler_json(_arq_memoria(cnpj), {}) if cnpj else {}
    if not plano:
        return {"tem": False, "memoria": len(mem)}
    org = _cl().organizar(plano)
    return {"tem": True, "empresa": plano.get("empresa", ""), "cnpjPlano": plano.get("cnpj", ""),
            "arquivo": plano.get("arquivo", ""), "quando": plano.get("quando", ""),
            "contas": len(plano.get("contas", [])), "despesas": len(org["despesas"]),
            "fornecedores": len(org["fornecedores"]), "tributos": len(org["tributos"]), "memoria": len(mem)}


def _salvo_temporario(dados, nome):
    import tempfile
    ext = os.path.splitext(nome)[1].lower() or ".bin"
    fd, caminho = tempfile.mkstemp(suffix=ext)
    with os.fdopen(fd, "wb") as f:
        f.write(dados)
    return caminho


# ------------------------------------------------------------------ acesso pela rede
import hmac, secrets, ipaddress

_tentativas = {}          # ip -> [instantes das tentativas erradas]


def ler_acesso():
    try:
        with open(ACESSO, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if not cfg.get("segredo"):
        cfg["segredo"] = secrets.token_hex(32)
        cfg.setdefault("rede", False)
        gravar_acesso(cfg)
    return cfg


def gravar_acesso(cfg):
    tmp = ACESSO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.replace(tmp, ACESSO)


def hash_codigo(codigo, segredo):
    return hashlib.pbkdf2_hmac("sha256", codigo.encode("utf-8"), segredo.encode("utf-8"), 120000).hex()


def ficha(cfg):
    """O que vai no cookie. Troca sozinho quando o codigo muda: quem entrou antes sai."""
    return hmac.new(cfg["segredo"].encode(), (cfg.get("codigoHash") or "").encode(), "sha256").hexdigest()


def ips_da_maquina():
    ips = []
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def pagina_acesso(tipo):
    """O que o outro computador ve antes de entrar."""
    if tipo == "desligado":
        corpo = """<h1>Acesso pela rede desligado</h1>
        <p>O Confere NFS-e esta rodando, mas o acesso a partir de outros computadores esta desligado.</p>
        <p class="nota">No computador onde o sistema esta instalado, abra o sistema e ligue
        <b>Acesso pela rede</b>.</p>"""
    else:
        corpo = """<h1>Entrar no Confere NFS-e</h1>
        <p>Digite o codigo de acesso do escritorio.</p>
        <form id="f"><input id="c" type="password" autocomplete="current-password" autofocus placeholder="codigo de acesso">
        <button type="submit">Entrar</button></form>
        <p id="m" class="erro"></p>
        <script>
        document.getElementById('f').onsubmit = async e => {
          e.preventDefault();
          const m = document.getElementById('m'); m.textContent = '';
          const r = await fetch('/api/acesso/entrar', { method: 'POST', body: JSON.stringify({ codigo: document.getElementById('c').value }) });
          if (r.ok) { location.href = '/'; return; }
          const j = await r.json().catch(() => ({}));
          m.textContent = j.erro === 'codigo incorreto' ? 'Codigo incorreto.' : (j.erro || 'Nao consegui entrar.');
        };
        </script>"""
    return """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Confere NFS-e</title>
<link rel="icon" href="/assets/favicon-sistema.png">
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#F4F6F9;font-family:"Segoe UI",system-ui,sans-serif;color:#17202E}
.cx{background:#fff;max-width:380px;width:calc(100% - 40px);border-radius:12px;box-shadow:0 20px 50px rgba(11,31,58,.18);overflow:hidden}
.cab{background:linear-gradient(135deg,#0B1F3A,#1D4372);border-bottom:4px solid #C9A227;padding:18px 22px;display:flex;align-items:center;gap:12px}
.cab img{height:34px}
.corpo{padding:22px}
h1{font-size:18px;margin:0 0 8px;color:#0B1F3A}
p{margin:0 0 14px;font-size:14px;line-height:1.5}
.nota{color:#63708A;font-size:13px}
form{display:flex;gap:8px}
input{flex:1;padding:10px 12px;border:1px solid #DDE3EC;border-radius:8px;font-size:15px}
input:focus{outline:2px solid #C9A227;outline-offset:1px}
button{background:#C9A227;color:#0B1F3A;border:0;border-radius:8px;padding:10px 16px;font-weight:650;font-size:14px;cursor:pointer}
.erro{color:#B3261E;margin:10px 0 0;min-height:1.2em}
</style></head><body><div class="cx">
<div class="cab"><img src="/assets/logo-totali-claro.png" alt="Totali"></div>
<div class="corpo">""" + corpo + """</div></div></body></html>"""


def host_aceitavel(host):
    """Recusa nome de dominio no Host. Um site malicioso pode apontar o proprio dominio
    para 127.0.0.1 ou para este IP (DNS rebinding) e ler a conferencia como se fosse daqui."""
    h = (host or "").rsplit(":", 1)[0].strip("[]").lower()
    if h in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private

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


def _variantes_qr(cv2, arr):
    """O QR do scan chega borrado e de baixo contraste: vale tentar mais de um preparo."""
    yield arr
    yield cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    yield cv2.addWeighted(arr, 1.6, cv2.GaussianBlur(arr, (0, 0), 3), -0.6, 0)


def _detectores_qr(cv2):
    """O Aruco acha e le muito mais que o detector antigo; o antigo ainda pega casos
    que o novo perde, entao os dois rodam."""
    dets = []
    for nome in ("QRCodeDetectorAruco", "QRCodeDetector"):
        try:
            dets.append(getattr(cv2, nome)())
        except Exception:
            pass
    return dets


def ler_qrcodes(img):
    """QR code da nota: quando o scan preserva o codigo, a chave vem sem erro de leitura."""
    try:
        import cv2, numpy as np
    except Exception:
        return []
    arr = np.array(img.convert("L"))
    dets = _detectores_qr(cv2)
    saida = []

    def guarda(textos):
        for t in textos or []:
            if t and t not in saida:
                saida.append(t[:900])

    for det in dets:
        for v in _variantes_qr(cv2, arr):
            try:
                ok, textos, *_ = det.detectAndDecodeMulti(v)
                if ok:
                    guarda(textos)
            except Exception:
                pass
            if saida:
                return saida

    # Nada leu, mas o codigo pode estar ali: recorta cada QR encontrado e amplia.
    # Num scan de 300dpi o modulo do QR fica com menos de 2 pixels e so decodifica ampliado.
    for det in dets:
        for v in _variantes_qr(cv2, arr):
            try:
                ok, pts = det.detectMulti(v)
            except Exception:
                continue
            if not ok or pts is None:
                continue
            for quad in pts:
                xs, ys = quad[:, 0], quad[:, 1]
                m = 14
                x0, x1 = max(0, int(xs.min()) - m), min(arr.shape[1], int(xs.max()) + m)
                y0, y1 = max(0, int(ys.min()) - m), min(arr.shape[0], int(ys.max()) + m)
                if x1 - x0 < 12 or y1 - y0 < 12:
                    continue
                rec = arr[y0:y1, x0:x1]
                for fator in (3, 5):
                    amp = cv2.resize(rec, None, fx=fator, fy=fator, interpolation=cv2.INTER_CUBIC)
                    for d2 in dets:
                        for v2 in _variantes_qr(cv2, amp):
                            try:
                                ok2, t2, *_ = d2.detectAndDecodeMulti(v2)
                                if ok2:
                                    guarda(t2)
                            except Exception:
                                pass
                            if saida:
                                return saida
                        try:
                            t3 = d2.detectAndDecode(amp)
                            t3 = t3[0] if isinstance(t3, tuple) else t3
                            if t3:
                                guarda([t3])
                                return saida
                        except Exception:
                            pass
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
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return u.hostname in ("localhost", "127.0.0.1", "::1") or (bool(host) and u.hostname == host)

    def local(self):
        """Pedido feito neste proprio computador."""
        ip = self.client_address[0]
        return ip.startswith("127.") or ip == "::1"

    def _cookie(self, nome):
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == nome:
                return v
        return ""

    def _html(self, status, corpo):
        b = corpo.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def porteiro(self):
        """Decide se o pedido passa. Deste computador, sempre. De outro da rede, so com o
        acesso pela rede ligado e depois de digitar o codigo."""
        if not host_aceitavel(self.headers.get("Host")):
            self._json({"erro": "endereco nao aceito"}, 421)
            return False
        if self.local():
            return True
        caminho0 = urllib.parse.urlparse(self.path).path
        if self.command == "GET" and caminho0 in ("/assets/logo-totali-claro.png", "/assets/favicon-sistema.png"):
            return True           # so os dois logos que a pagina de entrada mostra
        cfg = ler_acesso()
        if not cfg.get("rede") or not cfg.get("codigoHash"):
            self._html(403, pagina_acesso("desligado"))
            return False
        caminho = urllib.parse.urlparse(self.path).path
        if caminho == "/api/acesso/entrar":
            return True
        if hmac.compare_digest(self._cookie("confere"), ficha(cfg)):
            return True
        if self.command == "GET" and caminho in ("/", "/index.html"):
            self._html(200, pagina_acesso("entrar"))
            return False
        self._json({"erro": "entre com o codigo de acesso"}, 401)
        return False

    def id_sessao(self, q):
        """Cada navegador tem a sua conferencia. O id vem da propria pagina."""
        return re.sub(r"[^0-9a-z]", "", (q.get("id", [""])[0] or "").lower())[:40]

    def arquivo_sessao(self, q):
        i = self.id_sessao(q)
        return os.path.join(SESSOES, i + ".json") if len(i) >= 8 else None

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
        if not self.porteiro():
            return
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/plano":
            cnpj = re.sub(r"\D", "", q.get("empresa", [""])[0])[:14]
            return self._json(situacao_plano(cnpj))
        if u.path == "/api/acesso":
            if not self.local():
                return self._json({"erro": "so neste computador"}, 403)
            cfg = ler_acesso()
            porta = self.server.server_address[1]
            return self._json({"rede": bool(cfg.get("rede")), "temCodigo": bool(cfg.get("codigoHash")),
                               "enderecos": [f"http://{ip}:{porta}" for ip in ips_da_maquina()]})
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
            candidatos = [self.arquivo_sessao(q)]
            if self.local():
                candidatos.append(SESSAO)  # a conferencia que ja existia antes continua aqui
            for a in candidatos:
                if a and os.path.exists(a):
                    try:
                        with open(a, encoding="utf-8") as f:
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

    def entrar(self):
        ip = self.client_address[0]
        agora = time.time()
        erros = [t for t in _tentativas.get(ip, []) if agora - t < 300]
        if len(erros) >= 5:
            return self._json({"erro": "muitas tentativas erradas; espere 5 minutos"}, 429)
        dados = self.ler_corpo(4096) or b"{}"
        try:
            codigo = str(json.loads(dados.decode("utf-8")).get("codigo", ""))
        except Exception:
            codigo = ""
        cfg = ler_acesso()
        if not cfg.get("codigoHash") or not hmac.compare_digest(hash_codigo(codigo, cfg["segredo"]), cfg["codigoHash"]):
            erros.append(agora)
            _tentativas[ip] = erros
            time.sleep(0.6)
            return self._json({"erro": "codigo incorreto"}, 401)
        _tentativas.pop(ip, None)
        b = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", f"confere={ficha(cfg)}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def classificacao(self, u, q):
        cnpj = re.sub(r"\D", "", q.get("empresa", [""])[0])[:14]
        if len(cnpj) not in (11, 14):
            return self._json({"erro": "nao sei de qual empresa sao as notas (CNPJ do tomador ausente)"}, 400)
        dados = self.ler_corpo(MAX_UPLOAD)
        if dados is None:
            return self._json({"erro": "arquivo ausente ou grande demais"}, 413)
        cl = _cl()
        nome = os.path.basename(urllib.parse.unquote(q.get("nome", ["arquivo"])[0]))[:180]
        try:
            if u.path == "/api/plano":
                tmp = _salvo_temporario(dados, nome)
                try:
                    plano = cl.ler_plano(tmp)
                finally:
                    os.remove(tmp)
                if len(plano.get("contas", [])) < 10:
                    return self._json({"erro": "nao reconheci um plano de contas neste arquivo"}, 400)
                plano["arquivo"] = nome
                plano["quando"] = datetime.now().strftime("%d/%m/%Y %H:%M")
                org = cl.organizar(plano)
                if not org["despesas"]:
                    return self._json({"erro": "o plano nao tem contas de despesa (grupo 3)"}, 400)
                aviso = ""
                if plano.get("cnpj") and plano["cnpj"] != cnpj:
                    aviso = (f"Este plano e da empresa {plano.get('empresa') or plano['cnpj']} "
                             f"(CNPJ {plano['cnpj']}), mas as notas carregadas sao de outro CNPJ ({cnpj}).")
                    if q.get("forcar", [""])[0] != "1":
                        return self._json({"erro": aviso, "outraEmpresa": True}, 409)
                _gravar_json(_arq_plano(cnpj), plano)
                out = situacao_plano(cnpj)
                out["aviso"] = aviso
                return self._json(out)

            plano = _ler_json(_arq_plano(cnpj), None)
            if not plano:
                return self._json({"erro": "importe primeiro o plano de contas desta empresa"}, 400)
            org = cl.organizar(plano)

            if u.path == "/api/classificacao/memoria":
                tmp = _salvo_temporario(dados, nome)
                try:
                    nova = cl.ler_classificacao_anterior(tmp, org)
                finally:
                    os.remove(tmp)
                if not nova:
                    return self._json({"erro": "nao achei nesta planilha CNPJ de fornecedor e conta de debito do plano"}, 400)
                antiga = _ler_json(_arq_memoria(cnpj), {})
                _gravar_json(_arq_memoria(cnpj), cl.juntar_memoria(antiga, nova))
                return self._json({"aprendidos": len(nova), "novos": len(set(nova) - set(antiga)),
                                   "memoria": len(set(nova) | set(antiga))})

            # /api/classificacao/excel
            corpo = json.loads(dados.decode("utf-8"))
            textos = [(str(a), str(b)) for a, b in corpo.get("xmls", []) if b]
            notas = cl.notas_dos_xmls(textos)
            if not notas:
                return self._json({"erro": "nenhuma NFS-e nos XMLs carregados"}, 400)
            mem = _ler_json(_arq_memoria(cnpj), {})
            comp = corpo.get("competencia") or ""
            if not comp:
                from collections import Counter
                m = Counter(n["competencia"][:7] for n in notas if n.get("competencia")).most_common(1)
                comp = f"{m[0][0][5:7]}/{m[0][0][:4]}" if m else ""
            wb, res = cl.gerar_excel(notas, org, plano.get("empresa") or corpo.get("empresaNome") or "", comp, mem)
            buf = io.BytesIO()
            wb.save(buf)
            b = buf.getvalue()
            from collections import Counter
            resumo = dict(Counter(r["confianca"] for _, _, r in res))
            resumo["notas"] = len(res)
            resumo["semFornecedor"] = sum(1 for _, _, r in res if not r["credito"])
            resumo["competencia"] = comp
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("X-Classificacao", urllib.parse.quote(json.dumps(resumo, ensure_ascii=False)))
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except Exception as e:  # noqa
            return self._json({"erro": f"nao consegui processar: {e}"}, 500)

    def configurar_acesso(self):
        if not self.local():
            return self._json({"erro": "so neste computador"}, 403)
        dados = self.ler_corpo(4096) or b"{}"
        try:
            obj = json.loads(dados.decode("utf-8"))
        except Exception:
            return self._json({"erro": "json invalido"}, 400)
        cfg = ler_acesso()
        codigo = str(obj.get("codigo") or "")
        if codigo:
            if len(codigo) < 6:
                return self._json({"erro": "o codigo precisa de pelo menos 6 caracteres"}, 400)
            cfg["codigoHash"] = hash_codigo(codigo, cfg["segredo"])
        if "rede" in obj:
            if obj["rede"] and not cfg.get("codigoHash"):
                return self._json({"erro": "defina um codigo antes de ligar o acesso pela rede"}, 400)
            cfg["rede"] = bool(obj["rede"])
        gravar_acesso(cfg)
        porta = self.server.server_address[1]
        return self._json({"rede": bool(cfg.get("rede")), "temCodigo": bool(cfg.get("codigoHash")),
                           "enderecos": [f"http://{ip}:{porta}" for ip in ips_da_maquina()]})

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except Exception as e:  # noqa
            print("erro no pedido:", e)
            self.close_connection = True

    def do_POST(self):
        if not self.porteiro():
            return
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if not self.origem_valida():
            return self._json({"erro": "origem nao autorizada"}, 403)
        if u.path == "/api/acesso/entrar":
            return self.entrar()
        if u.path in ("/api/plano", "/api/classificacao/memoria", "/api/classificacao/excel"):
            return self.classificacao(u, q)
        if u.path == "/api/acesso":
            return self.configurar_acesso()
        if u.path == "/api/portal/config" and not self.local():
            # o certificado da empresa so se escolhe no computador onde ele esta
            return self._json({"erro": "o certificado so pode ser trocado no computador onde ele esta instalado"}, 403)
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
        if not self.porteiro():
            return
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if not self.origem_valida():
            return self._json({"erro": "origem nao autorizada"}, 403)
        if u.path == "/api/sessao":
            alvos = [self.arquivo_sessao(q)]
            if self.local():
                alvos.append(SESSAO)      # a antiga nao pode ressuscitar depois de apagada
            for a in alvos:
                if a and os.path.exists(a):
                    try:
                        os.remove(a)
                    except Exception:
                        pass
            return self._json({"ok": True})
        return self._json({"erro": "rota desconhecida"}, 404)

    def do_PUT(self):
        if not self.porteiro():
            return
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
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
            destino = self.arquivo_sessao(q) or (SESSAO if self.local() else None)
            if not destino:
                return self._json({"erro": "identificador da sessao ausente"}, 400)
            tmp = destino + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, destino)
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
        # Escuta na rede, mas quem decide quem entra e o porteiro: com o acesso pela rede
        # desligado (o padrao), pedido de fora recebe 403 e nada mais.
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", porta), Handler)
    except OSError as e:
        print(f"Nao consegui abrir a porta {porta}: {e}")
        print("Ja existe um Confere NFS-e aberto? Feche a outra janela preta e tente de novo.")
        try:
            input("Enter para fechar...")
        except (EOFError, RuntimeError):   # sem janela preta nao ha quem responda
            pass
        sys.exit(3)
    print(f"Confere NFS-e rodando em http://localhost:{porta}  (motores OCR: {', '.join(motores_disponiveis()) or 'nenhum'})")
    if "--sem-navegador" not in args:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{porta}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
