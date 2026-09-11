# Confere NFS-e · Totali

Cruza os XMLs das NFS-e com os PDFs (inclusive digitalizados) e mostra o que bate e o que não bate.

## Como abrir

1. Dê dois cliques em `iniciar.bat`. Ele sobe o servidor local e abre o navegador em http://localhost:8131.
2. Passo 1: solte os XMLs (ou um ZIP com todos).
3. Passo 2: solte os PDFs (ou um ZIP). Um PDF com várias notas dentro é separado nota a nota.
4. A conferência aparece sozinha. Clique numa linha para ver campo a campo, com a página do PDF onde cada dado foi lido.
5. "Baixar Excel" gera a planilha no padrão Totali (logo, cores, duas abas). "Imprimir / PDF" imprime a tabela com o cabeçalho da Totali.

## Cabeçalho

- **Empresa (CNPJ)** e **Papel** (tomadora ou prestadora): com isso o sistema sabe qual CNPJ da nota é o seu e qual é o da outra parte, mesmo quando o OCR não lê os rótulos "Prestador" / "Tomador".
- **OCR**: três modos.
  - **Duplo (recomendado)**: roda o motor preciso, reforça a leitura quando a página sai fraca e completa os buracos com o OCR do Windows. Lê também o **QR code** da nota, que traz a chave de acesso sem risco de erro de leitura. ~20 s por página.
  - **Preciso**: só o RapidOCR. ~15 s por página.
  - **Rápido**: só o OCR do Windows. Instantâneo, mas troca dígitos em scan ruim.
  Cada PDF passa pelo OCR uma vez; o resultado fica em `cache/`.

## O sistema guarda o trabalho

Ao fechar e abrir de novo, o sistema **retoma de onde você parou**: os documentos já lidos, as notas dos XMLs, as correções que você digitou e as decisões de aprovar ou não importar. Uma faixa verde no topo confirma a retomada e traz o botão **começar do zero**.

Os PDFs não precisam ser enviados de novo: o servidor guarda a leitura de cada arquivo e a recupera pelo código do arquivo. O trabalho em andamento fica em `sessao.json`, junto com o sistema.

Também ficam guardados entre um uso e outro: o CNPJ e o papel da empresa, os acumuladores e CFOPs do Domínio, os modelos ensinados, o certificado escolhido para o Portal Nacional e o último NSU lido.

Há duas cópias do trabalho: uma no servidor (`sessao.json`) e outra dentro do próprio navegador. Se o servidor estiver fora do ar, a do navegador segura o que der. Ao voltar, o sistema **pergunta** antes de retomar, mostrando de quando é e o que tem dentro — e só apaga se você escolher começar do zero.

## Histórico de usos

Cada trabalho concluído deixa um registro no Firebase da Totali: data, empresa, competência, quantos XMLs, quantos documentos no PDF e de que tipo, quantos conferiram, quantos divergiram, quantos ficaram pendentes e quantos foram importados.

**Vai só isso.** Nenhum PDF, nenhum XML, nenhum número de nota, nenhum valor e nenhum nome de fornecedor saem da máquina. O registro serve para saber quanto o sistema foi usado e onde ele mais erra.

O que liga esse histórico é o arquivo `firebase-config.js`, na pasta do sistema. Sem ele, tudo funciona igual — só não grava o histórico.

## A tela

- **Topo:** as duas áreas para soltar os arquivos. Depois de carregados, viram uma linha com a contagem (passe o mouse para ver os nomes dos arquivos).
- **Documentos identificados:** quantos documentos o sistema achou dentro dos PDFs, separados por tipo (NFS-e, faturas, notas de débito, NF de comunicação, outros, páginas sem texto) mais o total. Serve para conferir se a quantidade bate com o que você esperava do arquivo. Clique num tipo para filtrar a tabela.
- **Resultado da conferência:** quantas notas conferem, divergem, etc. Clique para filtrar; clique de novo (ou no × ao lado da busca) para tirar o filtro.
- **Tabela:** uma linha por documento, com situação, os três selos da conferência, número e tipo, data, prestador, valor e o que precisa ser olhado. O valor só mostra os dois lados quando eles diferem. Clique na linha para abrir o detalhe.

## A conferência

