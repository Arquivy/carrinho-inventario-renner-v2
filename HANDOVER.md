# 📋 HANDOVER: Carrinho de Inventário RFID Renner

> **Documento de Transição Técnica e Continuidade de Desenvolvimento**  
> **Data de Emissão:** 20 de Novembro de 2026  
> **Localização do Projeto:** `C:\Users\ivan_\Documents\carrinho inventario Renner`  
> **Status:** Base estruturada, especificações técnicas consolidadas, mockup de alta fidelidade gerado, pronto para implementação do pipeline de inventário e interface dedicada.

---

## 1. 🎯 Visão Geral do Projeto

O **Carrinho de Inventário da Renner** é uma solução de automação e conferência de estoque móvel desenvolvida para as lojas e centros de distribuição da **Lojas Renner**. O sistema opera embarcado em um carrinho móvel equipado com leitor RFID e antenas direcionadas, empurrado por um operador ao longo dos corredores de cabides/araras de roupas.

### Objetivos Principais
1. **Inventário Rápido em Movimento contínuo:** Realizar a contagem total de itens sem a necessidade de o operador parar o carrinho a cada arara ou lote de peças.
2. **Associação Dinâmica a Endereços Físicos:** Identificar tags de referência de endereço físico (`edc0`) fixadas nas araras e atribuir as peças lidas antes e depois ao endereço correspondente.
3. **Mitigação Inteligente de "Vazamento" (Crosstalk / Leakage):** Evitar a leitura indevida de peças de corredores adjacentes ou araras traseiras através de regras topológicas (`C - 2` e `C + 2`), orientação de antenas e filtragem temporal/espacial.
4. **Interface Operacional em Tempo Real:** Dashboard visual responsivo com feedback imediato por código de cores (Vermelho, Amarelo, Verde) do progresso de cada endereço e alertas de risco.

---

## 2. 🛠️ Tecnologias Utilizadas e Arquitetura

