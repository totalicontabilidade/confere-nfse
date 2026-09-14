# -*- coding: utf-8 -*-
"""
Portal Nacional da NFS-e - consulta as notas da empresa no ADN (Ambiente de Dados Nacional).

Rotas (Swagger "API NFS-e - ADN Contribuinte", producao restrita e producao):
  GET /DFe/{NSU}?lote=true&cnpjConsulta=...   -> lote de documentos a partir do NSU
  GET /NFSe/{ChaveAcesso}/Eventos             -> eventos de uma nota

A conexao e TLS com autenticacao mutua. O certificado pode vir de dois lugares:

  1. Repositorio de certificados do Windows (o jeito normal por aqui): basta escolher o
     certificado da empresa na tela. A chave privada nunca e exportada - quem assina e o
     proprio Windows, via curl.exe com backend Schannel.
  2. Arquivo .pfx / .p12 com senha, para quem tem o certificado solto em disco.

O que fica guardado em portal.json (so nesta maquina): a impressao digital do certificado
escolhido (ou o caminho do .pfx e a senha), o ambiente, o CNPJ e o ultimo NSU lido.
"""
import base64, gzip, json, os, re, subprocess, tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "portal.json")

AMBIENTES = {
    "producao": "https://adn.nfse.gov.br/contribuintes",
    "restrita": "https://adn.producaorestrita.nfse.gov.br/contribuintes",
}


# ------------------------------------------------------------------ configuração
def ler_config():
    if not os.path.exists(CONFIG):
        return {}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def gravar_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    try:  # o arquivo pode guardar a senha do .pfx: só o dono lê
        os.chmod(CONFIG, 0o600)
    except Exception:
        pass


# ------------------------------------------------------------------ certificados do Windows
_PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]