O sistema pergunta uma coisa: **este XML e este PDF são a mesma nota?** A resposta se apoia em três pontos, mostrados na coluna **XML × PDF** como três selos:

| Selo | O que compara |
|---|---|
| CNPJ | CNPJ/CPF do prestador e do tomador no XML contra os do PDF |
| Nº | número da nota nos dois arquivos |
| Data | data de emissão nos dois arquivos |

Verde com ✓ = igual nos dois. Vermelho com ✗ = diferente. Cinza com ? = não foi possível ler aquele dado no PDF.

**Nomes não valem como critério.** A grafia da razão social muda entre o XML e a impressão (abreviações, acentos, OCR), então nome entra só como informação no campo a campo, nunca decide a situação da nota.

**Situação de cada linha:**

- **Confere** — CNPJ, número e data batem, e nenhum outro campo diverge.
- **Divergente** — um dos três selos falhou, ou divergiu valor, chave ou código de verificação.
- **Atenção** — um dos três não foi lido no PDF, ou há diferença em campo secundário (base, alíquota, ISS, líquido, competência, retenções).
- **Sem PDF / Sem XML** — o par não foi encontrado.
- **Outro documento** — fatura, nota de débito ou NF de comunicação, que não têm XML de NFS-e.
- **PDF sem texto** — nem o OCR conseguiu ler a página.

Clicando na linha, um bloco no topo mostra os três pontos lado a lado com os valores de cada lado. Quando o CNPJ diverge mas o dígito verificador lido no PDF não fecha, o sistema avisa que é provável erro de leitura, não divergência real. Abaixo vem o campo a campo completo, com a página do PDF de onde saiu cada dado.

**Como o par é formado:** chave de acesso, depois número + CNPJ do prestador, código de verificação, número + tomador, valor + número, número sozinho e, por último, valor + data + prestador. Só casa quando existe um único candidato dos dois lados.

## Como o PDF é separado em documentos

Cada página do PDF vira um documento. Uma página só é tratada como continuação da anterior quando não tem identidade própria: sem chave de acesso, sem número e sem cabeçalho de nota, e com pouco texto. Isso evita o erro de uma fatura mal lida pelo OCR ser engolida pela nota da página anterior.

Os tipos reconhecidos: **NFS-e**, **Fatura**, **Recibo**, **Nota de débito**, **NF de comunicação** (NFCom), **NF de energia** e **Outro**. O tipo sai do texto da página; se errar, você corrige na conferência final ou ensinando o modelo.

## Conferência final (antes de importar)

O botão **Conferência final** abre a revisão documento por documento com a **imagem da página ao lado**:

- Navegue com ◀ ▶ ou pelos quadradinhos numerados no rodapé (cada um é uma página).
- **Só os que precisam de atenção** filtra o que ficou com aviso: valor não lido, CNPJ com dígito que não fecha, sem número, sem data.
- Os campos à direita são editáveis. O que você corrigir vale para a exportação e fica guardado nesta máquina.
- **Aprovar** ou **Não importar** cada documento. **Aprovar todos os que não têm aviso** resolve o volume de uma vez.
- Ao concluir, a exportação do Domínio já sai só com o que foi aprovado.

## Portal Nacional da NFS-e

O botão **Portal Nacional** consulta as notas em que a empresa aparece no Ambiente de Dados Nacional (ADN) e confronta com o que você recebeu, apontando nota do portal sem PDF correspondente.

Para funcionar são necessários:

1. **Certificado digital A1 da empresa instalado neste computador.** O sistema lista os certificados do Windows num menu: basta escolher o da empresa. A chave privada nunca sai do Windows. Também aceita arquivo `.pfx` com senha, para quem tem o certificado solto em disco.
2. **Credenciamento da empresa no Portal Nacional** (gov.br/nfse). Sem isso o portal recusa com 403.

O certificado escolhido fica guardado em `portal.json`, só nesta máquina. A consulta caminha por uma esteira numerada (NSU) e o sistema guarda onde parou, então da segunda vez só traz o que é novo. Escolha a **competência** e o **papel** (tomadora ou prestadora) antes de buscar.

A primeira varredura lê tudo o que existe na esteira desde o começo (na Play foram 7.316 documentos em menos de 3 minutos) e traz só as notas da competência pedida. O confronto mostra quantas notas do portal têm PDF, quais ficaram sem PDF, e quais NFS-e em PDF não vieram do portal.

