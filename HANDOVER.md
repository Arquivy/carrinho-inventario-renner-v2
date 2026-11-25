# 📋 HANDOVER: Carrinho de Inventário RFID Renner (v3)

> **Documento de Transição Técnica e Continuidade de Desenvolvimento**  
> **Última Atualização:** 24 de Novembro de 2026  
> **Versão:** `v3` (Branch `main` + Tag `v3`)  
> **Localização Atual:** `E:\carrinho-inventario-renner-v2`  
> **Status:** Sistema otimizado para alta densidade no CD com leitor Impinj R700 em **LLRP** (`R420`), **Bancada & Gravação** exclusiva para tags de endereço com busca por SKU e slider de RSSI isolado, **Carrinho RFID** com pré-processamento de risco de vazamento (`±2`), reconciliação espacial pós-processamento por EPCs e **Dashboard Executivo** completo com exportação Excel/PDF.

---

## 1. 🎯 Visão Geral do Projeto

O **Carrinho de Inventário da Renner** é uma solução de automação e conferência de estoque móvel para os centros de distribuição (CD) e lojas da **Lojas Renner**. O sistema combina:
1. **Home (`/`):** Importação de planilha Excel/CSV (offline via SheetJS local), seleção de corredor, gerenciamento de dispositivos e tabela comparativa de estoque.
2. **Carrinho RFID (`/carrinho`):** Dashboard operacional em tempo real em 4 quadrantes (Antenas 1 a 4), pré-processamento automático de **risco de vazamento** com ruas vizinhas (`±2`), captura leve de vazamentos e reconciliação espacial pós-processamento por EPCs compartilhados.
3. **Dashboard Executivo (`/carrinho/dashboard`):** Fechamento gráfico do inventário (100% offline) com 6 KPIs, gráficos por rua/status/quadrante, detecção de excesso e risco de vazamento, tabela de auditoria com filtros e exportação Excel/PDF.
4. **Bancada & Gravação (`/gravacao`):** Ferramenta exclusiva para gravar **tags físicas de endereço (`edc0`)** ancoradas no **TID**, com localização automática de endereço pelo SKU lido (maior RSSI) e controles independentes de potência e RSSI isolados da config do CD.

---

## 2. 🛠️ Stack & Arquitetura Atual

- **Linguagem:** Python 3.11+ (gerenciado via **Poetry**)
- **Backend:** FastAPI + Uvicorn (porta padrão `8000`)
- **Frontend:** Jinja2 + Tailwind CSS + Alpine.js + SheetJS local (`app/static/js/xlsx.full.min.js`) — gráficos em SVG/HTML puros (**100% offline**)
- **Banco de Dados:** SQLite (`database.db`) via SQLAlchemy (`Event`, `Inventory`, `Tag`)
- **SDK RFID:** `smartx-rfid` (v12.8.5) + `sllurp` (LLRP)

### Hardware & Configuração do Leitor Impinj R700 (Perfil CD)
- **Arquivo do dispositivo:** `config/devices/impinj-17-39-4a.json`
- **IP / Driver:** `192.168.1.21` | `"reader": "R420"` (LLRP porta `5084`) | `"firmware_version": null`
- **Ajustes para Alta Densidade no CD:**
  - `"session": 2` e `"search_mode": "single-target"` (silencia tags frontais lidas para enxergar peças escondidas atrás/no meio das pilhas)
  - `"active_ant": [1, 2, 3, 4]` (todas as 4 antenas do carrinho ativas)
  - `"read_power": 32.0` dBm e `"read_rssi": -80` dBm
  - `"rf_mode": 4`
- **Configuração Isolada da Bancada:** `config/gravacao_bancada.json` armazena `read_power`, `write_power` (padrão `25.0` dBm) e `rssi_min` (`-90` a `-20` dBm) sem sobrescrever `config/devices/impinj-17-39-4a.json`.

---

## 3. ✅ Funcionalidades Implementadas e Testadas (até v3)

### A. Codec RFID Renner (`app/services/rfid/renner_codec.py`)
- **Tag de Endereço (`edc0`):** `renner_serialize_epc_by_address("EN0.C131.S002.N1.0")` → `edc000000131000200010000` (24 hex) e decodificação reversa `renner_get_address_by_epc`.
- **Tag de SKU:** `get_renner_sku(epc)` decodifica o SKU de 24 hex.

### B. Motor do Carrinho, Otimização & Vazamento (`app/routers/api/v1/carrinho.py`)
- **Alta Performance em Alta Densidade (16 GB RAM):**
  - `pre_read_buffer` estruturado como `dict` indexado por `epc` (zero duplicatas) e limpeza com *throttle* de `0.5s`.
  - Sem `logging.info` por tag individual no caminho crítico `on_tag_read`.
- **Modo Único (Tempo Real) + Contagem de Excesso:**
  - Removido o modo alternativo de processamento em lote da UI; o fluxo unificado opera em tempo real e contabiliza **excesso** quando um endereço ativo recebe mais peças físicas de um SKU do que o esperado.
- **Regra de Vizinhança Física (`±2`):**
  - No CD, ímpares (`129, 131, 133`) e pares (`128, 130, 132`) ficam em lados separados, logo corredores adjacentes (costas com costas) sempre diferem por exatamente `2` (`_are_adjacent_corridors`).
