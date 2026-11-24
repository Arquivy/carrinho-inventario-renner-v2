# 📋 HANDOVER: Carrinho de Inventário RFID Renner

> **Documento de Transição Técnica e Continuidade de Desenvolvimento**  
> **Última Atualização:** 24 de Novembro de 2026  
> **Localização Original:** `C:\Users\ivan_\Documents\carrinho inventario Renner`  
> **Status:** Sistema funcional com leitor Impinj R700 operando em **LLRP** (`R420`), aba **Bancada & Gravação** com sliders de potência de leitura/gravação, **Carrinho RFID** com ativação dinâmica de fila por tag `edc0` e buffer temporal, e **Planilha de Teste de Vazamento** (`C129`, `C131`, `C133`) carregada.

---

## 1. 🎯 Visão Geral do Projeto

O **Carrinho de Inventário da Renner** é uma solução de automação e conferência de estoque móvel para os centros de distribuição e lojas da **Lojas Renner**. O sistema combina:
1. **Home (`/`):** Importação de planilha Excel/CSV (offline via SheetJS local), seleção de corredor, gerenciamento de dispositivos e tabela comparativa de estoque.
2. **Carrinho RFID (`/carrinho`):** Dashboard operacional em 4 quadrantes (Antenas 1 a 4) com ativação dinâmica por tags de endereço (`edc0...`) e reconciliação temporal de SKUs pré-lidos.
3. **Bancada & Gravação (`/gravacao`):** Ferramenta de preparação para gravar tags físicas de endereço e lotes de SKUs ancoradas estritamente no **TID** (evitando crosstalk) com controle independente de potência de leitura e gravação.

---

## 2. 🛠️ Stack & Arquitetura Atual

- **Linguagem:** Python 3.11+ (gerenciado via **Poetry**)
- **Backend:** FastAPI + Uvicorn (porta padrão `8000`)
- **Frontend:** Jinja2 + Tailwind CSS + Alpine.js + SheetJS local (`app/static/js/xlsx.full.min.js`)
- **Banco de Dados:** SQLite (`database.db`) via SQLAlchemy (`Event`, `Inventory`, `Tag`)
- **SDK RFID:** `smartx-rfid` (v12.8.5) + `sllurp` (LLRP)

### Hardware & Configuração Crítica do Leitor Impinj R700
- **IP do Leitor:** `192.168.1.21` (`config/devices/impinj-17-39-4a.json`)
- **Firmware do Leitor:** `8.2.3` (`"firmware_version": null` configurado para não bloquear conexão)
- **Descoberta Crítica (REST vs LLRP):**
  - O firmware `8.2.3` do R700 **não possui** o endpoint REST `/api/v1/profiles/inventory/tag-access` (introduzido apenas no firmware 8.4.x+), o que causava `404 Not Found` (`ERROR POSTING WRITE COMMAND`) ao tentar gravar via driver `R700_IOT`.
  - **Solução Implementada:** O leitor R700 foi comutado para a interface **`llrp`** (porta `5084`) e configurado no projeto com `"reader": "R420"` (driver LLRP nativo do `smartx_rfid`), que suporta leitura contínua de EPC + TID + RSSI e gravação nativa `C1G2Write` ancorada no TID.
  - Se o leitor for reiniciado ou resetado para REST em outro ambiente, basta enviar `PUT https://192.168.1.21/api/v1/system/rfid/interface` com `{"rfidInterface": "llrp"}`.

---

## 3. ✅ Funcionalidades Já Implementadas e Testadas

### A. Codec RFID Renner (`app/services/rfid/renner_codec.py`)
- **Tag de Endereço (`edc0`):** `renner_serialize_epc_by_address("EN0.C131.S001.N1.0")` → `edc000000131000100010000` (24 hex) e decodificação reversa `renner_get_address_by_epc`.
- **Tag de SKU:** `renner_get_epc_from_sku(sku, serial)` → `0000{sku:012x}{serial:08x}` (24 hex com serial sequencial não repetitivo) e `get_renner_sku`.

### B. Motor Dinâmico do Carrinho (`/carrinho` e `app/routers/api/v1/carrinho.py`)
- **Mapeamento Físico de 4 Quadrantes:**
  - Seções **Ímpares** = Esquerda (`Q1` Nível 2 Superior / Antena 1, `Q2` Nível 1 Inferior / Antena 2)
  - Seções **Pares** = Direita (`Q3` Nível 2 Superior / Antena 3, `Q4` Nível 1 Inferior / Antena 4)