def _powershell(script, timeout=60):
    r = subprocess.run(_PS + [script], capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.strip(), r.stderr.strip()


def listar_certificados():
    """Certificados com chave privada instalados no Windows, mais novos primeiro."""
    script = (
        "Get-ChildItem Cert:\\CurrentUser\\My,Cert:\\LocalMachine\\My -ErrorAction SilentlyContinue | "
        "Where-Object { $_.HasPrivateKey } | "
        "Select-Object @{n='thumbprint';e={$_.Thumbprint}}, @{n='titular';e={$_.Subject}}, "
        "@{n='validade';e={$_.NotAfter.ToString('yyyy-MM-dd')}}, "
        "@{n='loja';e={if($_.PSParentPath -like '*LocalMachine*'){'LocalMachine\\My'}else{'CurrentUser\\My'}}} | "
        "ConvertTo-Json -Compress"
    )
    saida, _ = _powershell(script)
    if not saida:
        return []
    try:
        dados = json.loads(saida)
    except Exception:
        return []
    if isinstance(dados, dict):
        dados = [dados]
    hoje = datetime.now().strftime("%Y-%m-%d")
    out = []
    vistos = set()
    for c in dados:
        thumb = (c.get("thumbprint") or "").upper()
        if not thumb or thumb in vistos:
            continue
        vistos.add(thumb)
        titular = c.get("titular") or ""
        m = re.search(r"CN=([^,]+)", titular)
        nome = (m.group(1) if m else titular).strip()
        doc = ""
        d = re.search(r":(\d{11,14})\s*$", nome)
        if d:
            doc = d.group(1)
            nome = nome[: d.start()].strip()
        out.append({
            "thumbprint": thumb, "nome": nome, "doc": doc, "loja": c.get("loja") or "CurrentUser\\My",
            "validade": c.get("validade") or "", "vencido": (c.get("validade") or "9999") < hoje,
        })
    out.sort(key=lambda c: (c["vencido"], c["nome"]))
    return out


def _cert_por_thumb(thumb):
    for c in listar_certificados():
        if c["thumbprint"] == (thumb or "").upper():
            return c
    return None


# ------------------------------------------------------------------ certificado em arquivo
def _carregar_pfx(caminho, senha):
    from cryptography.hazmat.primitives.serialization import pkcs12, Encoding, PrivateFormat, NoEncryption
    with open(caminho, "rb") as f:
        dados = f.read()
    chave, cert, extras = pkcs12.load_key_and_certificates(dados, (senha or "").encode("utf-8"))
    if chave is None or cert is None:
        raise ValueError("o arquivo nao tem chave privada e certificado (senha errada?)")
    pem = cert.public_bytes(Encoding.PEM)
    for c in (extras or []):
        pem += c.public_bytes(Encoding.PEM)
    return cert, pem, chave.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def dados_certificado_arquivo(caminho, senha):
    cert, _, _ = _carregar_pfx(caminho, senha)
    nome = cert.subject.rfc4514_string()
    m = re.search(r"(\d{14})", re.sub(r"[.\-/]", "", nome))
    validade = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    return {"nome": nome[:90], "doc": m.group(1) if m else "", "validade": validade.strftime("%Y-%m-%d"),
            "vencido": validade.timestamp() < datetime.now().timestamp()}


# ------------------------------------------------------------------ situação
def situacao():
    cfg = ler_config()
    origem = cfg.get("origem", "windows")
    info, pronto = {}, False
    if origem == "arquivo":
        cam = cfg.get("certificado", "")
        if cam and os.path.isdir(cam):
            info = {"erro": "esse caminho é uma pasta; aponte o arquivo .pfx dentro dela"}
        elif cam and os.path.exists(cam) and cfg.get("senha"):
            try:
                info = dados_certificado_arquivo(cam, cfg["senha"]); pronto = not info.get("vencido")
            except Exception as e:
                info = {"erro": str(e)}
        elif cam:
            info = {"erro": "arquivo não encontrado"}
    else:
        c = _cert_por_thumb(cfg.get("thumbprint"))
        if c:
            info = c; pronto = not c["vencido"]
    return {
        "configurado": pronto, "origem": origem, "thumbprint": cfg.get("thumbprint", ""),
        "caminho": cfg.get("certificado", ""), "ambiente": cfg.get("ambiente", "producao"),
        "cnpj": cfg.get("cnpj", ""), "ultimoNSU": cfg.get("ultimoNSU", 0),
        "fimEsteira": cfg.get("fimEsteira", 0), "maxSegundos": 240,
        "certificado": info, "certificados": listar_certificados(), "temCurl": _tem_curl(),
    }


def _tem_curl():
    try:
        r = subprocess.run(["curl.exe", "--version"], capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "Schannel" in (r.stdout or "")
    except Exception:
        return False


# ------------------------------------------------------------------ consulta
def _pedir(url, cfg, pem_tmp=None, timeout=90):
    """Devolve (status, texto). Usa o certificado do Windows ou o par PEM do .pfx."""
    cmd = ["curl.exe", "-s", "-S", "--max-time", str(timeout), "-w", "\n@@HTTP:%{http_code}"]
    if cfg.get("origem") == "arquivo":
        cmd += ["--cert", pem_tmp, "--key", pem_tmp]
    else:
        c = _cert_por_thumb(cfg.get("thumbprint")) or {}
        loja = c.get("loja", "CurrentUser\\My")
        cmd += ["--cert", f"{loja}\\{cfg.get('thumbprint','')}"]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout + 30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    saida = r.stdout or ""
    m = re.search(r"@@HTTP:(\d+)\s*$", saida)
    status = int(m.group(1)) if m else 0
    corpo = saida[: m.start()] if m else saida
    if not status and r.stderr:
        corpo = r.stderr
    return status, corpo


class _PemTemporario:
    """Só para certificado em arquivo: escreve o PEM num temporário e apaga ao sair."""

    def __init__(self, cfg):
        self.cfg, self.arq = cfg, None

    def __enter__(self):
        if self.cfg.get("origem") != "arquivo":
            return None
        _, pem, chave = _carregar_pfx(self.cfg["certificado"], self.cfg.get("senha", ""))
        fd, nome = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "wb") as f:
            f.write(chave + pem)
        try:
            os.chmod(nome, 0o600)
        except Exception:
            pass
        self.arq = nome
        return nome

    def __exit__(self, *a):
        if self.arq and os.path.exists(self.arq):
            try:
                os.remove(self.arq)
            except Exception:
                pass


def _descompacta(valor):
    """O ADN devolve o XML em GZip + Base64."""
    if not valor:
        return ""
    try:
        bruto = base64.b64decode(valor)
    except Exception:
        return valor if isinstance(valor, str) else ""
    if bruto[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(bruto).decode("utf-8", "replace")
        except Exception:
            pass
    return bruto.decode("utf-8", "replace")


def _competencia_do_xml(xml):
    m = re.search(r"<dCompet>(\d{4})-(\d{2})", xml) or re.search(r"<dhEmi>(\d{4})-(\d{2})", xml) or re.search(r"<dhProc>(\d{4})-(\d{2})", xml)
    return f"{m.group(1)}-{m.group(2)}" if m else ""


def _papel_no_xml(xml, cnpj):
    """'tomador', 'prestador' ou '' - onde o CNPJ da empresa aparece na nota."""
    if not cnpj:
        return ""
    toma = re.search(r"<toma>.*?</toma>", xml, re.S)
    prest = re.search(r"<prest>.*?</prest>", xml, re.S) or re.search(r"<emit>.*?</emit>", xml, re.S)
    if toma and cnpj in re.sub(r"\D", "", toma.group(0)):
        return "tomador"
    if prest and cnpj in re.sub(r"\D", "", prest.group(0)):
        return "prestador"
    return ""


def _pista_do_xml(xml):
    """(cnpj do prestador, valor com 2 casas, numero) - por regex, que e barato:
    isto roda uma vez por documento da esteira, e sao milhares."""
    bloco = re.search(r"<(?:emit|prest|PrestadorServico)>.*?</(?:emit|prest|PrestadorServico)>", xml, re.S)
    cnpj = ""
    if bloco:
        m = re.search(r"<(?:CNPJ|Cnpj|CPF|Cpf)>([\d.\-/]+)<", bloco.group(0))
        if m:
            cnpj = re.sub(r"\D", "", m.group(1))
    valor = ""
    m = re.search(r"<(?:vLiq|ValorLiquidoNfse|vServ|ValorServicos)>([\d.,]+)<", xml)
    if m:
        try:
            valor = "%.2f" % float(m.group(1).replace(",", "."))
        except ValueError:
            valor = ""
    m = re.search(r"<(?:nNFSe|Numero)>([^<]+)<", xml)
    numero = re.sub(r"\D", "", m.group(1)).lstrip("0") if m else ""
    return cnpj, valor, numero


def _serve_de_pista(xml, pistas):
    """A nota interessa mesmo fora da competencia se ela casa com um PDF que ficou
    sem XML na tela: mesmo prestador e mesmo valor, ou mesmo prestador e mesmo numero."""
    if not pistas:
        return False
    cnpj, valor, numero = _pista_do_xml(xml)
    if not cnpj:
        return False
    for p in pistas:
        if p.get("cnpj") != cnpj:
            continue
        if valor and p.get("valor") and valor == p["valor"]:
            return True
        if numero and p.get("numero") and numero == p["numero"]:
            return True
    return False


def consultar_dfe(nsu_inicial=None, limite_lotes=400, progresso=None, competencia=None, pistas=None,
                  papel=None, max_segundos=240):
    """Percorre a esteira do ADN a partir do NSU e devolve os XMLs.
    `competencia` = 'AAAA-MM' (ou lista) filtra o que volta; a esteira continua avancando.
    {'xmls': [...], 'ultimoNSU': n, 'total': n, 'lidos': n, 'erro': str|None, 'fim': bool}"""
    import time as _t
    inicio = _t.time()
    comps = [competencia] if isinstance(competencia, str) and competencia else (competencia or [])
    comps = [c for c in comps if c]
    pistas = [p for p in (pistas or []) if p.get("cnpj")]
    cfg = ler_config()
    st = situacao()
    if not st["configurado"]:
        return {"erro": "certificado não configurado ou vencido", "xmls": []}
    if not st["temCurl"]:
        return {"erro": "o curl do Windows (com Schannel) não foi encontrado nesta máquina", "xmls": []}
    base = AMBIENTES.get(cfg.get("ambiente", "producao"), AMBIENTES["producao"])
    nsu = int(nsu_inicial if nsu_inicial is not None else cfg.get("ultimoNSU", 0))
    cnpj = re.sub(r"\D", "", cfg.get("cnpj", "") or "")
    xmls, erro, fim, lidos = [], None, False, 0
    with _PemTemporario(cfg) as pem:
        for _ in range(limite_lotes):
            if _t.time() - inicio > max_segundos:
                erro = f"parei em {max_segundos}s para não travar a tela; clique de novo para continuar de onde parou"
                break
            url = f"{base}/DFe/{nsu}?lote=true"
            if cnpj:
                url += f"&cnpjConsulta={cnpj}"
            status, corpo = _pedir(url, cfg, pem)
            if status == 0:
                erro = f"falha de conexão: {corpo[:200]}"
                break
            try:
                dados = json.loads(corpo)
            except Exception:
                if status >= 400:
                    erro = f"o ADN respondeu {status}"
                else:
                    erro = "resposta do ADN não veio em JSON"
                break
            estado = dados.get("StatusProcessamento") or ""
            lote = dados.get("LoteDFe") or []
            if estado == "NENHUM_DOCUMENTO_LOCALIZADO" or (status == 404 and not lote):
                fim = True
                break
            if status in (401, 403):
                erro = "o ADN recusou o certificado (403). Confira o credenciamento da empresa no gov.br/nfse."
                break
            if status >= 400 and not lote:
                d = (dados.get("Erros") or [{}])[0]
                erro = f"{d.get('Codigo', status)}: {d.get('Descricao', corpo[:160])}"
                break
            if not lote:
                fim = True
                break
            for item in lote:
                doc = item.get("ArquivoXml")
                n = item.get("NSU")
                if n is not None:
                    nsu = max(nsu, int(n))
                if not doc:
                    continue
                lidos += 1
                xml = _descompacta(doc)
                comp = _competencia_do_xml(xml)
                por_pista = False
                if comps and comp not in comps:
                    if not _serve_de_pista(xml, pistas):
                        continue
                    por_pista = True   # veio por palpite: se não achar o PDF dela, some
                pp = _papel_no_xml(xml, cnpj)
                if papel and pp and pp != papel:
                    continue
                xmls.append({"nsu": n, "chave": item.get("ChaveAcesso"), "competencia": comp,
                             "papel": pp, "tipo": item.get("TipoDocumento"), "porPista": por_pista,
                             "xml": xml})
            if progresso:
                progresso(lidos, nsu)
    if fim:
        # ate onde a esteira chegou hoje. Na proxima varredura isto vira a regua
        # da barra de progresso: sem isso nao da para dizer quanto falta.
        cfg["fimEsteira"] = nsu
    if xmls or fim:
        cfg["ultimoNSU"] = nsu
        gravar_config(cfg)
    return {"xmls": xmls, "ultimoNSU": nsu, "total": len(xmls), "lidos": lidos, "erro": erro,
            "fim": fim, "segundos": round(_t.time() - inicio, 1)}


def testar():
    """Consulta um lote só, para conferir certificado e credenciamento."""
    st = situacao()
    if not st["configurado"]:
        return {"ok": False, "erro": st["certificado"].get("erro") or "certificado não escolhido", "situacao": st}
    r = consultar_dfe(nsu_inicial=ler_config().get("ultimoNSU", 0), limite_lotes=1, max_segundos=60)
    ok = not r.get("erro")
    return {"ok": ok, "erro": r.get("erro"), "certificado": st["certificado"], "encontrados": r.get("total", 0),
            "fim": r.get("fim"), "lidos": r.get("lidos", 0), "ambiente": st["ambiente"]}