- **Pré-processamento de Risco de Vazamento (`get_corridor_leakage_risk`):**
  - Ao selecionar um corredor, cruza automaticamente os SKUs com os corredores vizinhos (`±2`), exibindo um banner expansível com todos os endereços e SKUs em risco e destacando os cards nos quadrantes com selo `⚠️ Risco Vazamento`.
- **Captura Leve & Reconciliação Espacial Pós-Processamento (`POST /api/v1/carrinho/reconcile_leakages`):**
  - Tags não vinculadas na fila ativa são guardadas em `leakage_records` (maior RSSI por EPC) ancoradas ao `endereco_vazamento` mais próximo (mesma antena/mais recente).
  - Trocar de corredor (`switch_corredor`) preserva o histórico da sessão (`epc_to_address`, `read_progress`, `leakage_records`).
  - Na reconciliação, os EPCs lidos normalmente em um corredor que também vazaram no corredor vizinho (`±2`) revelam quais endereços estão costas com costas, permitindo resgatar peças perdidas por vazamento com segurança.

### C. Dashboard Executivo (`/carrinho/dashboard` e `GET /api/v1/carrinho/dashboard`)
- **6 KPIs:** Acuracidade Global (`%`), Endereços Lidos (`%`), Leitura Direta, Recuperados por Vazamento, Ruas/Endereços com Excesso e Endereços com Risco de Vazamento.
- **3 Gráficos Offline:** Progresso & Divergência por Rua, Donut de Status dos Endereços e Performance por Quadrante/Antena (`Q1`–`Q4`).
- **Auditoria & Exportação:** Tabela filtrável (*Todos*, *Com Falta*, *Com Excesso*, *Risco Vazamento*, *Concluídos*, *Não Lidos*), busca por texto, exportação `.xlsx` via SheetJS local e impressão/PDF.

### D. Aba de Bancada & Gravação (`/gravacao` e `app/routers/api/v1/gravacao.py`)
- **Exclusiva para Tag de Endereço (`edc0`):** Toda a parte de gravação em lote de SKUs foi removida da interface e da API.
- **Busca de Endereço pelo SKU Lido (`GET /api/v1/gravacao/enderecos_por_sku`):** Após selecionar o corredor, o botão **🔍 Buscar por SKU** usa a tag de SKU com maior RSSI na bancada; se houver 1 endereço com aquele SKU na rua, seleciona automaticamente; se houver mais de 1, filtra o seletor para escolha.
- **Sliders Isolados da Bancada:**
  - **Potência de Leitura** (`10.0`–`33.0` dBm)
  - **Potência de Gravação** (padrão `25.0` dBm — alterna apenas durante a gravação no TID e restaura em seguida)
  - **Filtro RSSI (Leitura)** (`-90` a `-20` dBm — filtra apenas a leitura na bancada, sem bloqueio na gravação)

---

## 4. 📂 Principais Arquivos do Projeto

```
carrinho-inventario-renner-v2/
├── HANDOVER.md                                  # Este documento de contexto (v3)
├── main.py                                      # Ponto de entrada FastAPI (porta 8000)
├── config/
│   ├── devices/impinj-17-39-4a.json             # Config R700 LLRP otimizada para CD
│   └── gravacao_bancada.json                    # Config isolada dos sliders da bancada
├── app/
│   ├── routers/
│   │   ├── api/v1/
│   │   │   ├── carrinho.py                      # Motor, risco ±2, reconciliação e /dashboard
│   │   │   └── gravacao.py                      # Bancada endereço, busca por SKU, sliders isolados
│   │   └── pages/
│   │       ├── carrinho.py                      # /carrinho e /carrinho/dashboard
│   │       └── gravacao.py                      # /gravacao
│   ├── services/rfid/
│   │   ├── _main.py                             # RfidManager + forward para carrinho_engine
│   │   └── renner_codec.py                      # Encode/decode edc0 e SKU Renner
│   ├── static/js/xlsx.full.min.js               # SheetJS offline (importação e exportação Excel)
│   └── templates/pages/
│       ├── carrinho/main.html                   # 4 Quadrantes + Risco Vazamento + Botão Dashboard
│       ├── carrinho/dashboard.html              # Dashboard Executivo offline + Exportar Excel/PDF
│       └── gravacao/main.html                   # Bancada Tag de Endereço + Busca por SKU + RSSI
└── tests/
    └── test_carrinho_leakage.py                 # Testes de reconciliação ±2, risco e dashboard
```

---

## 5. 🚀 Como Continuar em Outra Máquina

1. **Clonar o repositório na versão `v3` e subir o ambiente:**
   ```bash
   git clone https://github.com/Arquivy/carrinho-inventario-renner-v2.git
   cd carrinho-inventario-renner-v2
   git checkout v3
   poetry install
   poetry run python main.py
   ```
   *(Lembrete para máquinas com drive C: limitado: configurar cache/venv do Poetry em outro drive antes do `poetry install`.)*

2. **Acessar no navegador:**
   - Home (Importar Planilha): `http://localhost:8000/`
   - Carrinho RFID: `http://localhost:8000/carrinho`
   - Dashboard Executivo: `http://localhost:8000/carrinho/dashboard`
   - Bancada & Gravação: `http://localhost:8000/gravacao`
   - Dispositivos: `http://localhost:8000/settings/devices`

3. **Validar testes automatizados:**
   ```bash
   poetry run pytest
   ```