### Stack de Software
- **Linguagem:** Python 3.11+
- **Framework Web Backend:** [FastAPI](https://fastapi.tiangolo.com/) com servidor assíncrono [Uvicorn](https://www.uvicorn.org/)
- **Gerenciador de Dependências:** [Poetry](https://python-poetry.org/)
- **Frontend / UI:** Jinja2 Templates, Tailwind CSS (via Tailwind CDN / static JS), Alpine.js para reatividade e WebSockets para streaming de leituras em tempo real
- **Banco de Dados Local:** SQLite via [SQLAlchemy](https://www.sqlalchemy.org/) e migrações estruturadas via [Alembic](https://alembic.sqlalchemy.org/)
- **SDK & Protocolo RFID:** Biblioteca `smartx-rfid` (v12.8.5), decodificação nativa do padrão EPC Gen2 / Gen2X da Lojas Renner
- **Observabilidade:** Métricas integradas via Prometheus (`prometheus-fastapi-instrumentator`)
- **Empacotamento Desktop:** Suporte a PyInstaller (`scripts/build_exe.py`) e System Tray (`pystray`)

### Hardware e Infraestrutura Física
- **Leitor RFID Principal:** Impinj Speedway / Revolution **R700** (conexão Ethernet TCP/IP ou IoT Device Interface).
- **Arranjo de Antenas (4 Antenas):**
  - **Antena 1:** Superior Esquerda (Arara Alta Esquerda)
  - **Antena 2:** Inferior Esquerda (Arara Baixa Esquerda)
  - **Antena 3:** Superior Direita (Arara Alta Direita)
  - **Antena 4:** Inferior Direita (Arara Baixa Direita)
- **Modos de Operação de Antena:** Bilateral (ambos os lados simultâneos) ou Unilateral (inventário focado em apenas um dos lados).
- **Tags de Endereço Físico:** Tags de alta durabilidade com prefixo de identificação `edc0`, fixadas no início de cada pack/seção de cabides na arara.
- **Tags de Produto:** Tags EPC Gen2 UHF fixadas nos vestuários, decodificáveis para SKU e Serial.

---

## 3. 🧠 Decisões Técnicas Tomadas

### A. Topologia de Corredores e Risco de Vazamento
- **Numeração Par/Ímpar:** Em lojas Renner, os corredores são segregados: corredores ímpares ficam de um lado do salão/depósito e pares do outro.
- **Regra dos Vizinhos Laterais:** O corredor lateral adjacente a um corredor $C$ é sempre **$C - 2$** e **$C + 2$** (exemplo: para o corredor 131, os vizinhos são 129 e 133).
- **Carga Preventiva:** Ao selecionar o corredor ativo no sistema, o software carrega a lista de SKUs esperados do corredor selecionado e também dos corredores vizinhos ($C \pm 2$).
- **Marcador Visual de Vazamento (`*`):** Se uma peça lida no corredor atual possui SKU esperado no corredor vizinho ($C-2$ ou $C+2$), ela recebe automaticamente o alerta com asterisco `*` na tabela do operador para alertar sobre possível leitura através da divisória/arara.

### B. Atribuição Dinâmica de Endereços (`edc0`)
- **Movimento Fluido:** O carrinho não para. Ao ler uma tag de endereço `edc0`, o sistema abre uma janela de contexto para aquele endereço.
- **Janela de Sensibilidade Temporal:** Tags de produtos lidas dentro de uma janela de tempo configurável em torno da detecção do endereço físico são associadas a ele.
- **Janela de Tolerância por Contagem de Endereços:** Se novos endereços continuarem sendo detectados (ex: já se passaram 3 endereços à frente) e de repente surgir uma tag pertencente a um endereço muito anterior, essa tag é descartada daquele endereço e rotulada como `FORA_DE_RANGE` (suspeita de peça fora de posição ou vazamento tardio).
- **Toggles Independentes de Sensibilidade:** O operador ou engenheiro pode ligar/desligar de forma independente:
  1. O filtro por janela de tempo.
  2. O filtro por tolerância de quantidade de endereços passados.

### C. Triangulação Progressiva e Mapeamento
- As leituras gravam: EPC, SKU decodificado, Antena que leu (1 a 4), Potência da Antena, RSSI médio e Timestamp.
- Leituras marcadas como `FORA_DE_RANGE` são salvas no banco de dados SQLite local com todos os metadados. Elas servem de subsídio para o algoritmo de triangulação espacial: à medida que os corredores adjacentes são lidos, o sistema cruza as leituras para inferir a posição relativa física das araras.

### D. Interface do Operador (UI Dashboard)
O layout e design da interface já foram validados e renderizados em alta fidelidade no arquivo `mockup_frontend.jpg`:
1. **Cabeçalho:** Identificação Renner RFID R700, status de conexão online, seletor de corredor ativo e identificação dos vizinhos laterais.
2. **Barra Dinâmica de Endereços (Cards/Botões):**
   - 🔴 **Vermelho:** Leitura inferior a 50% do volume de peças esperado.
   - 🟡 **Amarelo:** Leitura entre 50% e 89% do esperado.
   - 🟢 **Verde:** Leitura igual ou superior a 90% do esperado.
   - Exibição de percentual numérico e contagem (Ex: `S003 - 94% [47/50]`).
3. **Tabela de Leituras em Tempo Real:** Listagem dinâmica com destaque para SKU, descrição, esperado vs. lido, antena capturadora e alerta de vazamento `*`.
4. **Painel de Antenas & Controles:** Indicador de status das 4 antenas, sliders de tempo de persistência e tolerância de endereços, botões de ação (Iniciar, Pausar, Finalizar e Exportar CSV/Excel).

---

## 4. 📂 Estrutura de Arquivos Relevantes

```
carrinho inventario Renner/
├── CONVERSA_TRANSCRIPT.jsonl           # Transcrição técnica original com todas as definições
├── CONVERSA_TRANSCRIPT_SESSAO_ATUAL.jsonl # Transcrição da sessão de handover e migração
├── HANDOVER.md                         # Este documento de contexto executivo
├── mockup_frontend.jpg                 # Mockup visual em alta fidelidade da tela do operador
├── pyproject.toml                      # Configuração Poetry e dependências (smartx-rfid 12.8.5)
├── poetry.lock                         # Lockfile do ecossistema Python
├── main.py                             # Ponto de entrada da aplicação FastAPI
├── app/
│   ├── async_func/
│   │   └── rfid.py                     # Loop assíncrono de polling/ingestão de leituras RFID
│   ├── core/
│   │   ├── build_app.py                # Fábrica de inicialização FastAPI
│   │   ├── config.py                   # Configurações do sistema
│   │   └── middleware.py               # Middlewares da aplicação
│   ├── db/                             # Gerenciador de sessão SQLAlchemy
│   ├── models/
│   │   └── rfid.py                     # Modelos ORM de Tags, Eventos e Leituras
│   ├── routers/
│   │   ├── api/v1/                     # Endpoints REST (rfid, devices, simulator, etc.)
│   │   └── pages/                      # Rotas de renderização de páginas HTML
│   ├── schemas/                        # Validações Pydantic
│   ├── services/                       # Regras de negócio e integração com leitor R700
│   ├── static/                         # Arquivos CSS, JS (Tailwind, Alpine), ícones e sons
│   └── templates/                      # Templates Jinja2 (HTML do dashboard)
├── docs/
│   ├── Consulta_Estoque__1_.csv        # Planilhas de exemplo de estoque esperado da Renner
│   └── dispatches.md                   # Documentação de despachos e eventos
└── scripts/
    ├── startup.bat / startup.sh        # Scripts de inicialização
    └── build_exe.py                    # Script de compilação em executável único
```

---

## 5. 🚀 Próximos Passos e Pendências Técnicas

Para dar continuidade na nova máquina de desenvolvimento, siga a seguinte ordem prioritária:

1. **Instalação e Setup do Ambiente:**
   ```bash
   # Clonar / abrir a pasta do projeto
   cd "carrinho inventario Renner"

   # Instalar dependências via Poetry
   poetry install

   # Executar as migrações do banco de dados local
   poetry run alembic upgrade head

   # Iniciar o servidor em modo de desenvolvimento
   poetry run python main.py
   # Acesso: http://localhost:5000
   ```

2. **Implementar a Lógica do Seletor de Corredor e Vizinhos:**
   - Criar módulo em `app/services/corridor_service.py` para receber o corredor ativo (ex: `131`) e carregar preventivamente a lista de SKUs esperados de `131`, `129` e `133` a partir das planilhas de estoque (`docs/Consulta_Estoque*.csv`).
   - Implementar flag `is_leak_risk: bool` para marcar tags com `*` quando o SKU coincidir com o corredor vizinho.

3. **Implementar o Gerenciador de Endereçamento Dinâmico (`edc0`):**
   - Criar `app/services/address_tracker.py` para processar a fila de tags do leitor.
   - Detectar tag de endereço `edc0` e chavear o endereço atual do carrinho.
   - Aplicar a janela de sensibilidade (tempo em segundos e tolerância de `N` endereços passados).
   - Classificar tags extemporâneas como `FORA_DE_RANGE`.

4. **Desenvolver a Nova Página do Dashboard do Operador:**
   - Criar `app/templates/pages/cart_inventory/index.html` e a rota correspondente em `app/routers/pages/cart.py`, espelhando fielmente o visual de `mockup_frontend.jpg`.
   - Implementar WebSockets ou SSE para transmissão ao vivo das leituras para a tabela e atualização da cor dos botões de endereço (Vermelho < 50%, Amarelo 50-89%, Verde >= 90%).

5. **Exportação de Relatório (CSV / Excel):**
   - Criar endpoint de finalização de corredor que gera o relatório contendo: Corredor, Endereço Físico, EPC, SKU, Descrição, Qtd Esperada, Qtd Lida, Antena, RSSI médio, Potência e Alerta de Vazamento.