**Limite importante:** o Portal Nacional só tem as notas do padrão nacional. Municípios com sistema próprio (é o caso de várias prefeituras) não aparecem ali.

## Modelos (o coletor)

Cada prefeitura ou emissor tem um layout. Quando aparece um layout desconhecido, a nota recebe a etiqueta **modelo novo**.

1. Abra a linha da nota e clique em **Ensinar modelo**.
2. À direita ficam os campos e o que foi lido automaticamente. Clique num campo e depois clique, na imagem da nota, na linha onde o valor está. O sistema aprende o rótulo (ex.: "VALOR TOTAL DA NOTA") e a posição.
3. Dê um nome ao modelo e salve. Todas as notas do mesmo layout são relidas na hora.
4. Da próxima vez, o sistema reconhece o layout pela assinatura do texto e pelo CNPJ do emissor, e aplica as regras sozinho.

Os modelos ficam em `modelos.json` (ao lado deste arquivo). Para compartilhar com outra máquina, use "Modelos" → Exportar / Importar, ou copie o arquivo.

## Excel do Domínio (importação de serviços tomados)

O botão **Excel do Domínio** preenche o modelo oficial `Modelo Notas de Serviços Tomados.xlsm` (guardado em `modelos-dominio\`), mantendo a macro **Gerar** e o botão. Também gera direto o `ServicoTomados.txt` que o Domínio importa (mesmo conteúdo que a macro produz).

- **O que entra:** por padrão, as NFS-e sem XML (só PDF) e as faturas / notas de débito. NF de comunicação e NFS-e já conferidas com XML podem ser marcadas também.
- **De onde vêm os dados:** do XML quando a nota tem XML; senão, do PDF (texto ou OCR).
- **Fornecedor:** vai só o CPF/CNPJ. Razão social, UF, município e endereço ficam em branco de propósito (o Domínio localiza o cadastro pelo CNPJ; preencher dá erro). A razão social lida fica na coluna de apoio.
- **Parâmetros** (ficam salvos): acumulador de NFS-e, acumulador de fatura/nota de débito, séries, UF da empresa, CFOP dentro/fora do estado (1933/2933, decidido pela UF do prestador), data de entrada (igual à emissão ou fixa).
- **Colunas de apoio (AC em diante):** tipo, razão social lida, página do PDF, arquivo, origem, avisos (sem número, sem valor, CNPJ com dígito inválido, acumulador vazio). A macro ignora essas colunas.
- **Retenções:** ISS normal/retido, IRRF, PIS, COFINS, CSLL e INSS quando aparecem na nota (linha de retenções federais). Confira antes de importar.

## Requisitos (já instalados nesta máquina)

Python 3.12 com: `pypdfium2 pillow numpy winocr rapidocr_onnxruntime` (ver `requisitos.txt`).
Para instalar em outra máquina: `python -m pip install -r requisitos.txt`.

## Arquivos

- `index.html` — interface (funciona também sem servidor, só para PDFs com texto).
- `servidor.py` — servidor local: OCR, imagens das páginas e guarda dos modelos.
- `modelos.json` — modelos ensinados.
- `cache/` — OCR já feito (pode apagar; será refeito quando precisar).
- `testes/` — arquivos de exemplo (XML nacional, ABRASF, DANFSE em PDF).
- `modelos-dominio/` — modelo oficial do Domínio (.xlsm) usado na exportação.
- `portal_nacional.py` — consulta ao ADN com o certificado digital.
- `portal.json` — certificado e senha (criado quando você configura; não compartilhe).
- `firebase-config.js` — endereço do banco do histórico de usos (fica só nesta máquina).

## Segurança

- O servidor escuta só em `127.0.0.1`: não fica acessível para a rede.
- Não entrega como página os PDFs do cache, os modelos, o `portal.json` nem o código.
- Recusa pedidos vindos de outros sites abertos no navegador (proteção contra CSRF).
- Confere se o arquivo enviado é mesmo um PDF e limita o envio a 300 MB.
- O certificado digital nunca sai da máquina; a senha fica só em `portal.json`.
- Para o histórico de usos vai apenas o resumo. As regras do banco recusam qualquer campo além dos combinados, e ninguém consegue alterar ou apagar um registro já gravado.