- **Ativação Dinâmica da Fila:** Os quadrantes iniciam vazios. Um endereço só entra na fila ativa quando sua tag física `edc0...` é lida.
- **Buffer Pré-leitura com Decaimento Temporal:** SKUs lidos antes da tag de endereço ficam retidos no buffer temporário; quando a tag `edc0` correspondente é lida dentro da janela de decaimento (`time_decay` de 1s a 30s), os SKUs são transferidos automaticamente para o endereço.
- **Fila Bounded & Vazamento:** Limite configurável por quadrante (`1` a `8` endereços). Leituras de corredores diferentes do selecionado são ignoradas na fila ativa.
- **Hook em Tempo Real:** `app/services/rfid/_main.py` encaminha toda tag recebida diretamente para `carrinho_engine.on_tag_read()`.

### C. Aba de Bancada & Gravação (`/gravacao` e `app/routers/api/v1/gravacao.py`)
- Seleção de Corredor e Endereço do banco/planilha.
- Captura das tags presentes na bancada com TID, EPC, RSSI e tipo decodificado.
- Gravação de **Tag de Endereço** e **Lote de SKUs** ancorada ao **TID** (`target_identifier="tid"`).
- **Sliders de Potência R700 (`10.0` a `33.0 dBm`):**
  - *Potência de Leitura:* Atualiza o leitor em tempo real e persiste em `config/devices/impinj-17-39-4a.json`.
  - *Potência de Gravação:* Aplicada automaticamente no momento da escrita no TID (foco curto para evitar crosstalk) e restaurada para a potência de leitura ao finalizar.

### D. Base de Teste de Vazamento (`C129`, `C131`, `C133`)
- Arquivos gerados na raiz: `planilha_teste_3_corredores.xlsx` e `planilha_teste_3_corredores.csv` (também já carregados no `database.db`).
- **Estrutura:** 3 corredores vizinhos (`C129`, `C131`, `C133`), 10 endereços por corredor (`S001` a `S005` x `N1`/`N2`), máximo de 20 peças/SKUs por endereço.

---

## 4. 📂 Principais Arquivos do Projeto

```
carrinho inventario Renner/
├── HANDOVER.md                            # Este resumo completo para continuidade
├── main.py                                # Ponto de entrada FastAPI (porta 8000)
├── database.db                            # SQLite com os 30 endereços de teste (C129, C131, C133)
├── planilha_teste_3_corredores.xlsx/.csv  # Planilha teste de vazamento (3 corredores, 10 end/corr, <=20 SKU)
├── config/devices/impinj-17-39-4a.json    # Config do R700 usando driver LLRP ("reader": "R420")
├── app/
│   ├── routers/
│   │   ├── api/v1/
│   │   │   ├── carrinho.py                # Motor dinâmico, fila por quadrante, buffer temporal
│   │   │   └── gravacao.py                # APIs da bancada, gravação por TID e sliders de potência
│   │   └── pages/
│   │       ├── index.py                   # Home (/)
│   │       ├── carrinho.py                # Página /carrinho
│   │       └── gravacao.py                # Página /gravacao
│   ├── services/rfid/
│   │   ├── _main.py                       # RfidManager + forward em tempo real para carrinho_engine
│   │   └── renner_codec.py                # Encode/decode edc0 e SKU Renner
│   ├── static/js/xlsx.full.min.js         # SheetJS offline para importação Excel/CSV
│   └── templates/
│       ├── includes/header.html           # Menu superior com abas Carrinho RFID e Bancada & Gravação
│       └── pages/
│           ├── carrinho/main.html         # UI 4 Quadrantes + Sliders + Modo Tempo Real/Lote
│           └── gravacao/main.html         # UI Bancada + Sliders Potência Leitura/Gravação + Lote TID
```

---

## 5. 🚀 Como Continuar em Outro PC

1. **Clonar o repositório e subir o ambiente:**
   ```bash
   git clone https://github.com/Arquivy/carrinho-inventario-renner-v2.git
   cd carrinho-inventario-renner-v2
   poetry install
   poetry run python main.py
   ```
   - Acessar no navegador:
     - Home: `http://localhost:8000/`
     - Carrinho RFID: `http://localhost:8000/carrinho`
     - Bancada & Gravação: `http://localhost:8000/gravacao`

2. **Próximos Passos Recomendados:**
   - Testar a gravação completa da Tag de Endereço + Lote de SKUs na bancada em LLRP ajustando os sliders de potência de leitura e gravação.
   - Executar o teste de campo/bancada simulando o corredor `C131` com tags dos corredores `C129` e `C133` próximas para validar a rejeição de vazamento (crosstalk) e calibrar o `time_decay` e a `Potência de Leitura`.
   - Caso precise de marcação visual explícita de peças suspeitas de vazamento (`C-2` / `C+2`) ou exportação de relatório consolidado pós-inventário do corredor, implementar sobre o `carrinho_engine`.
