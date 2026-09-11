# Confere NFS-e

Conferência de notas fiscais de serviço: cruza os **XMLs** com os **PDFs** das notas, separa cada documento que existe dentro do PDF, aponta o que não bate e monta o arquivo de importação do Domínio Contábil.

Feito para o escritório trabalhar com o que chega na prática: PDFs digitalizados, com várias notas no mesmo arquivo, de prefeituras diferentes.

## O que ele faz

- **Lê os dois lados.** Do XML (padrão nacional e ABRASF) e do PDF, inclusive quando o PDF é só imagem.
- **Separa o PDF em documentos.** Cada página vira um documento, classificado como NFS-e, fatura, recibo, nota de débito, NF de comunicação, NF de energia ou outro.
- **Confere pelo que importa:** CNPJ, número e data de emissão. Nome não vale como critério, porque a grafia muda entre o XML e a impressão.
- **Só passa o que leu com certeza.** Qualquer campo com leitura duvidosa vai para a conferência do usuário, com a imagem da página ao lado.
- **Aprende o leiaute de cada emissor.** Você ensina uma vez clicando no campo e na linha da nota; nas próximas o sistema reconhece sozinho.
- **Consulta o Portal Nacional** (Ambiente de Dados Nacional) com o certificado digital da empresa e confronta com o que você recebeu.
- **Exporta para o Domínio** no modelo oficial de Notas de Serviços Tomados, com a macro preservada, ou direto no TXT.

## Como rodar

Precisa de Python 3.11 ou mais novo, no Windows.

```bash
python -m pip install -r requisitos.txt
```

Depois, dois cliques em `Confere NFS-e.vbs` (abre sem janela de comando) ou em `iniciar.bat`. O sistema abre em `http://localhost:8131`.

O servidor escuta só em `127.0.0.1`: nada fica exposto na rede.

## Leitura de PDF digitalizado

Três modos, escolhidos no cabeçalho:

| Modo | Como funciona | Tempo por página |
|---|---|---|
| Duplo | RapidOCR, reforço em resolução maior quando a página sai fraca, complemento com o OCR do Windows e leitura de QR code | ~20 s |
| Preciso | Só o RapidOCR | ~15 s |
| Rápido | Só o OCR do Windows | instantâneo |

Quando o PDF já tem texto, nada disso é usado: o texto é lido direto, com posição.

Cada arquivo passa pela leitura uma vez só; o resultado fica guardado e volta na hora.

## A regra da certeza

O sistema nunca finge que leu. Um campo só é dado como certo quando veio do XML, do QR code, do texto do próprio PDF, foi confirmado por você, ou o reconhecimento ficou acima do limiar **e** o valor passa na validação do campo: dígito verificador do CNPJ, data plausível, valor maior que zero.

Fora disso, o campo entra na conferência final com o motivo em texto claro, e o documento não pode ser aprovado antes de você resolver.

## Portal Nacional

Usa a rota `GET /DFe/{NSU}` do ADN, com autenticação mútua por certificado ICP-Brasil A1. O sistema lista os certificados instalados no Windows para você escolher; a chave privada nunca sai da máquina.

Além do certificado, a empresa precisa estar credenciada no Portal Nacional (gov.br/nfse).

Só existem ali as notas do padrão nacional: municípios com sistema próprio não aparecem.

## Privacidade

Nada sai do computador, fora a consulta ao Portal Nacional. Os PDFs, os XMLs, o texto lido e a conferência em andamento ficam na pasta do sistema e não vão para este repositório.

## Arquivos

| Arquivo | Para que serve |
|---|---|
| `index.html` | A tela inteira do sistema |
| `servidor.py` | Servidor local: leitura dos PDFs, imagens das páginas, modelos e sessão |
| `portal_nacional.py` | Consulta ao Ambiente de Dados Nacional |
| `modelos-dominio/` | Modelo oficial do Domínio usado na exportação |
| `testes/` | Notas fictícias para testar sem dado de cliente |
| `LEIA-ME.md` | Manual de uso, em português, para quem vai operar |

---

Desenvolvido para a **Totali Soluções Contábeis**.
